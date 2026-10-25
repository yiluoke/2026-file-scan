# 2026-file-scan

部署内の維持管理システムに関するソース、設計書、マニュアル、問い合わせ記録、障害対応記録を横断して調査・相談するナレッジ基盤。

第1版は、ファイルサーバー等の指定フォルダを収集し、文書構造・原本位置・チャンクをDBへ**下書き登録するCLI**を提供する。検索・AI回答を接続する前に、何を取り込めたかと抽出不足を確認するための実装である。

2026-10-25更新：取り込みの8改善を適用。変更仕様と既存DBの移行方法は[取り込み改善設計（v3）](docs/design/05-ingestion-improvements.md)を参照。

## 最初に動かす

Python 3.12以上を使用する。以下はmacOS／Linuxのシェル例。

```sh
python -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
cp config.example.yaml config.local.yaml
```

標準の収集元はプロジェクト内の仮想ファイルサーバー`data/file-server/department/`。`python scripts/download_samples.py`で公開TXT・Word・Excel・PDFを各2件配置してから試す。社内資料を試す場合は、`config.local.yaml`の`sources[].root`を取り込みたいフォルダへ変更する。相対パスは設定ファイルの所在を基準に解決する。収集対象と出力先の`storage_dir`・`report_dir`は別の場所にする。

ローカル検証も **PostgreSQL 18＋pgvector** を使用する。先に[ローカルDBの導入・起動手順](docs/implementation/local-postgresql.md)でDBと`.env`を準備する。原本・画像は`artifacts/storage`、レポートは`artifacts/reports`へ保存する。DB起動後、接続情報を読み込んで次の順序で実行する。

```sh
set -a
source .env
set +a
python scripts/download_docling_models.py --config config.local.yaml
file-scan scan --config config.local.yaml
file-scan init-db --config config.local.yaml
file-scan ingest --config config.local.yaml
file-scan status --config config.local.yaml
```

| コマンド | 処理 |
| --- | --- |
| `scan` | 対象・除外・列挙エラーの棚卸し。レポートを書き出し、原本コピーやDB登録は行わない |
| `init-db` | スキーマv3を初期化。v1/v2へ管理表・キャッシュを追加し、既存データを保持 |
| `ingest` | 原本コピー、抽出、分割、DBへの下書き登録、確認ファイルの出力 |
| `embed` | 設定で有効な場合、OpenAI APIでチャンクのEmbeddingを生成してpgvectorへ保存 |
| `status` | 現在の選択世代・Embedding準備状況と、過去世代の集計を分けて表示 |

終了コードは`0`が正常、`1`が設定・DB等の実行全体のエラー、`2`が部分抽出や個別ファイルの失敗等を含む完了。画像の解析待ちも部分抽出として扱う。

Windowsでは仮想環境の有効化に`.venv\Scripts\Activate.ps1`を使う。Windows実機・実際の社内ファイルサーバーでの動作は未検証。UNCパスはWindowsで使用し、macOS／Linuxではマウント先を指定する。

## 第1版の対応範囲

| 入力 | 現在の方式 |
| --- | --- |
| TXT・Markdown | UTF-8／CP932のテキスト抽出、行位置、Markdown見出し |
| SQL・JSON・YAML・YML・CSV・LOG | 構文解析・実行をせず、テキストと行位置を保持 |
| Python | 実行せずASTで関数・クラス等を抽出 |
| XLSX・XLSM | openpyxlとOOXMLで複数シート、結合、数式と保存済み結果、画像を抽出 |
| DOCX | OOXMLを直接読み、段落・表の順序、見出し、目次参照、画像を抽出 |
| PDF | 標準はDoclingの実モデルによる本文・表・レイアウト抽出。原本座標と画像を保持し、文字なしページはOCR待ちとして記録 |

DoclingによるPDFの段組み・表・領域座標は抽出するが、全原本の完全復元は保証しない。Wordの表示レイアウトの再現は未対応。画像は実体と原本位置を保持し、OCR・画像説明待ちの`pending_images`として記録する。旧形式のDOC・XLS・PPTとPPTXは第1版の対象外。

同梱設定の収集拡張子はTXT・MD・PY・XLSX・XLSM・DOCX・PDFに限定している。SQLやログ等も収集する場合は、`include_extensions`へ対象拡張子を追加する。

DB登録は`draft`のみ。**OpenAI Embeddings APIによるベクトル生成・pgvector保存に対応**。APIキー・モデル・次元数を設定できる。初期設定は仮キーを入れて無効化している。`ingest`はローカル抽出までを行い、`embed`が有効な場合に抽出テキストを外部APIへ送信する。画像API、検索API、Dify、利用者別ACL、検索公開、レビュー承認画面は未実装。

チャンク本文は既定1800文字以内、見出し・列見出しを含む検索入力は既定8191トークン以内に分割する。`cl100k_base`の実測値を`chunks.token_count`と`chunk_embeddings.input_tokens`へ保存する。表は行を優先し、後続チャンクの検索文脈にも列見出しを引き継ぐ。

## 保存先と再実行

- DB：抽出本文、構造、原本位置、チャンク、ハッシュ、実体への参照、処理状態を保存する。原本・画像のバイト列をDBへ保存しない。
- `storage_dir`：原本コピーと画像を内容ハッシュで保管する。**1個のDBに対して初回登録時の`storage_dir`を固定**し、設定変更だけによる切り替えは拒否する。DBと保管先を一緒にバックアップする。
- `report_dir`：`scan-*.json`／`ingest-*.json`と、`generations/<世代ハッシュ>/`以下の確認用JSON・JSONL・Markdownを保存する。

同じ原本と同じ変換設定は既存の変換世代を選択して再利用する。分割設定だけを変えた場合は抽出キャッシュを使い、同じEmbedding入力には保存済みベクトルを利用する。変更のない原本は完全ハッシュ照合を保ち、一時コピーを省略する。部分抽出を同じ条件でやり直す場合は、次を実行する。

```sh
file-scan ingest --config config.local.yaml --retry-partial
```

画像APIが未実装のため、再実行だけで画像の解析待ちが解消されるわけではない。詳細は[第1版の操作・実装説明](docs/implementation/first-version.md)を参照。

## OpenAI Embeddingとファイルサーバー

[設定・公開サンプル・検証手順](docs/implementation/openai-embedding.md)を参照。実キーを`.env`の`OPENAI_API_KEY`または`config.local.yaml`の`embedding.api_key`へ設定し、`embedding.enabled: true`へ変更して`file-scan embed --config config.local.yaml`を実行する。既定モデルは`text-embedding-3-small`、1536次元。仮キーではAPIを呼ばず設定エラーにする。

## PostgreSQL＋pgvectorとDocling

標準設定はPostgreSQLを使用する。[ローカルDBの手順](docs/implementation/local-postgresql.md)にmacOS用Postgres.appと[Compose](compose.yaml)の導入・起動・停止方法を記載する。Composeもpgvectorを含み、開発用とテスト用のDBで拡張を有効化する。DB接続先は`FILE_SCAN_DATABASE_URL`環境変数でYAMLより優先して指定できる。認証情報をGitへ登録しない。`.env.example`は設定例であり、Python CLIは`.env`を自動で読み込まない。

PDFは**Doclingの実モデルによるレイアウト・表構造抽出**を標準とする。Doclingは通常の依存に含む。初回に次を実行してモデルを事前取得する。

```sh
python scripts/download_docling_models.py --config config.local.yaml
```

```yaml
pdf_backend: docling
docling_artifacts_path: data/models/docling
```

CPU固定・OCR無効・外部サービス無効で処理する。公開PDF2件を実モデルで検証し、本文・表・bboxを確認する。画像の意味解析とOCRは後続処理。詳細は[Doclingの導入・抽出・検証](docs/implementation/docling-pdf.md)を参照。

## コードを読む順序

コードには日本語のdocstringと処理目的のコメントを記載している。全体の流れは次の順序で確認できる。

| 順序 | モジュール | 確認する内容 |
| --- | --- | --- |
| 1 | [cli.py](src/file_scan/cli.py) | コマンドと終了コード |
| 2 | [config.py](src/file_scan/config.py) | 設定値、パス、対象範囲の検証 |
| 3 | [domain.py](src/file_scan/domain.py) | 抽出・チャンクの共通データ形式 |
| 4 | [collector.py](src/file_scan/collector.py) | ファイル列挙、除外、列挙失敗の区別 |
| 5 | [storage.py](src/file_scan/storage.py) | 原本のコピー、変更検知、ハッシュ保管 |
| 6 | [parsers.py](src/file_scan/parsers.py) | 形式別の抽出と未対応箇所の記録 |
| 7 | [chunking.py](src/file_scan/chunking.py) | 構造を優先する分割と原文への対応 |
| 8 | [db.py](src/file_scan/db.py) | DB定義、制約、初期化 |
| 9 | [pipeline.py](src/file_scan/pipeline.py) | 一連の処理、再実行、文書単位の登録 |

検証は`.env`を読み込み、`python scripts/test_postgres.py`と`python -m ruff check src tests scripts`を実行する。実PostgreSQLとpgvectorの検証を含む。`python -m pytest`単独では、テスト用接続先が未設定の場合に実DBテストをスキップする。

## 設計資料

| 機能 | 資料 | 設計レベル |
| --- | --- | --- |
| 1. 文書変換処理 | [文書変換処理](docs/design/01-document-conversion.md) | 詳細設計案 |
| 2. 検索基盤 | [検索基盤](docs/design/02-search-platform.md) | 概要設計案 |
| 3. LLM＋チャット画面 | [LLM＋チャット画面](docs/design/03-llm-chat.md) | 概要設計案 |
| 共通. データベース | [テーブル設計](docs/design/04-database-tables.md) | 論理・カラム・制約・索引の詳細設計案 |

設計資料は最終構成の設計案を含む。第1版の実装範囲と制限は本READMEと[操作・実装説明](docs/implementation/first-version.md)を参照し、設計に記載した機能がすべて実装済みとは扱わない。社内実データによる抽出精度・処理性能の検証は今後行う。

## 想定構成

- 文書変換：Python、Docling、Excel・Word等の形式別補完処理。
- 検索：PostgreSQL、pgvector、PGroonga、FastAPIによる検索API。
- 会話：DifyのChatflowと別途選定するLLM。外部検索APIを利用する。

## リポジトリの管理範囲

設計資料、アプリのソース、認証情報を含まない設定例・評価用コードをGitで管理する。

社内資料の原本、実データ、抽出画像、変換生成物、ログ、認証情報は会社が承認した保管先で管理する。`.gitignore`で`data/`、`artifacts/`、`logs/`、`var/`、`config.local.yaml`、`.env`等を除外する。独自の保管先をリポジトリ内へ追加する場合も、私有データがGit対象に入らないよう設定する。
