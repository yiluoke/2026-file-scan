# 専用スキーマ winnap の配置と移行

更新日：2026-11-02。PostgreSQLのアプリケーションテーブルを`public`から専用スキーマ`winnap`へ変更した。DB名・接続ユーザー名・原本保管先は変更しない。

## 配置

| 対象 | 配置・扱い |
| --- | --- |
| 取込の20テーブル | `winnap.documents`、`winnap.blocks`、`winnap.chunks`等 |
| 検索テーブル | `winnap.search_*`。取込と同じ物理スキーマで、管理版・metadataは分離 |
| 管理版 | `winnap.schema_migrations`は取込v3、`winnap.search_schema_migrations`は検索v1。配置変更だけでは管理版番号を変更しない |
| pgvector | 現行どおり`public`。型・関数・演算子のため接続の`search_path`に含める |
| PGroonga | 既存の拡張配置を維持。スキーマ移行スクリプトは拡張を移動・再作成しない |
| SQLite | テスト用。SQLAlchemyのスキーマ変換で`winnap`を既定DBへ対応付け、既存の単体テストを維持 |

DB接続の標準`search_path`は`winnap, public`。`pg_catalog`はPostgreSQLの暗黙の優先検索を使用する。アプリのテーブルはSQLAlchemyでスキーマ名を明示し、`winnap`に表がない場合に`public`の同名表へ自動で切り替えない。同期のCLI接続と非同期の検索API接続で同じ配置規則を使う。

スキーマ名は`src/file_scan/namespace.py`の固定値`winnap`。利用者向けに別名を指定するYAML設定は追加していない。実DBテストでは、一時スキーマへの対応付けを使ってデータを隔離する。

## 新規導入

接続ユーザーは対象DBにスキーマを作成できる権限を持つか、管理者が`winnap`を先に作成して必要な`USAGE`・`CREATE`権限を付与する。実行用ロールを分ける場合は、対象テーブル・シーケンスへの必要な権限も別途付与する。

次のどちらかを使う。

```sh
# Python CLIから作成
file-scan init-db --config config.local.yaml

# または新規導入用SQLから作成
psql -h DB_HOST -p 5432 -U DB_USER -d FILE_SCAN_DB \
  -v ON_ERROR_STOP=1 -f sql/postgresql/schema-v3.sql
```

`init-db`と配布SQLは`winnap`を作成してから表を作る。配布SQLは新規導入専用で、`winnap`または`public`に同名の取込テーブルがあれば停止する。検索を利用する場合は、その後`file-search init-db --config config.local.yaml`で同じ`winnap`内に検索表を追加する。

## 既存 public 配置からの移行

移行対象は、完全な取込v3と、存在する場合は完全な検索v1のテーブル一式。旧v1/v2は旧配置に対応する配布版でv3へ更新してから移行する。旧テーブルを持つDBに新しい`init-db`を先に実行すると、二重構築を避けるため移行スクリプトの実行を案内して停止する。

1. 対象DBと原本・画像の保管先をバックアップする。
2. 取込バッチ・Embedding・公開管理処理・検索APIを停止する。
3. 接続設定を読み込み、次を実行する。

```sh
source .venv/bin/activate
set -a
source .env
set +a

python scripts/migrate_postgres_schema.py --config config.local.yaml
file-scan init-db --config config.local.yaml
file-scan status --config config.local.yaml
# 検索を導入済み、または今回追加する場合
file-search init-db --config config.local.yaml
file-search status --config config.local.yaml
```

移行スクリプトは以下を検査・実行する。

- 移行元の表の完全性と、取込v3・検索v1の版を検査する。
- 移行先に既存の表・索引・シーケンス等がある場合は停止し、自動統合しない。
- 取込・公開処理と共通のバッチ排他、および初期化処理のDDL排他を取得する。
- 許可したアプリテーブルだけを、`ALTER TABLE ... SET SCHEMA winnap`で1トランザクション内に移動する。別アプリの`public`テーブルは残す。
- 索引・制約・テーブル所有シーケンス・FK参照・本文・ベクトル・公開コピー・世代ID・ストレージ参照を保持する。拡張や原本実体は移動しない。
- 途中失敗は全体をrollbackする。移行済みで整合する場合の再実行は`unchanged`になる。

配置変更だけなら再抽出・再Embedding・再公開は不要。接続プールに旧配置の接続やPrepared Statementを残さないよう、移行後にCLI・APIを再起動する。他ツールのSQLが`public.documents`等を直接参照している場合は、`winnap.documents`等へ修正する。

## 実装と検証

- `src/file_scan/db.py`・`src/search/schema.py`：明示的な`winnap`のテーブル定義。
- `src/file_scan/namespace.py`・`src/search/config.py`：同期・非同期接続、テスト用スキーマ変換。
- `scripts/export_postgres_schema.py`：実装から`winnap`付きDDLを生成。
- `scripts/migrate_postgres_schema.py`：既存データを保持した名前空間の移行。
- `tests/test_namespace.py`：本文・ベクトル・検索公開・索引・シーケンス・FKの保持、衝突・未知版・検索表不足の拒否、途中失敗のrollback、旧配置への参照防止。

検証結果（2026-11-02）：PostgreSQL 18.6・pgvector 0.8.6・PGroonga 4.1.0で全218テストが成功、スキップなし。既存依存の非推奨警告5件。ruff、SQL再生成一致、git diff --checkも成功した。テスト専用DB内の一時スキーマで検証し、実OpenAI API送信・業務DBの移行は行っていない。

参考：[PostgreSQLのALTER TABLE](https://www.postgresql.org/docs/18/sql-altertable.html)、[SQLAlchemyのスキーマ変換](https://docs.sqlalchemy.org/en/20/core/connections.html#translation-of-schema-names)。
