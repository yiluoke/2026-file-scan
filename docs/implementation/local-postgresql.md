# ローカル検証用 PostgreSQL＋pgvector・PGroonga

ローカル検証も PostgreSQL 18＋pgvector 0.8.6 を使用する。実際の本番の採用版が決まったら、ローカルのバージョンも合わせる。macOSではPostgres.app、Dockerを導入済みの環境ではComposeを利用できる。同じ5432番ポートを使うため、どちらか一方を起動する。

- アプリ用DB：`file_scan`
- テスト専用DB：`file_scan_test`
- ユーザー：`file_scan`（ローカル開発専用の管理ユーザー）
- 接続先：`127.0.0.1:5432`（SCRAMパスワード認証）

既存SQLiteのデータは自動移行しない。同じ原本をPostgreSQLへ再取り込みする。

## 接続設定

リポジトリのルートで実行する。`.env`と`config.local.yaml`がすでにある場合はコピーせず、内容を確認する。

```sh
cp .env.example .env
chmod 600 .env
cp config.example.yaml config.local.yaml
```

`.env`の`POSTGRES_PASSWORD`を設定し、2つの接続URLの`PASSWORD`を同じ値に置き換える。シェルとURLで扱いやすい英数字のランダム値を推奨する。URLの予約文字を使う場合はURL側だけパーセントエンコードする。

```sh
source .venv/bin/activate
set -a
source .env
set +a
```

Python CLIは`.env`を自動読込しないため、新しいターミナルでも読み込む。

## macOS：Postgres.app

[公式ダウンロード](https://postgresapp.com/downloads.html)からPostgreSQL 18を含むPostgres.appを取得し、`/Applications/Postgres.app`へ配置する。v2.9.6はPostgreSQL 18.6とpgvector 0.8.6を含む。以下ではGUIの自動初期化を使わず、このプロジェクト専用のデータ領域をCLIで管理する。

```sh
export PATH="/Applications/Postgres.app/Contents/Versions/18/bin:$PATH"
```

初回だけ、未作成の`data/postgresql`へ初期化する。パスワードの入力には`.env`の`POSTGRES_PASSWORD`と同じ値を指定する。既存クラスタがある場合はこの初期化を飛ばす。

```sh
initdb -D data/postgresql -U file_scan --encoding=UTF8 --locale=C --auth=scram-sha-256 -W
cat >> data/postgresql/postgresql.conf <<'CONF'
listen_addresses = '127.0.0.1'
port = 5432
unix_socket_directories = ''
CONF
pg_ctl -D data/postgresql -l data/postgresql/server.log -w start
PGPASSWORD="$POSTGRES_PASSWORD" createdb -h 127.0.0.1 -U file_scan file_scan
PGPASSWORD="$POSTGRES_PASSWORD" psql -h 127.0.0.1 -U file_scan -d file_scan -v ON_ERROR_STOP=1 -f scripts/init-postgres.sql
```

通常の起動・状態確認・停止は次のコマンドを使う。Macのログイン時に自動起動する設定は不要で、再起動後は明示的に起動する。

```sh
pg_ctl -D data/postgresql -l data/postgresql/server.log -w start
pg_ctl -D data/postgresql status
pg_ctl -D data/postgresql -m fast -w stop
```

DBの実体・ログはGit対象外の`data/postgresql/`、認証情報は`.env`に置く。

## Docker Compose

[compose.yaml](../../compose.yaml)は`pgvector/pgvector:0.8.6-pg18-trixie`を使用する。接続設定を作成後、次を実行する。

```sh
docker compose up -d --wait db
```

新しいボリュームでは`init-postgres.sql`が両DBのpgvector有効化とテストDB作成を行う。既存ボリュームでは初期化SQLは再実行されない。既存の`file_scan`へvectorを追加する場合は次を使い、テストDBがなければ別途作成する。

```sh
docker compose exec db psql -U file_scan -d file_scan -v ON_ERROR_STOP=1 -c 'CREATE EXTENSION IF NOT EXISTS vector;'
docker compose exec db psql -U file_scan -d postgres -v ON_ERROR_STOP=1 -c 'CREATE DATABASE file_scan_test OWNER file_scan;'
docker compose exec db psql -U file_scan -d file_scan_test -v ON_ERROR_STOP=1 -c 'CREATE EXTENSION IF NOT EXISTS vector;'
```

`CREATE DATABASE`は未作成の場合だけ実行する。既存データを残して停止する場合は`docker compose down`を使う。ネイティブ版の`data/postgresql/`とComposeの名前付きボリュームは別の保存先になる。

## PGroonga

2026-10-25に、ネイティブのPostgreSQL 18.6とpgvector 0.8.6へPGroonga 4.1.0を追加した。新しいネイティブ環境では[PGroonga導入手順](pgroonga.md)で実行時ライブラリ・DBごとの拡張検索パスと有効化を設定してから、以下の全テストを実行する。既存ComposeイメージにはPGroongaを含めていない。

## アプリと実DBの検証

接続設定をシェルへ読み込んでから実行する。

```sh
python scripts/download_samples.py
python scripts/download_docling_models.py --config config.local.yaml
file-scan scan --config config.local.yaml
file-scan init-db --config config.local.yaml
file-scan ingest --config config.local.yaml
file-scan status --config config.local.yaml
python scripts/test_postgres.py
python -m ruff check src tests scripts
```

`test_postgres.py`はテスト専用接続先を必須とし、PostgreSQL・pgvector・PGroongaの導入を確認し、版を表示してテストを実行する。テストでは一時表でベクトル保存・コサイン距離・HNSW索引の利用を確認し、取り込み契約テストは専用DB内のランダムなschemaを作成・削除する。SQLiteの単体テストも互換性確認用に残している。

OpenAI Embeddingの設定・生成・保存は[専用手順](openai-embedding.md)を参照。PGroongaの拡張導入と日本語索引の基本動作は確認済み。検索APIは未実装。
