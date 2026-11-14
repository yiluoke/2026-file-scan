"""固定版CodeWikiを子プロセスで呼ぶ。プロセス出力を通常ログへ出さない。"""

from __future__ import annotations

import os
import signal
import subprocess
import tempfile
from dataclasses import asdict
from pathlib import Path
from urllib.parse import urlsplit

from .collector import CPP_EXTENSIONS
from .store import ScanError, bytes_hash, read_json, write_json


class CodeWiki:
    def __init__(self, settings):
        self.settings = settings

    def _run(self, operation: str, repo: Path, output: Path) -> dict:
        settings = self.settings
        bridge = Path(__file__).with_name("codewiki_bridge.py")
        payload = {"operation": operation, "repo": str(repo), "output": str(output),
                   "cpp_extensions": sorted(CPP_EXTENSIONS),
                   "documentation": asdict(settings.documentation),
                   "expected_version": settings.codewiki.expected_version,
                   "expected_commit": settings.codewiki.expected_commit,
                   "limits": asdict(settings.codewiki)}
        # Python実行パス以外に秘密を渡さず、他処理の認証変数を継承しない。
        names = {"PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "TEMP", "TMP", "TMPDIR",
                 "LANG", "LC_ALL", "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE",
                 "TIKTOKEN_CACHE_DIR"}
        env = {k: v for k, v in os.environ.items() if k in names}
        if cache_dir := env.get("TIKTOKEN_CACHE_DIR"):
            # 子プロセスはソースの作業コピーで起動するため、呼出元を基準にする。
            env["TIKTOKEN_CACHE_DIR"] = str(Path(cache_dir).expanduser().resolve())
        if operation == "generate":
            doc = settings.documentation
            for name in (doc.model_env, doc.api_key_env, doc.base_url_env):
                value = os.environ.get(name, "")
                if not value or any(c in value for c in "\r\n\x00"):
                    raise ScanError("llm_environment_missing")
                env[name] = value
            endpoint = urlsplit(env[doc.base_url_env])
            if (endpoint.scheme not in {"http", "https"} or not endpoint.hostname
                    or endpoint.username is not None or endpoint.password is not None
                    or endpoint.query or endpoint.fragment):
                raise ScanError("invalid_llm_endpoint")
        output.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="bridge-", dir=settings.artifact_root) as temporary:
            home = Path(temporary)
            env.update(HOME=str(home), USERPROFILE=str(home), XDG_CONFIG_HOME=str(home / "config"),
                       PYTHON_DOTENV_DISABLED="1", MERMAID_VALIDATE="0", OTEL_SDK_DISABLED="true")
            request, response = home / "request.json", home / "response.json"
            write_json(request, payload)
            try:
                # -Iで親側cpp_scan/codewiki.pyによる同名パッケージの隠蔽やPYTHONPATH混入を防ぐ。
                process = subprocess.Popen([settings.codewiki.python, "-I", str(bridge), str(request),
                                            str(response)], cwd=repo, env=env,
                                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                           start_new_session=os.name != "nt")
            except OSError:
                raise ScanError("codewiki_python_unavailable") from None
            try:
                process.wait(timeout=settings.codewiki.timeout_seconds)
            except BaseException as exc:
                if os.name == "nt":
                    subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
                else:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                process.wait()
                if isinstance(exc, subprocess.TimeoutExpired):
                    raise ScanError("codewiki_timeout") from None
                raise
            if not response.is_file():
                raise ScanError("codewiki_no_response")
            result = read_json(response)
            if process.returncode != 0 or "error" in result:
                code = result.get("error", "failed")
                if code in {
                    "codewiki_version_mismatch", "llm_environment_missing", "ModuleNotFoundError",
                    "codewiki_checkout_modified", "codewiki_generation_incomplete",
                    "codewiki_not_installed", "codewiki_tokenizer_download_failed",
                    "codewiki_tokenizer_cache_invalid",
                }:
                    raise ScanError(code)
                detail = f": {code}" if (isinstance(code, str) and code.isascii()
                                          and code.isidentifier() and len(code) <= 64) else ""
                raise ScanError(f"codewiki_process_failed ({operation}{detail})")
            return result

    def profile(self) -> dict:
        return {"version": self.settings.codewiki.expected_version,
                "commit": self.settings.codewiki.expected_commit,
                "bridge_sha256": bytes_hash(Path(__file__).with_name("codewiki_bridge.py").read_bytes())}

    def analyze(self, repo: Path, output: Path) -> dict:
        return self._run("analyze", repo, output)

    def generate(self, repo: Path, output: Path) -> dict:
        return self._run("generate", repo, output)
