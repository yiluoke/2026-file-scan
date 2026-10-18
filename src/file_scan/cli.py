"""ユーザー向けCLI。棚卸し・DB初期化・登録を明示的に分ける。

終了コード: 0=正常終了、1=設定/DBなど実行全体の失敗、2=未処理/部分失敗あり。
業務文書・認証情報が混ざる例外全文は、通常の端末出力には表示しない。
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.engine import make_url

from file_scan import __version__
from file_scan.collector import collect
from file_scan.config import ConfigError, load_config
from file_scan.db import migrate, open_database, tables
from file_scan.pipeline import PipelineError, ingest, require_schema, write_json


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="文書の棚卸し・抽出・DB下書き登録")
    parser.add_argument("--version", action="version", version=__version__)
    commands = parser.add_subparsers(dest="command", required=True)
    for command, description in (
        ("scan", "対象と除外の棚卸し。原本コピー・DB更新はしません"),
        ("init-db", "登録用のDBテーブルを非破壊で初期化"),
        ("ingest", "収集・抽出し、DBへ下書き登録"),
        ("status", "指定ナレッジの登録状況を表示"),
    ):
        subcommand = commands.add_parser(command, help=description)
        subcommand.add_argument("--config", required=True, type=Path, help="YAML設定ファイル")
        if command == "ingest":
            subcommand.add_argument("--retry-partial", action="store_true",
                                    help="同じ入力・設定の部分抽出も再実行する")
    return parser


def _status(engine, knowledge_id: str) -> dict:
    require_schema(engine)
    members = tables["knowledge_documents"]
    generations = tables["extraction_generations"]
    with engine.connect() as conn:
        catalog = conn.execute(select(tables["knowledge_bases"]).where(
            tables["knowledge_bases"].c.knowledge_id == knowledge_id)).mappings().first()
        if catalog is None:
            return {"knowledge_id": knowledge_id, "documents": 0, "generations": {}}
        documents = conn.execute(select(func.count()).select_from(members).where(
            members.c.knowledge_id == knowledge_id, members.c.included.is_(True))).scalar()
        rows = conn.execute(select(generations.c.processing_status, func.count()).join(
            members, members.c.document_id == generations.c.document_id
        ).where(members.c.knowledge_id == knowledge_id).group_by(generations.c.processing_status))
        return {"knowledge_id": knowledge_id, "documents": documents,
                "scan_status": catalog["scan_status"], "search_enabled": catalog["enabled"],
                "generations": dict(rows.all())}


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    engine = None
    try:
        settings = load_config(arguments.config)
        if arguments.command == "scan":
            # DBへ接続せず、対象ルート・除外設定だけを確認する。
            scan = collect(settings)
            report = {
                "run_id": str(uuid4()), "scanned_at": datetime.now(timezone.utc).isoformat(),
                "items": [{"document_id": item.document_id, "source_id": item.source_id,
                           "relative_path": item.relative_path, "format": item.format}
                          for item in scan.items],
                "exclusions": scan.exclusions, "errors": scan.errors,
                "successful_sources": scan.successful_sources,
            }
            path = settings.report_dir / f"scan-{report['run_id']}.json"
            write_json(path, report)
            print(json.dumps({"discovered": len(scan.items), "excluded": len(scan.exclusions),
                              "errors": len(scan.errors), "report": str(path)}, ensure_ascii=False))
            return 2 if scan.errors else 0
        url = make_url(settings.database_url)
        if url.get_backend_name() == "sqlite" and url.database and url.database != ":memory:":
            Path(url.database).parent.mkdir(parents=True, exist_ok=True)
        engine = open_database(settings.database_url)
        if arguments.command == "init-db":
            migrate(engine)
            print("DBスキーマv1を確認しました。データの削除や検索公開は行いません。")
            return 0
        if arguments.command == "status":
            print(json.dumps(_status(engine, settings.knowledge_id), ensure_ascii=False))
            return 0
        report = ingest(settings, engine, retry_partial=arguments.retry_partial)
        print(json.dumps({"status": report["status"], **report["summary"],
                          "report": str(settings.report_dir / f"ingest-{report['run_id']}.json")},
                         ensure_ascii=False))
        return 0 if report["status"] == "succeeded" else 2
    except (ConfigError, PipelineError) as exc:
        print(f"エラー: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("処理を中断しました。次回起動時に中断したジョブを回収します。", file=sys.stderr)
        return 130
    except Exception as exc:
        # SQLAlchemy例外には接続先やパラメーターが含まれる。型だけを通知する。
        print(f"実行に失敗しました ({type(exc).__name__})。設定・DB接続・保存先を確認してください。",
              file=sys.stderr)
        return 1
    finally:
        if engine is not None:
            engine.dispose()
