"""一時PostgreSQLで契約テストを実行する開発補助。原本・本番DBは使わない。

準備: pip install -e '.[dev]' pgserver==0.1.4
実行: python scripts/test_postgres.py
PostgreSQLバイナリを同梱するpgserverはテスト専用で、本体の依存には含めない。
"""

import os
import subprocess
import sys
import tempfile
from pathlib import Path


def main() -> int:
    import pgserver
    import psycopg

    # Unix socketのパス長制限を避けるため、macOSでも短い一時パスを使う。
    directory = "/tmp" if os.name != "nt" else None
    with tempfile.TemporaryDirectory(prefix="fs-pg-", dir=directory) as path:
        server = pgserver.get_server(Path(path), cleanup_mode="stop")
        try:
            with psycopg.connect(server.get_uri()) as connection:
                version = connection.execute("SHOW server_version").fetchone()[0]
            print("検証用PostgreSQL: " + version, flush=True)
            # URI（認証情報を含み得る）は表示せず、テストプロセスへだけ渡す。
            environment = dict(os.environ, FILE_SCAN_TEST_DATABASE_URL=server.get_uri())
            return subprocess.call([sys.executable, "-m", "pytest", "-q", *sys.argv[1:]],
                                   env=environment)
        finally:
            server.cleanup()


if __name__ == "__main__":
    raise SystemExit(main())
