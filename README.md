# 2026-file-scan

部署内の維持管理システムに関するソース、設計書、マニュアル、問い合わせ記録、障害対応記録を横断して調査・相談するナレッジ基盤。

第1版は、ファイルサーバー等の指定フォルダを収集し、文書構造・原本位置・チャンクをDBへ**下書き登録するCLI**を提供する。検索・AI回答を接続する前に、何を取り込めたかと抽出不足を確認するための実装である。

## 最初に動かす

Python 3.12以上を使用する。以下はmacOS／Linuxのシェル例。

```sh
python -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
cp config.example.yaml config.local.yaml
```

同梱の架空サンプル`examples/input/`で試す場合は、設定の編集は不要。社内資料を試す場合は、`config.local.yaml`の`sources[].root`を取り込みたいフォルダへ変更する。相対パスは設定ファイルの所在を基準に解決する。収集対象と出力先の`storage_dir`・`report_dir`は別の場所にする。

標準例はローカルSQLiteの`data/file_scan.sqlite3`を使用し、原本・画像は`artifacts/storage`、レポートは`artifacts/reports`へ保存する。DBサーバーなしで試せる。設定後は次の順序で実行する。

```sh
file-scan scan --config config.local.yaml
file-scan init-db --config config.local.yaml
file-scan ingest --config config.local.yaml
file-scan status --config config.local.yaml
```

| コマンド | 処理 |
| --- | --- |
| `scan` | 対象・除外・列挙エラーの棚卸し。レポートを書き出し、原本コピーやDB登録は行わない |
| `init-db` | スキーマv1を初期化。適用済みなら再利用し、既存表を削除しない |
| `ingest` | 原本コピー、抽出、分割、DBへの下書き登録、確認ファイルの出力 |
| `status` | 指定ナレッジの文書数、処理状態別の世代数等を表示 |

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
| PDF | 標準はpypdfの軽量文字抽出。画像を保存し、文字なしページ等を未処理として記録 |

PDFの段組み・表・正確な領域座標の完全復元、Wordの表示レイアウトの再現は未対応。画像は実体と原本位置を保持し、OCR・画像説明待ちの`pending_images`として記録する。旧形式のDOC・XLS・PPTとPPTXは第1版の対象外。

同梱設定の収集拡張子はTXT・MD・PY・XLSX・XLSM・DOCX・PDFに限定している。SQLやログ等も収集する場合は、`include_extensions`へ対象拡張子を追加する。

DB登録は`draft`のみ。**画像API、Embedding、検索索引・検索API、Dify、利用者別ACL、検索公開、レビュー承認画面は未実装**。取り込み処理から文書・画像を外部LLM／OCR／画像APIへ送信しない。

チャンク本文の既定上限`1800`は暫定の**文字数**であり、トークン数ではない。`token_count`はNULLのまま保存する。設計案のPoC用トークン目安とは別の設定である。

## 保存先と再実行

- DB：抽出本文、構造、原本位置、チャンク、ハッシュ、実体への参照、処理状態を保存する。原本・画像のバイト列をDBへ保存しない。
- `storage_dir`：原本コピーと画像を内容ハッシュで保管する。**1個のDBに対して初回登録時の`storage_dir`を固定**し、設定変更だけによる切り替えは拒否する。DBと保管先を一緒にバックアップする。
- `report_dir`：`scan-*.json`／`ingest-*.json`と、`generations/<世代ハッシュ>/`以下の確認用JSON・JSONL・Markdownを保存する。

同じ原本と同じ変換設定は既存の変換世代を再利用する。部分抽出を同じ条件でやり直す場合は、次を実行する。

```sh
file-scan ingest --config config.local.yaml --retry-partial
```

画像APIが未実装のため、再実行だけで画像の解析待ちが解消されるわけではない。詳細は[第1版の操作・実装説明](docs/implementation/first-version.md)を参照。

## PostgreSQLと任意のDocling

PostgreSQLでは[設定例](config.postgresql.example.yaml)と[ローカル検証用Compose](compose.yaml)を使う。DB接続先は`FILE_SCAN_DATABASE_URL`環境変数でYAMLより優先して指定できる。認証情報をGitへ登録しない。`.env.example`は設定例であり、Python CLIは`.env`を自動で読み込まない。

DoclingのPDF構造抽出を試す場合は追加依存を導入し、設定を切り替える。

```sh
python -m pip install -e '.[docling]'
```

```yaml
pdf_backend: docling
```

Docling経路はCPU固定・OCR無効で、外部画像説明サービスを使わない。実モデルを使う変換は未検証。初回はモデル取得や事前配置が必要になる場合があり、依存不足・変換失敗を標準経路の成功で置き換えない。

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

検証コマンドは`python -m pytest`と`python -m ruff check src tests`。

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
