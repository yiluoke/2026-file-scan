"""検索用CLI。起動・公開操作を取り込みコマンドから分離する。"""

import argparse
import json
import sys

from sqlalchemy import select

from file_scan.config import load_config
from file_scan.db import open_database
from search import schema
from search.management import create_release, publish, unpublish


def parser():
    root = argparse.ArgumentParser(description="ローカル試作の検索API・公開対象管理")
    commands = root.add_subparsers(dest="command", required=True)
    for name in ("init-db", "create-release", "publish", "unpublish", "status", "serve"):
        command = commands.add_parser(name)
        command.add_argument("--config", required=True)
        if name in {"create-release", "publish", "unpublish"}:
            command.add_argument("--release", required=True)
        if name in {"publish", "unpublish"}:
            command.add_argument("--document-id", required=True)
        if name == "create-release":
            command.add_argument("--make-default", action="store_true")
        if name == "publish":
            command.add_argument("--generation-id", required=True)
            command.add_argument("--reviewed-by", required=True)
            command.add_argument("--allow-partial", action="store_true")
            command.add_argument("--include-image-descriptions", action="store_true",
                                 help="原図と照合した画像説明を公開に含める")
            command.add_argument("--feature-id", action="append", default=[])
        if name == "unpublish":
            command.add_argument("--withdrawn-by", required=True)
        if name == "serve":
            command.add_argument("--port", type=int, default=8000)
            command.add_argument("--allow-history", action="store_true")
    return root


def main(argv=None):
    args = parser().parse_args(argv)
    engine = None
    try:
        if args.command == "serve":
            import uvicorn
            from search.api import create_app
            from search.config import load_search_settings

            if not 1 <= args.port <= 65535:
                raise ValueError("invalid_port")
            app = create_app(load_search_settings(args.config), allow_history=args.allow_history)
            uvicorn.run(app, host="127.0.0.1", port=args.port, access_log=False)
            return 0
        settings = load_config(args.config)
        engine = open_database(settings.database_url)
        if args.command == "init-db":
            schema.migrate(engine)
            output = {"search_schema_version": 1, "ingestion_schema_version": 3}
        elif args.command == "create-release":
            output = {
                "release_id": create_release(
                    engine, settings, args.release, make_default=args.make_default
                )
            }
        elif args.command == "publish":
            output = {
                "publication_id": publish(
                    engine,
                    settings,
                    args.release,
                    args.document_id,
                    args.generation_id,
                    args.reviewed_by,
                    allow_partial=args.allow_partial,
                    include_image_descriptions=args.include_image_descriptions,
                    feature_ids=args.feature_id,
                )
            }
        elif args.command == "unpublish":
            output = unpublish(
                engine, settings, args.release, args.document_id, args.withdrawn_by
            )
        else:
            with engine.connect() as c:
                output = {
                    "catalogs": [
                        dict(r)
                        for r in c.execute(
                            select(schema.catalogs).where(
                                schema.catalogs.c.knowledge_id == settings.knowledge_id
                            )
                        ).mappings()
                    ],
                    "releases": [
                        dict(r)
                        for r in c.execute(
                            select(schema.releases).where(
                                schema.releases.c.knowledge_id == settings.knowledge_id
                            )
                        ).mappings()
                    ],
                }
        print(json.dumps(output, ensure_ascii=False, default=str))
        return 0
    except schema.SearchIndexError as error:
        print(f"検索索引を確認してください ({', '.join(error.missing_indexes)})。", file=sys.stderr)
        return 1
    except Exception as error:
        # 生SQL・原本・APIキーを含み得る例外本文は端末に転記しない。
        from search.management import PublicationError

        code = str(error) if isinstance(error, PublicationError) else type(error).__name__
        print(
            f"検索処理を実行できません ({code})。設定・DB・公開対象を確認してください。",
            file=sys.stderr,
        )
        return 1
    finally:
        if engine:
            engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
