# 現行スキーマv3の新規導入用SQL

Python・ライブラリ・DBサーバー・モデルを含む導入全体は[環境構築手順](../../docs/file-scan/本番導入仕様.md)を参照する。

[schema-v3.sql](schema-v3.sql)は、現在実装されている20テーブル、PK・UNIQUE・CHECK・外部キー、12個の明示索引を作成する。`pgvector`の有効化と`schema_migrations`へのv3適用記録も含む。検索専用のsearch_*テーブルと将来のACL表は含めない。検索表はfile-search init-dbで同じwinnap内に作成する。

正本は[src/file_scan/db.py](../../src/file_scan/db.py)。このSQLは[scripts/export_postgres_schema.py](../../scripts/export_postgres_schema.py)で実装定義から生成する。文書データ・原本・画像・APIキー・DBユーザー・パスワードは含めない。

2026-11-02：アプリテーブルの作成先を専用スキーマ`winnap`へ変更した。取込・検索の配置と旧`public`からの移行は[既存DBの移行手順](../../docs/file-scan/本番導入仕様.md#recovery)を参照。

## 導入手順

検証環境はPostgreSQL 18.6／pgvector 0.8.6。別の版を使用する場合は、その環境でも実行確認する。

1. 導入先のPostgreSQLサーバーにpgvector拡張のパッケージを配置する。
2. 導入先のDBと接続ユーザーを用意する。ユーザーには`winnap`スキーマの作成権限、または管理者が作成済みの`winnap`に対する`USAGE`・`CREATE`権限が必要。拡張の作成権限がない場合は、管理者が対象DBで先に`CREATE EXTENSION vector WITH SCHEMA public;`を実行する。
3. リポジトリのルートから次を実行する。接続情報は導入先の値へ置き換える。認証にはパスワード入力・`.pgpass`等を使い、SQLに認証情報を書かない。

```sh
psql -h DB_HOST -p 5432 -U DB_USER -d FILE_SCAN_DB \
  -v ON_ERROR_STOP=1 -f sql/postgresql/schema-v3.sql
```

SQLは`BEGIN`～`COMMIT`で実行する。同名の管理テーブルがある場合はエラーにし、既存構造の違いを隠す`CREATE TABLE IF NOT EXISTS`は使用しない。`winnap`内の既存v1/v2を更新する場合は、このSQLではなく`file-scan init-db --config 設定ファイル`を使う。既存の`public`配置は先に専用手順で移行し、旧v1/v2なら旧配置に対応する配布版でv3へ更新してから移行する。テーブル削除・既存データ削除は行わない。

標準の作成先は`winnap`。SQLがスキーマを作成し、全アプリテーブル・FKを`winnap`で修飾する。pgvectorは引き続き`public`に配置する。DBに拡張ファイルが未導入の場合、`CREATE EXTENSION`だけではインストールできない。既存のvector拡張が別schemaにある場合、このSQLは停止する。

## アプリへの接続

新環境の`config.local.yaml`または`FILE_SCAN_DATABASE_URL`へ接続先を設定する。SQLAlchemy用URLは`postgresql+psycopg://...`を使用する。新環境の収集元・`storage_dir`・モデル配置先も設定し、実行する。

```sh
file-scan status --config config.local.yaml
file-scan ingest --config config.local.yaml
```

初期状態は管理版3だけが登録され、文書は0件。`init-db`を続けて実行しても適用済みとして扱える。`ingestion_settings.storage_root`はこのSQLでは設定せず、最初のingestで新環境の保管先を登録する。

アプリの同期・非同期接続は`search_path=winnap,public`を設定し、テーブルも`winnap`で明示修飾する。SQL中の`SET LOCAL search_path`は導入トランザクション内だけに適用される。検索公開・ACL同期は現行と同じく無効／draftの制約を維持する。

## 作成テーブル

| テーブル | 役割 |
| --- | --- |
| `knowledge_bases` | ナレッジの範囲・棚卸し状態 |
| `documents` | 原本の論理台帳・最新原本版 |
| `knowledge_documents` | ナレッジごとの収集対象・状態 |
| `artifact_assets` | 原本・画像実体の保存先とハッシュ |
| `document_versions` | 原本の内容版・取得時情報 |
| `conversion_profiles` | 形式別の変換設定・処理版 |
| `extraction_generations` | 原本版と変換設定に対応する世代 |
| `blocks` | 本文・見出し・表等の抽出要素 |
| `chunks` | 検索用の分割本文・検索文脈 |
| `chunk_blocks` | チャンク本文と原本ブロックの対応 |
| `asset_occurrences` | 画像実体が原本内に現れる位置 |
| `artifact_relations` | ブロック間の参照・派生関係 |
| `index_jobs` | 処理試行・失敗・再試行の履歴 |
| `ingestion_settings` | DBに対応する実体保管先の固定 |
| `schema_migrations` | 適用済みのDBスキーマ版 |
| `embedding_profiles` | モデル・次元・入力形式の識別 |
| `chunk_embeddings` | チャンクごとのpgvectorベクトル |
| `selected_generations` | ナレッジ・文書ごとの選択世代 |
| `extraction_cache` | 分割条件から独立した抽出結果 |
| `embedding_cache` | モデル・入力ハッシュ単位のベクトル |

## 初期値と時刻列

現行定義の`default=`・`onupdate=`の多くはSQLAlchemyが実行時に補完する値であり、DBの`DEFAULT`やトリガーではない。このSQLも同じ物理定義を再現する。アプリを使わず直接INSERTする場合は、JSON、配列、状態、`created_at`・`updated_at`等の必須値を明示する。直接UPDATEする場合も`updated_at`を明示する。

`schema_migrations`の初期行だけは、SQL内で必須時刻を明示して登録する。整数単独PKの`schema_migrations.version`と`ingestion_settings.singleton_id`は現行DDLと同じ`SERIAL`になり、関連シーケンスも自動で作成される。通常のアプリはキー値を明示する。

## SQLの再生成と検査

依存ライブラリを導入したプロジェクト環境で実行する。DB接続は不要。

```sh
python scripts/export_postgres_schema.py
python scripts/export_postgres_schema.py --check
```

同梱SQLと現行定義の一致、新規導入後の物理構造とアプリの`migrate`で作った構造の一致、取り込み動作、同じSQLの再実行時の既存データ保護は`tests/test_schema_sql.py`で検証する。

検証結果（2026-10-25）：PostgreSQL 18.6／pgvector 0.8.6のテスト専用DBに作った空の一時schemaで導入を実行し、列・PK・UNIQUE・CHECK・外部キー・索引・列コメントがアプリ作成スキーマと一致した。適用版3の認識、取り込み・再実行、再導入の拒否と既存データ保持、導入途中の失敗時の全体rollbackも確認済み。`tests/test_schema_sql.py`と`tests/test_db.py`の計37テスト成功、ruff・`git diff --check`・SQL再生成一致チェックも成功した。開発用DBのテーブル・データは変更していない。
