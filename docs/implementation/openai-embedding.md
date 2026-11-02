# OpenAI Embeddingとローカルファイルサーバーの検証

日本語スキャンPDFはDocling内のRapidOCR＋ONNX Runtime CPUでローカルに文字化する。OCR自体にOpenAIキーは不要。OCR本文も既存のsearch_textを生成し、embed実行時にEmbedding APIへ送る。手順は[日本語OCR](rapidocr-pdf.md)を参照。

取り込みは設定したファイルサーバーのパスを再帰収集し、原本のコピー、本文抽出、チャンクの下書き登録まで行う。続いて`embed`コマンドが抽出済みの`search_text`をOpenAI Embeddings APIへ送り、ベクトルをPostgreSQLのpgvector型で保存する。

## 設定

`config.local.yaml`の設定例。実際のAPIキーはGit対象外のこのファイル、または`.env`で定義できる。同名の環境変数が設定されていれば`api_key`より優先する。

```yaml
embedding:
  enabled: false
  model: text-embedding-3-small
  dimensions: 1536
  api_key_env: OPENAI_API_KEY
  api_key: sk-placeholder-replace-me
  batch_size: 16
  read_batch_size: 128
  batch_max_tokens: 262112
  timeout_seconds: 60
```

`.env`を使う場合は次を実キーに置き換える。

```dotenv
OPENAI_API_KEY=sk-placeholder-replace-me
```

有効なキーを設定して`enabled: true`へ変更するとAPIを使える。YAMLへ直接キーを書く場合は、`.env`の仮キー行をコメントアウトし、すでに読み込んだ環境変数は`unset OPENAI_API_KEY`で解除する。仮キー入りの同梱設定では`false`にしているため、取り込みはそのまま検証できる。`false`の`embed`は`disabled`を表示し、APIを呼ばない。仮キーのまま`true`にすると、設定エラーで停止する。

モデルは`text-embedding-3-small`と`text-embedding-3-large`に対応する。次元数はsmallが最大1536、largeが最大3072で、短縮次元も指定できる。モデルと次元数を変えた場合は別プロファイルを作成する。[OpenAI公式Embeddingガイド](https://developers.openai.com/api/docs/guides/embeddings)

APIへ送る入力はチャンクの`search_text`（文書の相対パス・節・表のヘッダー等の文脈を含む抽出テキスト）。原本のファイルバイト列、画像、DBの接続情報、APIキーを本文に含めない。

## 仮想ファイルサーバー

ローカルではプロジェクト内の次のフォルダを共有フォルダとして扱う。

```text
data/file-server/department/
├── text/    rfc8259.txt, rfc4180.txt
├── word/    test.docx, blocks.docx, ~$lock.docx（除外）
├── excel/   test1.xlsx, multisheet.xlsx
├── pdf/     dummy.pdf, table.pdf
└── 旧版/    除外確認.txt（除外）
```

標準設定の`sources[].root`は`data/file-server/department`。本番では同じ設定をマウント済みの共有フォルダへ変更する。相対パスはYAMLの所在が基準で、WindowsではUNCパスも指定できる。`source_id`とルートからの相対パスで文書を識別し、原本の所在は`source_uri`へ保持する。

公開ファイルの準備は次を実行する。

```sh
python scripts/download_samples.py
```

取得URL・提供元・SHA-256は[サンプル一覧](../../examples/public-samples.json)、取得日時と実際のサイズは`artifacts/reports/public-samples-download.json`に記録する。TXT・Word・Excel・PDFを各2件取得し、OfficeのZIP構造とPDFヘッダーも検査する。再取得時にハッシュが変われば停止する。原本は`data/`配下としてGit管理対象外になる。

## 実行

[PostgreSQLの起動](local-postgresql.md)後、リポジトリのルートで次を実行する。

```sh
source .venv/bin/activate
set -a
source .env
set +a
python scripts/download_samples.py
python scripts/download_docling_models.py --config config.local.yaml
file-scan scan --config config.local.yaml
file-scan init-db --config config.local.yaml
file-scan ingest --config config.local.yaml
file-scan embed --config config.local.yaml
file-scan status --config config.local.yaml
python scripts/test_postgres.py
```

`init-db`はv1/v2からスキーマv3へ非破壊で移行する。既存の原本・世代・ベクトルを保持し、選択世代と段階別キャッシュの表を追加する。移行後はingestで選択を確定する。新しいDBは先に`CREATE EXTENSION vector`が必要。

`embed`は現在の対象・原本版・選択中世代・変換設定・実行時の設定が一致する下書きチャンクだけを対象にする。作成日時の最新世代には戻らない。失敗・未処理・設定不一致は`unready_documents`へ集計し、成功扱いにしない。

DB側で処理済み入力を除外し、最大`read_batch_size`件ずつキー範囲で読む。同じモデル・次元・入力版と実際の検索テキストが一致すれば、他世代で作成済みのベクトルも再利用する。モデル設定が異なるベクトルは共有しない。

分割時に検索入力全体をtiktokenで計測し、既定8191トークン以内にする。送信時も再検査し、1回の要求件数（最大32）と合計トークン数（最大300000）の両方を制限する。超過入力は送信せず記録する。DBの読込接続を閉じてからAPIを呼ぶ。トークナイザー辞書は初回に公開配布元から取得する。[OpenAI公式API仕様](https://developers.openai.com/api/reference/python/resources/embeddings/methods/create)

詳細は[取り込み改善設計](../design/05-ingestion-improvements.md)。`embedded`はAPI結果を保存したチャンク数、`api_inputs`はAPI入力件数、`reused`は他世代等の保存済み入力から適用した数、`unchanged`は現在の世代ですでに一致した数を示す。

API応答は件数・対応するindex・次元数・有限の非ゼロ数値を検査してからバッチ単位で保存する。失敗したバッチを含む未完了分は再実行で再開できる。ジョブ状態は`index_jobs`へ、キーを除いた結果は`artifacts/reports/embed-*.json`へ保存する。API例外の原文や認証情報をレポートへ転記しない。

Embeddingだけで検索公開は行わない。検索API、公開承認、利用者ACL、PGroongaは未実装。変動次元のベクトルを保存するため、検索用HNSW索引はこの変更では作らない。

## 検証の範囲

公開8ファイルを実際にダウンロードし、本文・原本位置・DB登録・除外・再実行時の再利用・原本の非変更を確認する。PDF2件はDoclingの実モデルで本文・表・原本座標を抽出する。文字なしページや画像解析待ちを含む場合は部分抽出とし、その`ingest`の終了コードは2。

APIのテストはOpenAI SDKのHTTP層を模擬し、送信先・認証ヘッダー・モデル・dimensions・float形式を検証する。応答のベクトルはテスト専用DB内の独立schemaへ保存し、認証エラー、次元不整合、トークン超過、再実行、モデル設定変更も確認する。本物のAPIキーによる生成の疎通・品質・料金は未検証。仮キーや模擬ベクトルを開発用DBへ保存しない。
