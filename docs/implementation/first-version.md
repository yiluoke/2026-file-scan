# 文書変換・DB下書き登録 第1版

更新日：2026-10-25。v3の世代選択・キャッシュ・制限値・移行は[取り込み改善設計](../design/05-ingestion-improvements.md)を参照。

## 1. この版で確認すること

第1版は、指定フォルダから文書を収集し、検索基盤へ渡す前の下書きを作るバッチCLIである。実ファイルを使って、全シート・ページを処理できるか、原本位置へ戻れるか、表や見出しが保持されるか、取り込めない箇所を識別できるかを確認する。

処理は次の順序で進む。

```text
設定検査 → ファイル棚卸し → 原本コピーとSHA-256
         → 形式別抽出 → チャンク生成 → DBへ文書単位で確定
         → 品質レポートと確認用ファイルの出力
```

文書の公開状態は`draft`のみ。抽出成功の`succeeded`は、公開承認や内容の正しさの保証を意味しない。追加した`embed`コマンドはOpenAI Embeddings APIとpgvector保存に対応する。全文・ベクトル検索API、Dify、LLM回答、利用者別ACL、公開レビューの画面は未実装。

## 2. 導入と設定

Python 3.14系で、リポジトリのルートから実行する。以下はmacOS／Linuxの例である。

```sh
python -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements/ubuntu-26.04.lock
python -m pip install --no-deps --no-build-isolation -e '.[dev,search]'
cp config.example.yaml config.local.yaml
```

Windows PowerShellでは有効化コマンドを`.venv\Scripts\Activate.ps1`に置き換え、設定ファイルをコピーする。Windows実機での動作は未検証。

[`config.example.yaml`](../../config.example.yaml)と[`config.postgresql.example.yaml`](../../config.postgresql.example.yaml)は、どちらもローカルPostgreSQL＋pgvector用である。[ローカルDBの導入手順](local-postgresql.md)に従って先にDBを起動する。実際の収集先と接続情報を含む設定は、Git対象外の`config.local.yaml`等で管理する。

標準設定例は、`python scripts/download_samples.py`で公開サンプルを配置する`data/file-server/department/`を仮想ファイルサーバーとして使う。DBはローカルPostgreSQLの`file_scan`、実体は`artifacts/storage`、確認ファイルは`artifacts/reports`に作る。取込対象はDOCX・XLSX・XLSM・PDF・TXT・Markdownに限定し、プログラム・SQL・JSON・YAML・YML・CSV・LOGは対象にしない。対象外の拡張子を`include_extensions`に追加した設定はエラーになる。

| 設定 | 意味と初期値 |
| --- | --- |
| `sources` | 収集元の一覧。`source_id`、`root`、`recursive`、除外条件を指定 |
| `knowledge_id` | ナレッジの識別子。同じIDの`system_id`・`environment`は後から変更しない |
| `database_url` | DB接続文字列。既定はローカルPostgreSQL。SQLiteは単体テスト等で明示指定可能 |
| `database_url_env` | 接続文字列を読む環境変数名。標準は`FILE_SCAN_DATABASE_URL` |
| `storage_dir` | 原本コピー・画像の保管先。1個のDBに対して固定 |
| `report_dir` | 実行レポートと世代別確認ファイルの出力先 |
| `include_extensions` | 収集する拡張子。対応形式に限って指定する |
| `include_hidden` | 既定は`false`。隠しファイル・フォルダ、Excelの非表示部分の取り込みも制御 |
| `pdf_backend` | `docling`が既定。`native`を明示すると軽量なpypdf本文抽出 |
| `docling_artifacts_path` | 事前取得したDoclingモデルの配置先。既定`data/models/docling` |
| `chunk_max_characters` | チャンク本文の文字数上限。既定`1800`、最小`100` |
| `chunk_max_tokens` / `chunk_context_max_tokens` | 検索入力全体と検索文脈のトークン上限。既定`8191` / `1024` |
| `parse_workers` / `parse_worker_max_tasks` / `parse_worker_max_memory_mb` | 再利用する解析プロセスの上限、交換タスク数、処理後ピークメモリ閾値。既定`1` / `100` / `1536` |
| `max_file_bytes` | 原本サイズ上限。既定`104857600`、100 MiB |
| `copy_retries` | コピー中に原本が変わった場合の再試行回数。既定`2` |
| `parse_timeout_seconds` | OCR無効PDF・他形式の抽出時間上限。既定`120`秒 |
| `pdf_ocr` | PDF日本語OCR。既定enabled=true、mode=auto、scale=3.0、timeout_seconds=1800。言語・CPUエンジン・smallモデルは固定 |

相対パスは設定ファイルのディレクトリを基準に解決する。SQLiteの相対保存先も同じ基準となる。マウント済みのファイルサーバーを`root`に指定できる。UNCパスはWindowsでのみ扱い、macOS／Linuxではマウント先のパスを使う。

`source_id`は継続して同じ収集元を表す値にする。文書IDは`source_id`とルート相対パスから作るため、変更・移動・名称変更は別文書として扱われる。安定IDによる移動追跡は第1版では行わない。

`exclude_directories`はルート相対のディレクトリパスで指定し、globではない。`exclude_filenames`はパスを含まないファイル名globで指定する。収集ルート同士の重複・親子関係、および収集ルート内の`storage_dir`・`report_dir`は拒否する。シンボリックリンクやジャンクションをたどる収集も行わない。

## 3. 実行手順

### 3.1 棚卸し

```sh
file-scan scan --config config.local.yaml
```

収集対象、除外理由、列挙失敗を`report_dir/scan-<実行ID>.json`へ出す。原本コピー・DB接続は行わない。収集元に到達できなければ、空の正常結果にせずエラーを記録する。

### 3.2 DB初期化

```sh
file-scan init-db --config config.local.yaml
```

空の管理領域へスキーマv3を作成する。既存v1/v2には選択世代・抽出キャッシュ・Embeddingキャッシュを追加し、v1にはEmbedding用の2表も追加して既存データを保持する。v3適用済みの場合はそのまま利用する。移行後はingestで選択中世代を確定してからembedする。未管理の同名表、未知のスキーマ版、移行記録と実表の不一致は自動修復しない。既存データの削除や検索公開は行わない。

ローカル検証は本番と同じPostgreSQL＋pgvectorで行う。SQLite対応は単体テスト等のために残している。

### 3.3 抽出と下書き登録

```sh
file-scan ingest --config config.local.yaml
```

棚卸しを実行し直し、原本の作業コピーを保管してから抽出する。コピー前後の情報を比較して更新中の原本を検知し、SHA-256で内容を識別する。原本を実行・上書きしない。

DBの排他制御で同時登録を制限し、コピーや抽出の間に長時間のDB更新トランザクションを持たない。ブロック・チャンク・原本への対応は文書単位のトランザクションで登録する。ある文書が失敗しても、他文書の処理を続行して結果を記録する。

抽出は子プロセスで行い、時間上限超過は失敗として記録する。前回中断したジョブは次回登録時に失敗として回収し、新しい試行を作る。

### 3.4 結果確認

```sh
file-scan status --config config.local.yaml
```

現在の文書数・選択世代の状態を`documents`・`generations`へ、過去世代を`historical_generations`へ分けて表示する。プロファイル別の現在のEmbedding済み・未処理チャンク数は`embeddings`へ出す。確認が必要な文書は実行レポートと世代別の確認ファイルから調べる。

## 4. 保存されるもの

### 4.1 DBと実体の分離

DBには文書台帳、原本の版、変換設定、抽出ブロック、チャンク、原本位置、関係、画像の出現位置、処理ジョブ等を保存する。原本・画像のバイト列はDBへ格納せず、`storage_dir`の内容ハッシュ単位のファイルを参照する。

```text
storage_dir/
  objects/sha256/<ハッシュ先頭2文字>/<ハッシュ全体>

report_dir/
  scan-<実行ID>.json
  ingest-<実行ID>.json
  generations/<世代ハッシュ>/
    manifest.json
    document.json
    chunks.jsonl
    review.md
```

ハッシュ保管された実体のファイル名には、元の拡張子が付かない。元の形式、MIME、原本URI、原本位置はDB等のメタデータで対応付ける。

初回の`ingest`で、DBに一つの正規化済み`storage_dir`を結び付ける。同じDBで別の保管先を設定すると拒否する。移行にはDB参照と実体を整合させる別途の手順が必要であり、設定だけでの移行には対応しない。バックアップ・復旧はDBと実体保管先を一組として扱う。

### 4.2 確認用ファイル

| ファイル | 用途 |
| --- | --- |
| `scan-*.json` | 発見した原本、除外、列挙エラーを確認 |
| `ingest-*.json` | 実行全体と文書別の処理状態、警告、画像待ち、失敗区分を確認 |
| `manifest.json` | 対象世代と、その文書の登録結果を確認 |
| `document.json` | ブロック、関係、画像の出現位置を確認 |
| `chunks.jsonl` | 1行1チャンクで本文・検索用テキスト・ブロックへの対応を確認 |
| `review.md` | 原本位置と抽出本文を人が見比べる |

確認ファイルは確定したDBレコードから作る。これらを書き換えてもDBへは反映されない。通常の再実行でも確認ファイルを再生成する。DB登録後の出力失敗は`export_error`として区別し、DB登録成功を抽出失敗で上書きしない。

DBの抽出本文、レポート、確認用Markdownも社内情報を含む。原本・画像と同じく会社が承認した保管場所で管理する。

## 5. 形式別の実装範囲

| 形式 | 抽出内容・原本位置 | 主な制限 |
| --- | --- | --- |
| TXT・MD | UTF-8／CP932、段落、MDの見出し、行位置 | 対応文字コードとして読めなければ停止。NUL入りデータは拒否 |
| XLSX・XLSM | 全シート棚卸し、行・セル、結合、見出し候補、数式、保存済み結果、画像アンカー | 完全な表示形式・任意帳票の意味解析は行わない。複雑な数式、図形・グラフ、VBA等は警告 |
| DOCX | OOXMLの本文順、見出し・継承、表、目次リンクと本文の対応、画像、段落・表位置 | 表示ページ・浮動図形のレイアウト再現は行わない。自動番号、変更履歴、未対応の補助本文等を警告 |
| PDF Docling（標準） | 実モデルのレイアウト・読み順・本文・表構造・bbox、図の領域画像 | CPU固定・日本語RapidOCRあり。OCRを含むモデル事前配置が必要。図の意味解析は後続 |
| PDF native | pypdfの文字レイヤー、ページ位置、画像 | OCRなし。段組み・表・図の完全復元や正確な矩形座標は未対応 |

Excelの数式文字列と保存済み計算結果は別に保持し、再計算やマクロ実行は行わない。保存済み結果がない場合は空欄と区別する。非表示部分は既定で本文へ取り込まないが、対象外であることを棚卸し情報に残す。

Wordは目次を本文リンク先と照合する。目次の古い項目名や未解決リンクを記録し、目次に合わせて本文を創作しない。変更履歴の削除文字を通常本文へ混ぜず、未確定の変更があることを警告する。原本位置はDOCXの世代内の段落・表等であり、表示環境で変わるページ番号とは異なる。

PDFの画像領域は日本語RapidOCRで文字を読み、本文・表・位置として保存する。OCR後も文字が取れないページは`pdf_page_without_text_after_ocr`を付けて部分抽出にする。全面スキャン背景のOCR完了と図の意味説明待ちは区別する。Office画像のOCR・図の説明は後続機能である。

Officeパッケージの展開後容量512 MiB、XML部品128 MiB、部品数20,000、シート内実在セル250,000を内部上限とする。書式だけで膨らんだ使用範囲を全面走査せず、上限超過を無言で切り捨てない。これらの内部上限は現時点ではYAMLから変更しない。

## 6. 分割と品質の扱い

文書の親・節・内容の由来が同じ要素だけをまとめ、改行や空白を優先して分割する。チャンクには本文の文字範囲と原本ブロックの文字範囲を保持する。見出しパスやExcelの列見出し等を加えた`search_text`は、根拠となる`content_text`と分ける。

`chunk_max_characters: 1800`はUnicode文字数による本文上限。加えて、見出し・列見出しを含む検索入力全体を`chunk_max_tokens: 8191`以下に分割し、cl100k_baseの実測値を`token_count`へ保存する。Doclingの表は行境界を優先し、検索用文脈へ列見出しを引き継ぐ。文脈は`chunk_context_max_tokens: 1024`以内とし、長大な場合の省略は品質フラグで記録する。

原本位置・親子関係・チャンク本文と原文の対応を保存前に検査する。文書に品質警告や解析待ち画像がある場合は`processing_status=partial`とし、正常抽出の`succeeded`と区別する。

## 7. 再実行・更新・削除

原本の内容ハッシュと形式別の変換設定・実装・依存バージョンから変換世代を識別する。同じ入力と同じ条件なら既存世代を再利用し、選択中世代を明示する。抽出キャッシュは分割設定から独立させ、分割だけの変更では再抽出しない。既存実体の再利用時にも整合を検査する。変更のない原本は完全ハッシュ照合を維持し、一時コピーだけを省略する。

既存の部分抽出も通常は再利用する。同じ条件で抽出を再試行する場合に限り、次を使う。

```sh
file-scan ingest --config config.local.yaml --retry-partial
```

`--retry-partial`はOCR設定を変更する機能ではない。PDFのOCRは`pdf_ocr`で制御する。同条件で未処理の図やOffice画像は再度部分抽出になる。

原本変更や変換条件の変更は別の世代として保存する。完全に列挙できた収集元から消えた文書は論理的な削除状態を記録し、通信・列挙障害を削除と誤認しない。設定による除外も原本削除と区別する。第1版は物理ファイルや履歴を自動削除しない。

## 8. PostgreSQL接続とDocling

### 8.1 PostgreSQL

[ローカルPostgreSQL＋pgvectorの導入手順](local-postgresql.md)を参照する。Postgres.appまたはComposeでPostgreSQL 18とpgvector 0.8.6を用意し、アプリ用の`file_scan`とテスト用の`file_scan_test`を分ける。DB起動・拡張有効化後、CLIから`init-db`を実行してから`ingest`を行う。

`FILE_SCAN_DATABASE_URL`が設定されていれば、YAMLの`database_url`より優先する。独自の`database_url_env`も指定できるが、標準の`FILE_SCAN_DATABASE_URL`が両者より優先する。Python CLIは`.env`ファイルを自動読込しないため、シェルまたは実行環境の仕組みで環境変数を渡す。

第1版の`init-db`は登録用スキーマを作成する。vector拡張の導入・有効化はDBの構築時に行う。OpenAI Embeddingの設定・実行は[専用手順](openai-embedding.md)を参照する。検索API・PGroongaは未実装。

### 8.2 Doclingによる標準PDF抽出

Doclingは通常の依存に含め、`pdf_backend: docling`を標準とする。初回に`python scripts/download_docling_models.py --config config.local.yaml`でレイアウト・表構造の実モデルを取得する。配置先は`docling_artifacts_path`で指定する。

本文・見出し・表・bboxをDoclingから抽出する。原画像の保存にはpypdfも使用するが、本文の代替には使わない。CPU固定・日本語RapidOCR有効・外部サービス無効で実行する。OCR不足・図の意味解析待ちは部分抽出として記録する。公開PDF2件の実モデル検証は[Doclingの手順](docling-pdf.md)を参照。

## 9. 終了コードと確認手順

| 終了コード | 意味 | 確認すること |
| --- | --- | --- |
| `0` | 実行が正常に完了 | 棚卸し件数、確認用ファイル、原本との対応 |
| `1` | 設定・DB接続等の全体エラー | 設定ファイル、DB初期化、接続先、保管先 |
| `2` | 部分抽出、個別失敗、列挙エラー、確認ファイル出力失敗等あり | 実行レポートの`quality_flags`、`pending_images`、`error_code`、`export_error` |

対話的な中断では`130`を返す。終了コード2でも、問題のない文書はDBへ登録されている場合がある。`ingest`の`registered`には部分抽出の登録も含み、`partial`は別の品質区分なので単純に足し合わせない。

原本の例外全文やDB接続文字列を通常の端末へ出さず、区分を通知する。エラー区分だけで原因が特定できない場合は、社内の検証用コピーを使って抽出結果と実装を調べる。

## 10. コードと検証

処理目的を説明する日本語docstring・コメントを起点に読む。モジュールを追う順序は[READMEの一覧](../../README.md#コードを読む順序)を参照する。個々のデータがどの段階で作られるかは、`domain.py`と`pipeline.py`を並べて確認すると分かりやすい。

```sh
set -a
source .env
set +a
python scripts/test_postgres.py
python -m ruff check src tests scripts
```

`FILE_SCAN_TEST_DATABASE_URL`にテスト専用DBを指定する。補助スクリプトはインストール済みPostgreSQLとpgvectorを確認し、ベクトル演算・HNSW索引と既存のテストを実行する。取り込みテストはランダムなschemaを作成・削除するため、本番接続先は使わない。pgserverによる一時PostgreSQLの起動は行わない。通常の`python -m pytest`では、テスト用接続先が未指定なら実接続テストをスキップする。

テストは小さな原本を生成し、複数シート、数式と保存済み結果、Wordの順序・目次・画像、プログラム・設定・ログ形式を取り込まないこと、画像PDFを成功扱いしないこと、再実行、DBの制約等を検証する。公開8原本・実Doclingモデル・実PostgreSQLは自動検証する。社内実ファイル、Windows実機、実ファイルサーバー、本物のOpenAI APIキーによる品質・料金の受入検証は別途行う。

原本、抽出本文、画像、ログ、確認用ファイル、ローカルDB、接続情報はGitへ登録しない。`.gitignore`で`data/`、`artifacts/`、`logs/`、`var/`、`config.local.yaml`、`.env`等を除外する。任意の別パスをリポジトリ内へ追加する場合は、そのパスも除外する。
