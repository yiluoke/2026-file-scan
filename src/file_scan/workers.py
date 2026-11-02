"""上限付きの解析プロセスを再利用し、期限超過・メモリ超過時だけ交換する。"""

from __future__ import annotations

import multiprocessing
import sys
from contextvars import ContextVar
from pathlib import Path

ACTIVE_POOL: ContextVar = ContextVar("file_scan_parser_pool", default=None)


class WorkerError(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _worker(channel, backend, hidden, artifacts, ocr):
    from file_scan.parsers import parse_file

    try:
        while True:
            request = channel.recv()
            if request is None:
                break
            path, file_format = request
            try:
                result = parse_file(Path(path), file_format, pdf_backend=backend,
                                    include_hidden=hidden, docling_artifacts_path=Path(artifacts),
                                    pdf_ocr=ocr)
                response = (True, result)
            except Exception as error:
                response = (False, getattr(error, "code", type(error).__name__))
            try:
                import resource
                memory = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
                memory_mb = memory / (1024 * 1024 if sys.platform == "darwin" else 1024)
            except ImportError:
                # Windowsではタスク数による交換を必須とし、メモリ計測可否を記録する。
                memory_mb = None
            channel.send((*response, memory_mb))
    except (EOFError, BrokenPipeError):
        pass
    finally:
        channel.close()


class ParsePool:
    def __init__(self, settings):
        self.settings = settings
        self.slots = [None] * settings.parse_workers
        self.cursor = 0
        self.metrics = {"started": 0, "reused": 0, "recycled": 0, "peak_memory_mb": 0,
                        "memory_measurement_available": True}

    def __enter__(self):
        self.token = ACTIVE_POOL.set(self)
        return self

    def _stop(self, index):
        slot = self.slots[index]
        if slot is None:
            return
        process, channel, _ = slot
        channel.close()
        if process.is_alive():
            process.terminate()
        process.join(timeout=2)
        if process.is_alive():
            process.kill()
            process.join()
        self.slots[index] = None

    def __exit__(self, *_):
        ACTIVE_POOL.reset(self.token)
        for index, slot in enumerate(self.slots):
            if slot is not None:
                process, channel, _ = slot
                try:
                    channel.send(None)
                except (BrokenPipeError, EOFError, OSError):
                    pass
                process.join(timeout=1)
                self._stop(index)

    def extract(self, asset, item):
        index = self.cursor % len(self.slots)
        self.cursor += 1
        if self.slots[index] is None:
            context = multiprocessing.get_context("spawn")
            parent, child = context.Pipe()
            process = context.Process(target=_worker, args=(
                child, self.settings.pdf_backend, self.settings.include_hidden,
                str(self.settings.docling_artifacts_path), self.settings.pdf_ocr))
            try:
                process.start()
            except BaseException:
                parent.close()
                child.close()
                raise
            child.close()
            self.slots[index] = (process, parent, 0)
            self.metrics["started"] += 1
        else:
            self.metrics["reused"] += 1
        process, channel, tasks = self.slots[index]
        try:
            channel.send((str(asset.path), item.format))
            timeout = (self.settings.pdf_ocr.timeout_seconds
                       if item.format == "pdf" and self.settings.pdf_backend == "docling"
                       and self.settings.pdf_ocr.enabled else self.settings.parse_timeout_seconds)
            if not channel.poll(timeout):
                raise WorkerError("parse_timeout")
            success, result, memory = channel.recv()
            if memory is None:
                self.metrics["memory_measurement_available"] = False
            else:
                self.metrics["peak_memory_mb"] = max(self.metrics["peak_memory_mb"], round(memory, 1))
            self.slots[index] = (process, channel, tasks + 1)
            if tasks + 1 >= self.settings.parse_worker_max_tasks or (
                memory is not None and memory >= self.settings.parse_worker_max_memory_mb
            ):
                self._stop(index)
                self.metrics["recycled"] += 1
            if not success:
                raise WorkerError(result)
            return result
        except (EOFError, BrokenPipeError, OSError):
            self._stop(index)
            raise WorkerError("parser_process_failed") from None
        except BaseException:
            self._stop(index)
            raise
