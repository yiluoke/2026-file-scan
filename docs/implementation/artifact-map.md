# フォルダ配下の成果物と役割

対象：`/Users/yiluoke/Documents/2026-file-scan`

実装・設定・設計資料はファイル単位で、DB内部ファイル、仮想環境、キャッシュはフォルダ単位で整理する。認証情報の値は記載しない。

## 1. 全体構成

```text
2026-file-scan/
├── config.local.yaml               実際に使うローカル設定
├── config.example.yaml             標準設定のひな型
├── config.postgresql.example.yaml  PostgreSQL設定のひな型
├── .env                            接続情報・APIキー
├── .env.example                    環境変数のひな型
├── compose.yaml                    DockerでDBを起動する場合の構成
├── pyproject.toml                  依存関係・CLI・テスト・静的検査の設定
├── .python-version                 Python 3.14.4の指定
├── requirements/                   Ubuntu標準版の対応表・制約・全依存の固定
├── src/file_scan/                  アプリ本体
├── tests/                          自動テストとテスト用設定生成
├── scripts/                        準備・DB初期設定・検証用コマンド
├── examples/                       公開サンプル取得一覧と旧サンプル
├── docs/                           設計・操作説明
├── data/                           仮想ファイルサーバー・DB実体等
└── artifacts/                      原本コピー・抽出結果・レポート
```

## 2. 設定ファイル

| ファイル | 役割 | 現在の使い方 |
| --- | --- | --- |
| [config.local.yaml](../../config.local.yaml) | アプリを実行する際の実設定。収集元、DB、保存先、抽出・分割条件、Embeddingを指定する | CLIの`--config config.local.yaml`で明示的に読み込む。Git対象外 |
| [config.example.yaml](../../config.example.yaml) | 標準設定のひな型 | `config.local.yaml`へコピーして利用する。現在はPostgreSQL＋仮想ファイルサーバー用 |
| [config.postgresql.example.yaml](../../config.postgresql.example.yaml) | PostgreSQL向け設定例 | 現在の標準設定例と主要な設定値は同じ。テスト専用設定ではない |
| [.env](../../.env) | DB認証情報、アプリ用・テスト用DBの接続URL、OpenAI APIキー | シェルで読み込んで環境変数として渡す。Git対象外 |
| [.env.example](../../.env.example) | `.env`のひな型 | 実際のパスワード・APIキーを設定する前の説明と仮の値を記載する |
| [compose.yaml](../../compose.yaml) | Docker利用時のPostgreSQL＋pgvector構成 | DBイメージ、ポート、永続ボリューム、初期化SQL、ヘルスチェックを定義する。Macへ直接導入したPostgres.appとは別の起動方法 |
| [pyproject.toml](../../pyproject.toml) | Pythonプロジェクト共通設定 | 依存ライブラリ、`file-scan`コマンド、pytestの探索先、ruffの検査ルールを定義する |
| [.python-version](../../.python-version) | Python管理ツール向けの版指定 | Ubuntu 26.04標準実体に合わせた3.14.4 |
| [ubuntu-26.04-baseline.json](../../requirements/ubuntu-26.04-baseline.json) | Ubuntu公式の版との対応表 | 索引URL・ハッシュ、標準対応版、追加依存、互換例外の理由 |
| [ubuntu-26.04-constraints.txt](../../requirements/ubuntu-26.04-constraints.txt) | 依存解決の制約 | Ubuntu標準対応版と5件の互換例外を指定 |
| [ubuntu-26.04-tools.in](../../requirements/ubuntu-26.04-tools.in) | 構築用Pythonツール | pip 25.1.1とsetuptools 78.1.1 |
| [ubuntu-26.04.lock](../../requirements/ubuntu-26.04.lock) | 間接依存を含めた固定一覧 | dev・search、OSごとのCPU用PyTorch等のmarkerも含む |
| [.gitignore](../../.gitignore) | Git管理から除外するパスの定義 | 実設定、認証情報、原本、DB、生成物、仮想環境等を除外する |

### config.local.yamlの主な項目

| 項目 | 意味 | 現在の設定 |
| --- | --- | --- |
| `knowledge_id` | 取り込み結果をまとめるナレッジのID | `demo` |
| `system_id` / `environment` | ナレッジの対象システムと環境 | `demo-system` / `sample` |
| `database_url` | アプリのDB接続先 | ローカルの`file_scan`。環境変数があればそちらを優先 |
| `sources[].source_id` | 収集元の継続的な識別子 | `local-file-server` |
| `sources[].root` | 収集対象の最上位フォルダ | `data/file-server/department` |
| `sources[].recursive` | サブフォルダも収集するか | `true` |
| `exclude_directories` / `exclude_filenames` | 収集対象から除外するフォルダ・ファイル名 | `旧版/`、Office一時ファイル、TMP、作業中ファイル |
| `include_extensions` | 収集する拡張子 | TXT・MD・PY・XLSX・XLSM・DOCX・PDF |
| `storage_dir` | 原本コピー・画像の保存先 | `artifacts/storage` |
| `report_dir` | レポート・抽出結果の出力先 | `artifacts/reports` |
| `pdf_backend` | PDF抽出方式 | `docling`。レイアウト・表構造の実モデルを使用 |
| `docling_artifacts_path` | Doclingモデル配置先 | `data/models/docling` |
| `chunk_max_characters` | チャンク本文の文字数上限 | `1800`。トークン数とは別 |
| `max_file_bytes` | 収集する1ファイルのサイズ上限 | 100 MiB |
| `copy_retries` | コピー中に原本が変化した場合の再試行回数 | `2` |
| `parse_timeout_seconds` | 1文書の抽出処理の時間上限 | `120`秒 |
| `embedding.enabled` | OpenAI Embedding生成を有効にするか | `false` |
| `embedding.model` / `dimensions` | 使用モデルと出力ベクトルの次元数 | `text-embedding-3-small` / `1536` |
| `embedding.api_key_env` / `api_key` | APIキーの取得元となる環境変数名・YAML直接指定値 | `OPENAI_API_KEY`。現在は仮キー |
| `embedding.batch_size` / `timeout_seconds` | APIへ送信する件数とタイムアウト | `16`件 / `60`秒 |

相対パスはYAMLファイルの所在を基準に解決する。収集元の内部に`storage_dir`や`report_dir`を置くと自己取り込みになるため、設定検証で拒否する。

### .envと設定の優先順位

| 環境変数 | 用途 |
| --- | --- |
| `POSTGRES_PASSWORD` | ローカルDBのユーザー認証用。Composeの初期化等に使用する |
| `FILE_SCAN_DATABASE_URL` | アプリ用のDB接続URL。現在は`file_scan`へ接続する |
| `FILE_SCAN_TEST_DATABASE_URL` | 自動テスト専用のDB接続URL。現在は`file_scan_test`へ接続する |
| `OPENAI_API_KEY` | OpenAI APIキー。現在は仮キーを設定している |

DB接続先の優先順位は`FILE_SCAN_DATABASE_URL`、YAMLの`database_url_env`で指定した環境変数、YAMLの`database_url`、プログラム既定値の順。Embeddingのキーは`api_key_env`で指定した環境変数をYAMLの`api_key`より優先する。

Python CLIは`.env`を自動読込しない。次のようにシェルへ読み込む。

```sh
set -a
source .env
set +a
```

YAMLへ直接実キーを書く場合、環境変数に残っている仮キーが優先される点に注意する。必要に応じて`.env`の仮キー行をコメントアウトし、`unset OPENAI_API_KEY`で解除する。

## 3. テスト用の設定と実行方法

現在、`config.test.yaml`のようなテスト専用YAMLファイルはない。次の組み合わせでテストを設定する。

| 設定場所 | 役割 |
| --- | --- |
| `.env`の`FILE_SCAN_TEST_DATABASE_URL` | テスト専用DBの接続先を指定する |
| `tests/conftest.py` | PostgreSQL用の共通fixture。テスト専用DBにランダムなschemaを作り、スキーマ初期化後にテストへ渡し、終了時に削除する |
| `tests/test_pipeline.py`のfixture | 同じ取り込みテストをSQLiteとPostgreSQLで実行する。原本フォルダや保存先はpytestの一時フォルダに作る |
| `tests/test_embeddings.py`のfixture | テスト用の収集元、モデル、次元数、仮のキーをPythonコードで設定する。OpenAI SDKのHTTP応答を模擬する |
| 各テストの`tmp_path`・一時YAML | 設定読込、相対パス、CLI動作等のため、テストごとに専用ファイル・フォルダを生成する |
| `pyproject.toml`の`tool.pytest.ini_options` | `tests/`を探索し、`src/`をPythonの探索パスに追加する |
| `scripts/test_postgres.py` | PostgreSQL・pgvectorへの接続を確認してpytestを実行する。子プロセスからアプリ用`FILE_SCAN_DATABASE_URL`を外し、各テストの接続設定が上書きされるのを防ぐ |

通常の検証は次のコマンドを使う。

```sh
source .venv/bin/activate
set -a
source .env
set +a
python scripts/test_postgres.py
```

`python -m pytest`単独でも実行できるが、テスト用DB接続先がない場合は実PostgreSQLのテストがスキップされる。SQLiteは単体テストと互換性確認にも使用する。ローカルのアプリ実行先はPostgreSQLである。

2026-10-25の追加設定：`chunk_max_tokens`、`chunk_context_max_tokens`、`parse_workers`、`parse_worker_max_tasks`、`parse_worker_max_memory_mb`、`embedding.read_batch_size`、`embedding.batch_max_tokens`。既定値・意味は[取り込み改善設計](../design/05-ingestion-improvements.md)を参照。テストは各fixtureから同じSettings型で制限を変えて検証する。

## 4. アプリの各ソース

| ファイル | 役割 |
| --- | --- |
| [cli.py](../../src/file_scan/cli.py) | コマンドの入口。`scan`・`init-db`・`ingest`・`embed`・`status`を受け付け、設定読込、処理呼出、表示、終了コードを扱う |
| [config.py](../../src/file_scan/config.py) | YAML・環境変数から`Settings`、`Source`、`EmbeddingSettings`を組み立てる。相対パス、除外設定、対象範囲、型、重複、自己取り込み等を検証する |
| [collector.py](../../src/file_scan/collector.py) | 収集元を再帰的に列挙する。対象拡張子・除外条件・リンク等を判定し、安定した文書IDと原本URIを付ける。列挙エラーと空フォルダを区別する |
| [storage.py](../../src/file_scan/storage.py) | 原本を安全にコピーする。コピー前後の変更を検出し、内容ハッシュで原本・画像を保管する。既存の保管物の欠落・改変も確認する |
| [parsers.py](../../src/file_scan/parsers.py) | ファイル形式別の抽出器。テキスト、Markdown、Python、Word、Excel、PDFを共通のブロックへ変換し、行・セル・ページ等の原本位置を保持する。Python原本は実行しない |
| [domain.py](../../src/file_scan/domain.py) | 抽出・分割・登録で使う共通データ型。`Block`、`ExtractionResult`、`ExtractedAsset`、`Relation`、`Chunk`、`ChunkBlock`を定義する |
| [chunking.py](../../src/file_scan/chunking.py) | 抽出ブロックを検索用チャンクへ分割・結合する。文書・節の文脈と原本の文字範囲の対応を保持する |
| [db.py](../../src/file_scan/db.py) | SQLAlchemyによるテーブル・FK・制約・索引、DB接続、スキーマ初期化を定義する。v1/v2からv3へ選択世代・キャッシュ等を非破壊で追加する |
| [pipeline.py](../../src/file_scan/pipeline.py) | 収集から抽出・分割・DB登録・確認ファイル出力までをつなぐ。排他、再実行、処理世代、ジョブ、文書単位のトランザクション、部分失敗を管理する |
| [embeddings.py](../../src/file_scan/embeddings.py) | 対象チャンクの選択、トークン数確認、OpenAI SDK呼出、応答検証、pgvector保存を行う。入力ハッシュによる再利用、プロファイル分離、失敗時の再開も扱う |
| [profiles.py](../../src/file_scan/profiles.py) | 形式別の抽出プロファイル、分割プロファイル、全体の変換設定ハッシュを生成する |
| [cache.py](../../src/file_scan/cache.py) | 抽出済みブロック・画像参照等の保存・整合性検査・分割時の再利用 |
| [tokens.py](../../src/file_scan/tokens.py) | cl100k_baseでのトークン計測と、検索文脈の予算内への制限 |
| [workers.py](../../src/file_scan/workers.py) | 解析子プロセスの再利用・時間制限・タスク数／メモリ閾値での交換 |
| [models.py](../../src/file_scan/models.py) | 固定モデルと配置manifest・実体のハッシュ照合、プロセス内の検査キャッシュ |
| [state.py](../../src/file_scan/state.py) | 選択世代・最新原本版・設定一致を検査する現在状態の共通クエリ |
| [docling-models.lock.json](../../src/file_scan/docling-models.lock.json) | Git管理するモデルの固定コミット・期待ファイル・SHA-256 |
| [__main__.py](../../src/file_scan/__main__.py) | `python -m file_scan`でCLIを起動する入口 |
| [__init__.py](../../src/file_scan/__init__.py) | Pythonパッケージの初期定義とアプリのバージョン情報 |

主要な実行経路は次のとおり。

```text
scan:    cli → config → collector → 棚卸しレポート
init-db: cli → config → db → スキーマ初期化・移行
ingest:  cli → config → pipeline
                       ├→ collector → storage → parsers → chunking
                       └→ db登録 → 抽出結果・レポート出力
embed:   cli → config → embeddings → OpenAI API → pgvector保存
status:  cli → config → db → 登録状況の表示
```

## 5. 各テストソース

| ファイル | 主な検証内容 |
| --- | --- |
| [conftest.py](../../tests/conftest.py) | 実PostgreSQLの共通fixtureとテスト用schemaの作成・削除 |
| [test_collection.py](../../tests/test_collection.py) | 設定読込、相対パス、収集条件、除外、収集失敗、原本コピーと保管物の整合性 |
| [test_parsers.py](../../tests/test_parsers.py) | 形式別の本文・表・画像・数式等の抽出、原本位置、未対応部分の記録 |
| [test_chunking.py](../../tests/test_chunking.py) | 分割サイズ、文脈、原本文字範囲との対応 |
| [test_db.py](../../tests/test_db.py) | テーブルの制約、FK、初期化、再実行、v1/v2→v3移行、未知スキーマ版の拒否等。主にSQLiteの単体テスト |
| [test_pipeline.py](../../tests/test_pipeline.py) | 取り込み一連の処理、同一原本の再利用、変更、失敗時のrollback、削除・除外、排他、CLI等。SQLiteと実PostgreSQLの両経路 |
| [test_docling.py](../../tests/test_docling.py) | Doclingの実モデルによる本文・表・座標・原画像保持、OCR待ち、モデル不足の検証 |
| [test_postgres.py](../../tests/test_postgres.py) | 実PostgreSQL上のpgvector型、距離演算、HNSW索引の利用 |
| [test_embeddings.py](../../tests/test_embeddings.py) | キー設定と秘匿、OpenAI SDKのHTTP要求、実pgvector保存、再利用、モデル変更、認証失敗、不正応答、入力上限、途中再開 |
| [test_public_samples.py](../../tests/test_public_samples.py) | ダウンロード済み公開8ファイルのハッシュ、抽出、原本位置、除外、実DB登録、再取り込み、原本の非変更 |
| [test_schema_sql.py](../../tests/test_schema_sql.py) | 配布用SQLと実装の一致、実PostgreSQLでの構造一致、取り込み、既存データ保護、途中失敗時の全体rollback |

`test_postgres.py`はテストの中身で、`scripts/test_postgres.py`はテストを起動する補助コマンドである。

追加テスト：[test_improvements.py](../../tests/test_improvements.py)は、世代取り違え・段階別再利用・モデル改変・表見出し・長文脈・DB/API上限・状態表示・I/O・ワーカー交換・v2移行を検証する。

## 6. 準備スクリプトと入力サンプル

| ファイル・フォルダ | 役割 |
| --- | --- |
| [scripts/download_docling_models.py](../../scripts/download_docling_models.py) | 固定lockに従うモデル取得、明示更新、配置済み実体の整合性検証 |
| [scripts/download_samples.py](../../scripts/download_samples.py) | 公開サンプルのダウンロード、ハッシュ・形式検査、仮想ファイルサーバーへの配置、除外確認用ファイルの作成 |
| [scripts/init-postgres.sql](../../scripts/init-postgres.sql) | 開発用DBのvector拡張有効化、テスト専用DB作成、同DBの拡張有効化。Composeの新規ボリューム初期化時にも使用 |
| [scripts/export_postgres_schema.py](../../scripts/export_postgres_schema.py) | 実装のDB定義から新規導入用SQLを生成し、`--check`で同梱SQLとの一致を検査する |
| [sql/postgresql/schema-v3.sql](../../sql/postgresql/schema-v3.sql) | 別環境へ現行20テーブル・制約・12索引・pgvector・適用版を導入するSQL |
| [sql/postgresql/README.md](../../sql/postgresql/README.md) | 導入手順、作成表の役割、アプリ側の初期値、SQLの再生成方法 |
| [scripts/test_postgres.py](../../scripts/test_postgres.py) | テスト専用DBへの接続確認とpytest起動 |
| [scripts/check_python_environment.py](../../scripts/check_python_environment.py) | Python実体と、そのOSで必要な全固定依存の版照合。今回のmacOS環境では97件を確認 |
| [examples/public-samples.json](../../examples/public-samples.json) | 公開サンプル8件のURL、提供元、保存先、SHA-256を記録した取得一覧 |
| [examples/input/ユーザー登録.md](../../examples/input/ユーザー登録.md) | 以前の最小動作確認に使用した架空のMarkdown文書。現在の標準収集先は仮想ファイルサーバー |
| [examples/input/register_sample.py](../../examples/input/register_sample.py) | Pythonの構造抽出用の小さな架空ソース。同上 |

## 7. 設計資料と操作説明

| ファイル | 役割 |
| --- | --- |
| [README.md](../../README.md) | プロジェクトの目的、導入、主要コマンド、実装範囲、資料への入口 |
| [environment-setup.md](environment-setup.md) | 別環境のPython・必要ライブラリ、DB・テーブル、モデル、設定、取り込み確認、Windows・閉域環境の構築手順 |
| [ubuntu-26.04-python.md](ubuntu-26.04-python.md) | Python 3.14.4への切替、Ubuntu標準対応版、5件の互換例外、全179テストの検証記録 |
| [ubuntu-26.04-vm.md](ubuntu-26.04-vm.md) | 本番のWindowsホスト上Ubuntu 26.04 VMに合わせたPython・DB・SMBマウント・systemd起動・受入確認 |
| [01-document-conversion.md](../design/01-document-conversion.md) | 収集、形式別抽出、原本対応、分割、品質管理等の設計 |
| [02-search-platform.md](../design/02-search-platform.md) | pgvector・全文検索・検索API・権限制御等の設計。設計には未実装部分も含む |
| [03-llm-chat.md](../design/03-llm-chat.md) | Dify、LLM回答、チャット画面、根拠提示等の設計案 |
| [04-database-tables.md](../design/04-database-tables.md) | 最終構成を含むDBの論理・カラム・制約・索引の設計案。現在のDDL実装は`db.py` |
| [first-version.md](first-version.md) | 現行CLIの基本操作、処理状態、再実行、対応範囲の説明 |
| [local-postgresql.md](local-postgresql.md) | ローカルPostgreSQL＋pgvectorの導入、起動、停止、接続、検証手順 |
| [openai-embedding.md](openai-embedding.md) | OpenAIの設定、仮想ファイルサーバー、サンプル準備、Embedding実行・保存・検証の説明 |
| [docling-pdf.md](docling-pdf.md) | Doclingのモデル導入、標準PDF抽出、実モデルの検証手順 |
| [artifact-map.md](artifact-map.md) | 本一覧。成果物・設定・各ソースの役割を整理する |

追加設計：[05-ingestion-improvements.md](../design/05-ingestion-improvements.md)は8改善の確定仕様、設定・キャッシュキー・DB移行・運用を定義する。

## 8. 原本・DB・生成物

以下はGit管理対象外のローカル成果物。

| パス | 役割 |
| --- | --- |
| `data/file-server/department/text/` | 公開TXT原本2件：`rfc8259.txt`、`rfc4180.txt` |
| `data/file-server/department/word/` | 公開Word原本2件：`test.docx`、`blocks.docx`。`~$lock.docx`は除外確認用 |
| `data/file-server/department/excel/` | 公開Excel原本2件：`test1.xlsx`、`multisheet.xlsx` |
| `data/file-server/department/pdf/` | 公開PDF原本2件：`dummy.pdf`、`table.pdf` |
| `data/file-server/department/旧版/` | 収集対象外フォルダの動作確認用 |
| `data/postgresql/` | Macへ直接導入したPostgreSQLのクラスタ実体。DB、設定、稼働情報、ログ等を含む。アプリ用とテスト用のDBはこのクラスタ内にある |
| `data/models/docling/` | レイアウト・表構造モデルと、版・ハッシュの取得記録 |
| `data/tokenizer-cache/` | 入力トークン数検証用に取得したトークナイザー辞書。実行時のキャッシュ先はtiktokenの設定に従う |
| `data/file_scan.sqlite3` | PostgreSQLへ切り替える前のローカルSQLite DB。現在の標準設定では参照しない |
| `data/file_scan.sqlite3.ingest.lock` | 上記SQLiteに対応する取り込みロック用ファイル |
| `artifacts/storage/` | 内容ハッシュで保管した原本コピー・抽出画像等。原本の共有フォルダと分離した保管先 |
| `artifacts/reports/scan-*.json` | 対象・除外・列挙エラーの棚卸し結果 |
| `artifacts/reports/ingest-*.json` | 各ファイルの登録・再利用・失敗・部分抽出等の結果 |
| `artifacts/reports/generations/<世代ハッシュ>/manifest.json` | 対象の抽出世代の識別・処理状態・集計等 |
| `artifacts/reports/generations/<世代ハッシュ>/document.json` | 抽出ブロック、原本位置、画像参照、関係情報 |
| `artifacts/reports/generations/<世代ハッシュ>/chunks.jsonl` | 1行1チャンクの確認用出力。原本ブロックとの対応を含む |
| `artifacts/reports/generations/<世代ハッシュ>/review.md` | 人が読むための抽出本文と原本位置の確認用Markdown |
| [public-samples-download.json](../../artifacts/reports/public-samples-download.json) | サンプル取得日時、URL、サイズ、ハッシュの記録 |
| [local-validation.md](../../artifacts/reports/local-validation.md) | 公開8ファイルと自動テストの検証結果。Doclingへの切り替え後に更新 |

DB・保管先はアプリが管理する成果物で、抽出結果を確認する場合は`artifacts/reports/`を参照する。生成した確認ファイルを編集しても、DBへ逆反映されない。

## 9. 実行環境・管理用ファイル

| パス | 役割 |
| --- | --- |
| `.venv` / `.venv-ubuntu2604/` | 通常使うPython 3.14.4の独立仮想環境。現在は`.venv`から後者へのリンク |
| `.venv-python312-backup-20261025/` | 変更前のPython 3.12環境の保管。通常実行には使用しない |
| `artifacts/ubuntu2604/python/` | 現環境で使用するPython 3.14.4本体。仮想環境が参照するため保持する |
| `artifacts/ubuntu2604/`のその他 | Ubuntu公式版の調査資料、検証ログ、版照合JSON、配布wheel、構築ツールとキャッシュ |
| `src/file_scan.egg-info/` | Pythonパッケージ導入時に生成される依存関係・入口等のメタデータ |
| `.pytest_cache/` | pytestの前回実行情報等 |
| `.ruff_cache/` | ruffの検査キャッシュ |
| `**/__pycache__/` | Pythonが生成する実行用バイトコードのキャッシュ |
| `.git/` | Gitの変更履歴・管理情報 |
| `.DS_Store` | macOS Finderが作成するフォルダ表示用メタデータ |

## 10. 普段変更する場所

- 収集対象や除外条件を変える：`config.local.yaml`の`sources`と`include_extensions`。
- DB接続先やAPIキーを変える：`.env`。実行するシェルへ再読込する。
- Embeddingの有効化・モデル・次元数を変える：`config.local.yaml`の`embedding`。
- サンプル取得先を変える：`examples/public-samples.json`。
- ファイル形式別の抽出を改善する：`src/file_scan/parsers.py`。
- 分割・原本対応を改善する：`src/file_scan/chunking.py`。
- 取り込みの全体挙動を変える：`src/file_scan/pipeline.py`。
- DB定義や移行を変える：`src/file_scan/db.py`。
- OpenAI API呼出とベクトル保存を変える：`src/file_scan/embeddings.py`。

最新の検証記録：[ubuntu-26.04-python.md](ubuntu-26.04-python.md)にPython 3.14.4・179テスト・公開8原本の結果を記載する。改善適用時の記録は[ingestion-improvements-validation.md](ingestion-improvements-validation.md)。


## 検索API

- 実装：`src/search/`。取り込みv3のmetadataと分離した検索v1を管理する。
- 操作：[検索APIの操作手順](search-api.md)。`file-search`から公開対象・採用版を明示管理する。
- API：`/search-with-status`、Dify互換`/retrieval`、原本取得`/sources/{publication_id}`。
- 動的な契約：実行中APIの`/openapi.json`。本文を含まない検索監視は`search_events`へ保存する。
- 検証：`tests/test_search.py`。取り込みへの影響、公開・更新・出典、HTTP契約、予算、失敗・切断を検証する。
