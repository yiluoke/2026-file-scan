"""インストール済みのPostgreSQL＋pgvectorで検証する。テスト専用DBを使用する。

実行前に.envをシェルへ読み込む。pgserverによる一時DBは使用しない。
"""

import os
import subprocess
import sys

from sqlalchemy import text

from file_scan.db import open_database


def main() -> int:
    url = os.environ.get("FILE_SCAN_TEST_DATABASE_URL")
    if not url:
        print("FILE_SCAN_TEST_DATABASE_URLにテスト専用DBを指定してください。", file=sys.stderr)
        return 1
    engine = open_database(url)
    try:
        if engine.dialect.name != "postgresql":
            raise ValueError("PostgreSQLの接続先が必要です")
        with engine.connect() as connection:
            version = connection.execute(text("SHOW server_version")).scalar_one()
            vector = connection.execute(text(
                "SELECT extversion FROM pg_extension WHERE extname = 'vector'"
            )).scalar_one()
        print(f"検証用PostgreSQL: {version} / pgvector: {vector}", flush=True)
    except Exception as exc:
        print(f"検証用DB・vector拡張を確認できません ({type(exc).__name__})。", file=sys.stderr)
        return 1
    finally:
        engine.dispose()
    # CLIテストの明示的な接続先をアプリ用環境変数で上書きしない。
    environment = dict(os.environ)
    environment.pop("FILE_SCAN_DATABASE_URL", None)
    return subprocess.call([sys.executable, "-m", "pytest", "-q", *sys.argv[1:]],
                           env=environment)


if __name__ == "__main__":
    raise SystemExit(main())
