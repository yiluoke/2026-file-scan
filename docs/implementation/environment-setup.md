# 別環境での環境構築手順

更新日：2026-11-02。現在のファイル取り込みCLIとDBスキーマv3を、新しい環境へ導入するための手順。Python・ライブラリ・DB・テーブル・モデル・設定を準備し、公開サンプルの取り込みまで確認する。

**本番導入先はWindowsホスト上のUbuntu 26.04 VM。** この環境では[Ubuntu 26.04 VM向け手順](ubuntu-26.04-vm.md)を先に参照する。標準Python 3.14の仮想環境と依存版の固定、拡張の版、SMBのマウント、Linuxサービスの起動方法を具体化している。以下のWindows PowerShell例は、Windowsへ直接導入する場合の参考である。

すべてのコマンドは、配布したプロジェクトのルート（`pyproject.toml`と`compose.yaml`がある場所）で実行する。OS固有の箇所以外は、仮想環境を有効にした後の`python`・`file-scan`を使用する。

2026-11-02更新：アプリテーブルの配置先は専用スキーマ`winnap`。pgvectorはpublicに配置する。既存public配置を継続使用する場合は[スキーマ移行](winnap-schema.md)を先に行う。

## 1. 導入構成と準備するもの

| 項目 | 使用するもの・確認事項 |
| --- | --- |
| Python | Ubuntu 26.04標準の3.14系。確認版3.14.4。アプリは3.14以上3.15未満を許可する |
| OS・CPU | 実検証済みはmacOS／arm64。Linux・Windows向けの操作例も以下に記載するが、そのOS実機での受入確認は別途行う |
| 本番の実行OS | Windowsホスト上のUbuntu 26.04 VM。アプリ・Python・DBはUbuntu内へ配置し、SMB共有はLinuxへマウントして使う |
| DB | PostgreSQL 18.6＋pgvector 0.8.6で検証。Docker Composeで用意する方法と、既存DBを利用する方法のどちらかを選ぶ |
| DBクライアント | 既存DBを使う場合は`psql`。Docker方式ではコンテナ内の`psql`を使えるため、ホスト側の導入は不要 |
| 検索用の追加拡張 | 取り込みにはPostgreSQL＋pgvectorを使う。追加されたPGroonga検証も含めて全テストを行う場合は、PGroongaもサーバーへ導入する（10節参照） |
| モデル実行 | CPUでDoclingを実行する。GPU・CUDAの準備は必須ではない |
| メモリ・ディスク | 公開サンプル処理では解析ワーカーだけで約1.25 GiBのピークを観測。OS・DB・Python分にも余裕を持たせる。依存パッケージ、モデル、原本コピー、世代履歴の保存容量を別に確保する |
| ネットワーク | 初回はPythonパッケージ配布元、Dockerレジストリ（Docker方式）、Hugging Faceのモデル配布元、トークナイザー辞書配布元へ接続。公開サンプルはその提供元へ接続する |
| OpenAI API | Embeddingを実行する場合だけ、有効なAPIキーとAPIへの接続が必要。初回の抽出・DB登録確認はEmbedding無効のまま行える |

Pythonは[公式配布](https://www.python.org/downloads/)または導入先で承認された配布方法で用意する。仮想環境は移設先で作り直す。[Python venvの公式説明](https://docs.python.org/3.14/library/venv.html)

### 配布するファイル

プロジェクトのソース一式を配布する。特に次が必要。

- `pyproject.toml`、`.python-version`、`requirements/`、`src/file_scan/`、`README.md`。
- `scripts/`、`sql/postgresql/`、`config.example.yaml`、`.env.example`。
- 検証する場合は`tests/`、`examples/`、`docs/`。
- `src/file_scan/docling-models.lock.json`と`rapidocr-models.lock.json`も必須。モデルの固定版・期待ハッシュを含む。

移設元の`.venv`・`.env`・`config.local.yaml`・PostgreSQLのデータディレクトリを、新規導入手順の入力として流用しない。設定と認証情報は新環境の値で作る。既存文書・DBのデータ移行は、この新規構築手順とは別作業とする。

## 2. Python仮想環境とライブラリの導入

Ubuntuでは通常OpenCVのロード用に`sudo apt install -y libgl1 libglib2.0-0t64`も実行する。既存headless構成からの更新は[OCR更新手順](rapidocr-pdf.md)で両OpenCV配布を削除してから入れ直す。

### macOS／Linux

Python 3.14のコマンド名が`python3.14`である例。

```sh
python3.14 --version
python3.14 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements/ubuntu-26.04.lock
python -m pip install --no-deps --no-build-isolation '.[dev,search]'
python scripts/check_python_environment.py
python -m pip check
file-scan --version
```

### Windows PowerShell

Python Launcherで3.14を選ぶ例。

```powershell
py -3.14 --version
py -3.14 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements/ubuntu-26.04.lock
python -m pip install --no-deps --no-build-isolation '.[dev,search]'
python scripts/check_python_environment.py
python -m pip check
file-scan --version
```

組織のポリシー等でActivate.ps1を実行できない場合は、仮想環境内の実行ファイルを直接指定する。以後のコマンドも同様に読み替える。

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements/ubuntu-26.04.lock
.\.venv\Scripts\python.exe -m pip install --no-deps --no-build-isolation '.[dev,search]'
.\.venv\Scripts\python.exe scripts/check_python_environment.py
.\.venv\Scripts\python.exe -m pip check
.\.venv\Scripts\file-scan.exe --version
```

固定ファイルは実行依存と検証・検索用extraの依存を含む。先に全依存を導入してから、アプリを`--no-deps --no-build-isolation`で導入する。開発時は後者に`-e`を追加する。版の更新はlockと対応表を一緒に更新して検証する。OSが管理するPythonへのpip導入は行わず、仮想環境を使う。

### 必須ライブラリの一覧

正本は[pyproject.toml](../../pyproject.toml)。個別に最新版を選ぶ代わりに、上記コマンドで指定版をまとめて導入する。

| Pythonパッケージ | 指定版 | 用途 |
| --- | --- | --- |
| `SQLAlchemy` | 2.0.45 | DBテーブル定義・SQL・トランザクション |
| `psycopg[binary]` | 3.3.2 | PostgreSQL接続。binary extraも導入する |
| `PyYAML` | 6.0.3 | YAML設定の読み込み |
| `openpyxl` | 3.1.5 | Excelのセル・数式等の読み込み |
| `python-docx` | 1.2.0 | Word関連の処理・テスト。本文順序等はOOXML処理も併用 |
| `pypdf` | 6.9.2 | PDFの原画像取得、明示したnative経路の文字抽出 |
| `charset-normalizer` | 3.4.4 | テキストの文字コード判定 |
| `Pillow` | 12.1.1 | 画像の保存・加工 |
| `openai` | 2.21.0 | OpenAI Embeddings APIのSDK |
| `pgvector` | 0.5.1 | Python／SQLAlchemy側のvector型対応 |
| `tiktoken` | 0.12.0 | Embedding入力のトークン計測 |
| `docling-slim` | 2.140.0 | 公式DoclingのPDF・CPUモデル用extraを使用。本文・レイアウト・表構造抽出 |
| `opencv-python` | 4.10.0.84 | TableFormer・RapidOCRの画像処理。headless版は併用しない |
| `rapidocr` / `onnxruntime` | 3.9.1 / 1.24.1 | PDFの日本語OCR・CPU推論 |

検証用extraは`pytest==9.0.2`、`ruff==0.17.1`、`reportlab==4.4.10`、`httpx==0.28.1`。ReportLabはPDFテスト原本の生成に使う。

Doclingから`torch`、`transformers`、`huggingface-hub`、`docling-core`、`docling-parse`、`docling-ibm-models`等も依存として解決される。これらを別々に手動で追加する必要はない。wheelが提供されないOS・CPU・Python版では追加のビルド環境が必要になるため、その場合は導入先に合う依存構成を確認する。

Pythonの`pgvector==0.5.1`と、DBサーバー側拡張の`pgvector 0.8.6`は別物。両方が必要であり、pipだけではDBサーバーに拡張は入らない。

直接依存は`pyproject.toml`、間接依存と構築ツールは[requirements/ubuntu-26.04.lock](../../requirements/ubuntu-26.04.lock)で固定する。Ubuntuに対応パッケージのないDocling等は追加依存、標準版と依存条件が合わない5件は明示的な互換例外として管理する。全対応表・例外理由・実検証は[Python版整合の記録](ubuntu-26.04-python.md)を参照。

## 3. 設定ファイルと認証情報

新規配置先で、まだ同名ファイルがない場合にコピーする。

macOS／Linux：

```sh
cp config.example.yaml config.local.yaml
cp .env.example .env
chmod 600 .env
```

PowerShell：

```powershell
Copy-Item config.example.yaml config.local.yaml
Copy-Item .env.example .env
```

`.env`を編集する。下の`PASSWORD`は実際の同一パスワードに置き換える。Docker方式ではその値を`POSTGRES_PASSWORD`にも設定する。接続URLの扱いを単純にするため、初期構築では十分な長さのランダムな英数字等を用いる。URLの予約文字を含むパスワードはURL側でパーセントエンコードする。

```dotenv
POSTGRES_PASSWORD=PASSWORD
FILE_SCAN_DATABASE_URL=postgresql+psycopg://file_scan:PASSWORD@127.0.0.1:5432/file_scan
FILE_SCAN_TEST_DATABASE_URL=postgresql+psycopg://file_scan:PASSWORD@127.0.0.1:5432/file_scan_test
OPENAI_API_KEY=sk-placeholder-replace-me
TIKTOKEN_CACHE_DIR=data/tokenizer-cache
```

既存DBを使う場合はDBユーザー・ホスト・ポート・DB名を置き換える。環境変数のDB接続先はYAMLの`database_url`より優先される。APIキーも環境変数をYAMLの`embedding.api_key`より優先する。

Python CLIは`.env`を自動読込しない。新しいターミナルでも次を実行する。

macOS／Linux：

```sh
source .venv/bin/activate
set -a
source .env
set +a
```

PowerShell（上のような、引用符を付けない単純な`名前=値`の.env用）：

```powershell
.\.venv\Scripts\Activate.ps1
Get-Content -Encoding UTF8 .env | ForEach-Object {
    if ($_ -match '^([A-Za-z_][A-Za-z0-9_]*)=(.*)$') {
        [Environment]::SetEnvironmentVariable($Matches[1], $Matches[2], 'Process')
    }
}
```

Docker Composeはルートの`.env`を自動読込する。Composeが起動しただけでは、ホストのPythonへ環境変数を渡したことにはならない。

## 4. PostgreSQL＋pgvectorの準備

以下のA・Bのどちらかを選ぶ。MacでPostgres.appを新規構築する場合は、既存の[ネイティブDB導入手順](local-postgresql.md)も参照できる。同じポートで複数のDB方式を同時起動しない。

### A. Docker Composeで新規DBを用意する

Docker Engine＋Composeプラグイン、またはDocker Desktopを用意する。[Docker Composeの公式導入案内](https://docs.docker.com/compose/install/)

```sh
docker --version
docker compose version
docker compose config -q
docker compose up -d --wait db
docker compose ps
docker compose exec -T db psql -U file_scan -d file_scan -c "SHOW server_version;"
docker compose exec -T db psql -U file_scan -d file_scan -c "SELECT extversion FROM pg_extension WHERE extname = 'vector';"
```

同梱の[compose.yaml](../../compose.yaml)は`pgvector/pgvector:0.8.6-pg18-trixie`を使う。ホストの接続先は`127.0.0.1:5432`。新しいボリュームでは[scripts/init-postgres.sql](../../scripts/init-postgres.sql)が次を準備する。

- `file_scan`のvector拡張。
- 検証専用の`file_scan_test`とそのvector拡張。

この初期化スクリプトは20個のアプリテーブルを作らない。次節で作成する。初期化スクリプトは既存ボリュームには再適用されない。既存ボリュームでDB・拡張が不足する場合は[既存ボリュームの手順](local-postgresql.md)で確認する。

このComposeはローカル／受入確認用のDB構成で、`file_scan`は初期管理ユーザーになる。既存の業務DBを使う場合は、Bのように管理者がDBを準備してアプリ用ユーザーで接続する。

### B. 既存または別サーバーのPostgreSQLを使う

OSへPostgreSQLを直接新規導入する場合は[PostgreSQL公式ダウンロード](https://www.postgresql.org/download/)から対象OS用の18系を準備する。そのうえでDB管理者がサーバー側へpgvectorを導入する。配布パッケージやビルド方法は[pgvector公式の導入方法](https://github.com/pgvector/pgvector#installation)を参照する。拡張の配置先は`public`とする。

新規DBを作る場合の管理者操作例。`psql`で管理DBに接続してから実行する。ユーザー・DBが既存の場合は作成部分を省略する。

```sh
psql -h DB_HOST -p 5432 -U DBA_USER -d postgres
```

```sql
CREATE ROLE file_scan LOGIN;
\password file_scan
CREATE DATABASE file_scan OWNER file_scan;
CREATE DATABASE file_scan_test OWNER file_scan;
\connect file_scan
CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public;
\connect file_scan_test
CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public;
\quit
```

`\password`・`\connect`はpsqlのコマンド。`CREATE DATABASE`はトランザクションに入れず実行する。アプリユーザーは各DBの所有者とする例で、通常の取り込みにDB管理者権限は不要。テストには専用DB内でのschema作成権限が必要になる。社内DBの場合は認証方式・TLS・到達先を管理者の指定へ合わせる。

## 5. 20テーブルの作成

### 新規導入：配布SQLを実行する

作成するSQLは[sql/postgresql/schema-v3.sql](../../sql/postgresql/schema-v3.sql)。20テーブル、PK・UNIQUE・CHECK・外部キー、12索引、vector有効化、スキーマ版3の登録を含む。

Docker方式では次の2コマンドを使う。macOS・Linux・PowerShellで共通。

```sh
docker compose cp sql/postgresql/schema-v3.sql db:/tmp/file-scan-schema-v3.sql
docker compose exec -T db psql -U file_scan -d file_scan -v ON_ERROR_STOP=1 -f /tmp/file-scan-schema-v3.sql
```

既存／外部サーバー方式では、ホストのpsqlから実行する。

```sh
psql -h DB_HOST -p 5432 -U file_scan -d file_scan -v ON_ERROR_STOP=1 -f sql/postgresql/schema-v3.sql
```

作成先は専用スキーマ`winnap`。同名の管理テーブルがある場合は停止する。複数文を最後まで実行し、`COMMIT`できたことを確認する。既存v1/v2を更新する場合は新規SQLを再実行せず、次のコマンドを使う。

```sh
file-scan init-db --config config.local.yaml
```

テーブル作成もPythonへ任せる新規導入方式を選ぶ場合は、vector有効化後に上記`init-db`だけを実行しても同じv3を作成できる。SQL導入後の`init-db`は適用済み確認になる。方式の詳細は[SQL導入手順](../../sql/postgresql/README.md)を参照する。

テスト専用DBへアプリテーブルを先に作る必要はない。自動テストがそのDB内の一時schemaへ作成する。

### 作成結果の確認

Dockerの例。外部DBの場合は同じSQLをホストのpsqlで実行する。

```sh
docker compose exec -T db psql -U file_scan -d file_scan -c "SELECT version FROM winnap.schema_migrations;"
docker compose exec -T db psql -U file_scan -d file_scan -c "SELECT count(*) FROM information_schema.tables WHERE table_schema = 'winnap' AND table_type = 'BASE TABLE';"
file-scan status --config config.local.yaml
```

空の専用DBならスキーマ版`3`、テーブル数`20`、文書数`0`になる。直接SQLで業務行をINSERTする場合、現行の多くの初期値・時刻はアプリ側が補完する設計であることに注意する。初回取り込みはCLIで行う。

## 6. Doclingモデルとトークナイザー辞書

日本語スキャン書籍向けにRapidOCR＋ONNX Runtime CPUを採用した。以下は仮想環境を有効にしたPythonで実行する。ライブラリは`.venv`内、モデルの実体はプロジェクトの`data/models/docling/`内へ保存する。OCRは画像から文字を読む処理、トークナイザー辞書は抽出済み文章のトークン数を数えるもので、役割が異なる。

ライブラリ導入だけではPDF用のモデル実体は揃わない。ネットワーク接続できる環境で次を実行する。

```sh
python scripts/download_docling_models.py --config config.local.yaml
python scripts/download_docling_models.py --config config.local.yaml --verify-only
python -c "from file_scan.tokens import count_tokens; print(count_tokens('日本語の初期確認'))"
```

- 配置先は既定`data/models/docling/`。レイアウトモデル、TableFormer accurateと、日本語対応RapidOCRの検出・方向分類・文字認識のONNXモデル3点（合計約31.7 MB）を取得する。認識用文字集合は認識ONNX内に含まれる。`pdf_ocr.enabled: false`の場合はOCRモデルを取得・検査しない。
- 通常の取得は同梱lockの固定コミットを使う。導入作業で`--update-lock`を付ける必要はない。
- `model-manifest.json`と`ocr-model-manifest.json`について版・ファイルサイズ・ハッシュを検査する。モデル不足や改変時に、取込処理が自動でネット取得することはない。
- 辞書は設定した`TIKTOKEN_CACHE_DIR`へ準備する。上記の相対パスはプロジェクトルートで使う前提。定期実行等で作業場所が変わる場合は絶対パスで指定する。

PDF処理はCPU・日本語OCR有効・外部サービス無効で実行する。既定`pdf_ocr.mode: auto`は必要領域をDoclingが選ぶ。不良な既存文字層は自動品質判定しないため、対象別の設定で`full_page`を指定して置き換える。OCR期限は`pdf_ocr.timeout_seconds: 1800`。モデル・設定変更後は通常の`ingest`で新世代を作る。オフライン移設・旧環境更新を含む詳細は[日本語OCRの手順](rapidocr-pdf.md)を参照。

## 7. 収集元の準備と設定

最初に公開サンプルで確認する場合：

```sh
python scripts/download_samples.py
file-scan scan --config config.local.yaml
```

既定の仮想ファイルサーバー`data/file-server/department/`へTXT・Word・Excel・PDF各2件を配置する。取得元・SHA-256は[examples/public-samples.json](../../examples/public-samples.json)。scanの期待値は対象8件・列挙エラー0。旧版・Officeロック等は除外され、OSの隠しファイルで除外件数は増えることがある。

実ファイルサーバーへ切り替える場合は、先にOSで共有フォルダを利用可能にし、実行ユーザーで読めることを確認する。`config.local.yaml`の例：

```yaml
sources:
  - source_id: department-share
    root: /mnt/company-share/department
    recursive: true
    exclude_directories: [旧版/]
    exclude_filenames: ['~$*', '*.tmp']
storage_dir: artifacts/storage
report_dir: artifacts/reports
pdf_backend: docling
docling_artifacts_path: data/models/docling
```

Macのマウント先なら`/Volumes/company-share/department`等、Windowsなら`'\\server\share\department'`のようなUNCパスに置き換える。UNCはWindowsでのみ使用できる。サービス実行時は、その実行アカウントにも共有へのアクセスが必要。原本を読む権限と、保管先・レポート先へ書く権限を用意する。

`storage_dir`・`report_dir`は収集元の外に置く。パスの相対指定はYAMLの場所が基準。原本経路のシンボリックリンク等は拒否する。`source_id`を変えると論理文書IDも変わるため、同じ収集元の識別子は継続して使う。

## 8. 取り込みと再実行の確認

```sh
file-scan ingest --config config.local.yaml
file-scan status --config config.local.yaml
file-scan ingest --config config.local.yaml
```

公開8サンプルの新規導入時の期待値：

- 初回：`registered=8`、`failed=0`、`partial=0`。
- 2回目：`unchanged=8`。変更のない原本の一時書込みは0。
- status：現在の文書8・成功世代8。履歴世代は別集計。Embedding無効の間は未生成チャンクがpendingとして表示される。
- `artifacts/reports/ingest-*.json`と、世代別の`document.json`・`chunks.jsonl`・`review.md`で内容を確認できる。

一般の業務原本では画像解析待ち・文字なしページ等があるとpartialになり得る。終了コードは`0`が成功、`1`が設定・接続等の失敗、`2`が部分失敗・未処理あり。コード2のときはレポートで対象を確認する。

初回ingestでDBと`storage_dir`を対応付ける。その後の保管先変更は移行作業になるため、導入時に保存先を確定する。

## 9. OpenAI Embeddingを使う場合

抽出・登録まで確認できた後、実APIキーを`.env`の`OPENAI_API_KEY`へ設定する。環境変数を読み直し、`config.local.yaml`で次を設定する。

```yaml
embedding:
  enabled: true
  model: text-embedding-3-small
  dimensions: 1536
  api_key_env: OPENAI_API_KEY
  batch_size: 16
  read_batch_size: 128
  batch_max_tokens: 262112
  timeout_seconds: 60
```

```sh
file-scan embed --config config.local.yaml
file-scan status --config config.local.yaml
```

有効な設定では抽出した検索用テキストをAPIへ送信する。仮キーは設定エラー、`enabled: false`のままならdisabledで終了する。課金・利用権限は導入先のAPIアカウントで確認する。異なるモデル・次元のベクトルは別プロファイルになる。詳細は[OpenAI Embeddingの設定](openai-embedding.md)。

## 10. 自動テストと導入時の記録

`FILE_SCAN_TEST_DATABASE_URL`が`file_scan_test`等の専用DBを指していることを確認する。dev extra、公開サンプル、実Doclingモデルを準備して実行する。

### 取り込み環境の検証（PostgreSQL＋pgvector）

追加のPGroongaテストを除き、取り込み・DDLを確認する場合。CLIテストが明示する接続先をアプリ用環境変数で上書きしないよう、テストプロセスだけ`FILE_SCAN_DATABASE_URL`を外す。

macOS／Linux：

```sh
env -u FILE_SCAN_DATABASE_URL python -m pytest -q --ignore=tests/test_pgroonga.py
```

PowerShell：

```powershell
$applicationUrl = $env:FILE_SCAN_DATABASE_URL
try {
    Remove-Item Env:FILE_SCAN_DATABASE_URL -ErrorAction SilentlyContinue
    python -m pytest -q --ignore=tests/test_pgroonga.py
} finally {
    $env:FILE_SCAN_DATABASE_URL = $applicationUrl
}
```

共通の静的検査：

```sh
python -m ruff check src tests scripts
python scripts/export_postgres_schema.py --check
```

### PGroongaの追加検証も実行する場合

現在の`scripts/test_postgres.py`はvectorとpgroongaの両拡張を確認する。PGroonga未導入のDBではテスト開始前に停止する。取り込み向けComposeのpgvectorイメージだけで、PGroongaも使えるとは扱わない。

導入先のOS・PostgreSQL版に対応するサーバー側パッケージを[PGroonga公式手順](https://pgroonga.github.io/install/)で準備し、管理者がアプリ用・テスト用DBそれぞれに[scripts/init-pgroonga.sql](../../scripts/init-pgroonga.sql)を適用する。SQLの実行だけでサーバーのライブラリは導入されない。外部サーバーでの実行例：

```sh
psql -h DB_HOST -p 5432 -U DBA_USER -d file_scan -v ON_ERROR_STOP=1 -f scripts/init-pgroonga.sql
psql -h DB_HOST -p 5432 -U DBA_USER -d file_scan_test -v ON_ERROR_STOP=1 -f scripts/init-pgroonga.sql
python scripts/test_postgres.py
```

導入先でのPGroongaの版・同居動作も記録する。取り込み用の新規20テーブルSQLは、PGroongaのサーバーパッケージ導入や全文索引の作成を代行しない。

テストは専用DB内に一時schemaを作成・削除する。OpenAIのテストはHTTP応答を模擬し、実APIは呼ばない。実モデル・サンプル未配置のテストはスキップするため、件数だけでなくスキップ理由も確認する。

移設元では、取り込み改善時の全156テストと、その後のSQL配布・DB関連37テストが成功している。追加されたテストもあるため、導入先はその時点の配布ソースに含まれる全テストを実行して記録する。過去の件数を固定の合格条件にしない。[既存の検証記録](ingestion-improvements-validation.md)

実行環境の依存を保存する例：

```sh
python -m pip freeze > artifacts/reports/pip-freeze.txt
python --version
```

Python版、OS・CPU、ソースのコミット／配布版、PostgreSQL・vector版、モデルlock、テスト結果、設定ファイルの秘密情報を除く項目を合わせて記録する。APIの実疎通・料金・検索品質、社内の共有フォルダ・対象OSの受入確認は別途行う。

## 11. インターネットへ接続できない環境の場合

同じOS・CPU・Python 3.14の接続可能な準備環境で必要物を揃える。Python・DB・コンテナイメージ自体の配布も導入先の方法に合わせる。

```sh
python -m pip wheel --wheel-dir artifacts/wheelhouse -r requirements/ubuntu-26.04.lock
python -m pip wheel --no-deps --no-build-isolation --wheel-dir artifacts/wheelhouse .
```

ソース一式に加え、`artifacts/wheelhouse/`、`data/models/docling/`全体（manifest込み）、準備済み`data/tokenizer-cache/`を配布する。公開サンプルで確認する場合は`data/file-server/department/`も含める。wheelhouseはOS・CPU・Python版が違う環境へ流用せず、対象ごとに用意する。

導入先で新しい仮想環境を作成・有効化して実行する。wheelhouseはアプリ自身のwheelと、その依存のwheelを含めておく。

```sh
python -m pip install --no-index --find-links artifacts/wheelhouse -c requirements/ubuntu-26.04.lock 'file-scan[dev,search]==0.1.0'
python scripts/check_python_environment.py
python -m pip check
python scripts/download_docling_models.py --config config.local.yaml --verify-only
python -c "from file_scan.tokens import count_tokens; print(count_tokens('日本語の初期確認'))"
```

`TIKTOKEN_CACHE_DIR`はコピー先を指す値で読み込む。モデルの新規ダウンロードと公開サンプルの取得コマンドは省略する。Embeddingを実際に生成するには、閉域環境でもOpenAI APIへ接続できる経路が必要。

## 12. よくある停止箇所

| 症状 | 確認・対応 |
| --- | --- |
| `python`・`file-scan`が見つからない | Python 3.14と仮想環境の有効化を確認。Windowsは仮想環境内のexeを直接指定できる |
| pipでwheel不足・ビルド失敗 | Python版・OS・CPU・パッケージ取得元を確認。別環境の`.venv`を流用しない |
| DB接続・認証の失敗 | DB起動、ホスト・ポート、`.env`の読込、URLのパスワード、TLS設定を確認 |
| `vector`型・拡張がない | サーバー側のpgvectorパッケージと、接続先DBの`CREATE EXTENSION`を両方確認 |
| `test_postgres.py`がpgroonga不足で停止 | 10節の取り込み専用検証を選ぶか、サーバー側PGroongaと各DBの拡張を導入して全テストを行う |
| 同名テーブルでSQLが停止 | 作成済みDBへ新規SQLを再適用していないか確認。既存v1/v2は`init-db`で更新 |
| 未対応スキーマ版 | ソース配布版とDB版を確認。必要な初期化・移行を実施 |
| Doclingモデル不足・ハッシュ不一致 | lockに対応するモデルを配置し`--verify-only`で確認。配置記録だけを手修正しない |
| tokenizer辞書の取得失敗 | 接続可能環境で辞書を準備し、`TIKTOKEN_CACHE_DIR`と実行ユーザーの読込権限を確認 |
| 収集0件・列挙エラー | 共有のマウント・実行アカウント、パス、拡張子・除外設定をscanで確認 |
| storage_dirが初回と異なる | 最初に対応付けた保管先を確認。DB参照だけを変更せず、DBと実体の移行を計画する |
| Embeddingがdisabled／仮キーエラー | `embedding.enabled`、実キー、環境変数の再読込を確認 |

## 13. 再起動後の運用

新しいシェルでプロジェクトルートへ移動し、仮想環境と`.env`を読み込む。Docker方式は`docker compose up -d --wait db`、ネイティブDBはその環境のサービス起動方法を使う。その後、必要な`scan`・`ingest`・`embed`・`status`を実行する。

初期SQL、モデル取得、サンプル取得を毎回繰り返す必要はない。DBと`storage_dir`は組で保全する。Docker方式でデータを保持して停止する操作は`docker compose down`。DBボリューム削除を伴う操作は通常の停止手順に含めない。

## 14. この手順書の作成時に確認したこと

2026-11-02 日本語OCR追加後の最終確認：macOS arm64・Python 3.14.4、固定依存105件の不一致0件、pip check成功。PostgreSQL 18.6・pgvector 0.8.6・PGroonga 4.1.0で全232テスト成功、スキップ0件。依存ライブラリ由来の非推奨警告11件。日本語スキャン／文字層との混在、全ページOCR、不良文字層置換、白紙、図の待ち、オフライン実行、モデル改変検知、設定変更後の新世代登録と再利用を確認した。Ruff・配布wheelへの両モデルlock同梱も成功。実書籍の精度、Ubuntu VM上の性能、実OpenAI API接続は未検証。詳細は[日本語OCRの実装・運用](rapidocr-pdf.md)。 導入先では8～10節で受入確認を行う。
