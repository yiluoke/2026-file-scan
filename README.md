# 2026-file-scan

部署内の維持管理システムに関する設計書、マニュアル、問い合わせ記録、障害対応記録を横断して調査・相談するナレッジ基盤。

文書とC++・Qtソースから下書きを作り、管理者が公開した資料を検索APIで参照します。機能別の仕様書・機能説明資料・本番導入仕様は[ドキュメント一覧](docs/README.md)から選べます。

## 最初に動かす

本番構成はWindowsホスト上のUbuntu 26.04 VM、Python 3.14です。共通環境、モデル、DB、収集元の配置は[文書取込の本番導入仕様](docs/file-scan/本番導入仕様.md)を参照してください。

以下はmacOS／Linuxのシェル例。

```sh
python3.14 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements/ubuntu-26.04.lock
python -m pip install --no-deps --no-build-isolation -e '.[dev,search]'
python scripts/check_python_environment.py
cp config.example.yaml config.local.yaml
```

標準の収集元はプロジェクト内の仮想ファイルサーバー`data/file-server/department/`。`python scripts/download_samples.py`で公開TXT・Word・Excel・PDFを各2件配置してから試す。社内資料を試す場合は、`config.local.yaml`の`sources[].root`を取り込みたいフォルダへ変更する。相対パスは設定ファイルの所在を基準に解決する。収集対象と出力先の`storage_dir`・`report_dir`は別の場所にする。

階層が不明なバックアップフォルダは、各収集元の`exclude_directory_names`へ名前を指定すると、どの階層でも配下ごと除外できる。名前は大小文字を区別しない完全一致で、`BK`は`bk`にも一致するが`BK2`には一致しない。パスやglobは指定しない。既存の`exclude_directories`は引き続き収集元からの相対パスで指定し、両方を併用できる。

```yaml
sources:
  - source_id: department-share
    root: /mnt/file-server/department
    recursive: true
    exclude_directory_names: [BK, 一時保存]  # 階層を問わず除外
    exclude_directories: [設計書/旧版/]  # 特定の相対パスを除外
    exclude_filenames: ['~$*', '*.tmp']
```

除外フォルダの内部は探索せず、`scan`のレポートへフォルダ単位で`excluded_directory`を記録する。先に`scan`で対象を確認してから`ingest`へ進む。取り込み済み文書が除外条件に一致した場合は、次回`ingest`で対象外となり、保存済みの履歴は残る。

ローカル検証も **PostgreSQL 18＋pgvector** を使用する。先に[ローカル検証環境の構築記録](docs-backup-2026-11-20/implementation/local-postgresql.md)でDBと`.env`を準備する。原本・画像は`artifacts/storage`、レポートは`artifacts/reports`へ保存する。DB起動後、接続情報を読み込んで次の順序で実行する。

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

## C++／Qt 5ソースの解析・取込

文書取込とは別の`cpp-scan`を追加した。`scan` → `analyze` → `generate` → `ingest`の順に、ソース棚卸し、原本固定・CodeWiki構造解析・Qt補完、日本語Markdown生成、既存file-scanでの下書き登録を行う。`.pro`の詳細評価やビルドは必須にしない。

固定コミットのCodeWikiを、文書取込と同じPython 3.14の仮想環境に導入する。生成後の分割・DB登録・Embeddingは既存処理を使用し、Embeddingと検索公開は別途明示実行する。[導入・操作手順](docs/cpp-scan/本番導入仕様.md)・[ソース解析設定例](config.cpp-source.example.yaml)・[生成文書の取込設定例](config.cpp-docs.example.yaml)

解析結果と生成物の読み方は[C++・Qtソース取込の機能説明資料](docs/cpp-scan/機能説明資料.md)を参照。

## 第1版の対応範囲

| 入力 | 現在の方式 |
| --- | --- |
| TXT・Markdown | UTF-8／CP932のテキスト抽出、行位置、Markdown見出し |
| XLSX・XLSM | openpyxlとOOXMLで複数シート、結合、数式と保存済み結果、画像を抽出 |
| DOCX | OOXMLを直接読み、段落・表の順序、見出し、目次参照、画像を抽出 |
| PDF | 標準はDoclingの実モデルによる本文・表・レイアウト抽出。日本語RapidOCR＋ONNX Runtime CPUでスキャン本文も抽出。原本座標と画像を保持 |

DoclingによるPDFの段組み・表・領域座標は抽出するが、全原本の完全復元は保証しない。Wordの表示レイアウトの再現は未対応。PDFのスキャン本文はローカルOCRを行う。Office埋込画像や図の意味説明は`pending_images`として残し、実体と原本位置を保持する。旧形式のDOC・XLS・PPTとPPTXは第1版の対象外。

収集対象はTXT・MD・XLSX・XLSM・DOCX・PDF。Markdownの別拡張子`.markdown`も明示設定できる。`include_extensions`は対応文書形式の部分集合だけを受け付け、プログラム・SQL・JSON・YAML・YML・CSV・LOG等の追加は設定エラーにする。

DB登録は`draft`のみ。**OpenAI Embeddings APIによるベクトル生成・pgvector保存に対応**。APIキー・モデル・次元数を設定できる。初期設定は仮キーを入れて無効化している。`ingest`は文書を抽出し、画像認識を有効にした場合は対象画像の説明を生成する。`embed`は有効時に検索用テキストを外部APIへ送信する。Dify本体、利用者別ACL、レビュー承認画面は未実装。管理者が採用した世代は検索用のコピーに公開し、取込データは下書きのまま保持する。公開条件と操作は[検索APIの仕様書](docs/file-search/仕様書.md)を参照。

チャンク本文は既定1800文字以内、見出し・列見出しを含む検索入力は既定8191トークン以内に分割する。`cl100k_base`の実測値を`chunks.token_count`と`chunk_embeddings.input_tokens`へ保存する。表は行を優先し、後続チャンクの検索文脈にも列見出しを引き継ぐ。

## 保存先と再実行

- DB：抽出本文、構造、原本位置、チャンク、ハッシュ、実体への参照、処理状態を保存する。原本・画像のバイト列をDBへ保存しない。
- `storage_dir`：原本コピーと画像を内容ハッシュで保管する。**1個のDBに対して初回登録時の`storage_dir`を固定**し、設定変更だけによる切り替えは拒否する。DBと保管先を一緒にバックアップする。
- `report_dir`：`scan-*.json`／`ingest-*.json`と、`generations/<世代ハッシュ>/`以下の確認用JSON・JSONL・Markdownを保存する。

同じ原本と同じ変換設定は既存の変換世代を選択して再利用する。分割設定だけを変えた場合は抽出キャッシュを使い、同じEmbedding入力には保存済みベクトルを利用する。変更のない原本は完全ハッシュ照合を保ち、一時コピーを省略する。部分抽出を同じ条件でやり直す場合は、次を実行する。

```sh
file-scan ingest --config config.local.yaml --retry-partial
```

画像認識は既定で無効。対象形式と外部送信の有効化は[画像の説明に使う設定](docs/file-scan/仕様書.md#画像の説明)を参照。

## OpenAI Embeddingとファイルサーバー

[設定・公開サンプル・検証手順](docs/file-scan/本番導入仕様.md)を参照。実キーを`.env`の`OPENAI_API_KEY`または`config.local.yaml`の`embedding.api_key`へ設定し、`embedding.enabled: true`へ変更して`file-scan embed --config config.local.yaml`を実行する。既定モデルは`text-embedding-3-small`、1536次元。仮キーではAPIを呼ばず設定エラーにする。

## PostgreSQL＋pgvectorとDocling

別環境への新規導入用DDLは[sql/postgresql/schema-v3.sql](sql/postgresql/schema-v3.sql)。現行20テーブル・制約・12索引・pgvector有効化・スキーマ版登録を含む。[SQLの実行手順](sql/postgresql/README.md)を参照する。

標準設定はPostgreSQLを使用する。[ローカルDBの構築記録](docs-backup-2026-11-20/implementation/local-postgresql.md)にmacOS用Postgres.appと[Compose](compose.yaml)の導入・起動・停止方法を記載する。Composeもpgvectorを含み、開発用とテスト用のDBで拡張を有効化する。DB接続先は`FILE_SCAN_DATABASE_URL`環境変数でYAMLより優先して指定できる。認証情報をGitへ登録しない。`.env.example`は設定例であり、Python CLIは`.env`を自動で読み込まない。

PDFは**Doclingの実モデルによるレイアウト・表構造抽出**を標準とする。Doclingは通常の依存に含む。初回に次を実行してモデルを事前取得する。

```sh
python scripts/download_docling_models.py --config config.local.yaml
```

```yaml
pdf_backend: docling
docling_artifacts_path: data/models/docling
pdf_ocr:
  enabled: true
  mode: auto
  scale: 3.0
  timeout_seconds: 1800
```

日本語RapidOCR 3.9.1＋ONNX Runtime 1.24.1をCPUで実行する。`pdf_ocr.enabled`の既定値は`true`、`mode`は`auto`、期限は1800秒。モデルの不足・改変を実行前に検出する。PDFのOCR本文も既存チャンク・Embedding経路を使用する。図の説明は設定で有効にできる。詳細は[モデルの導入・検証手順](docs/file-scan/本番導入仕様.md#configuration)を参照。

PGroonga 4.1.0をローカルのアプリ用・テスト用DBへ導入済み。新規環境は[導入手順](docs/file-search/本番導入仕様.md)を参照。現在のComposeイメージはpgvectorのみ。検索APIは権限制御を保留したローカル試作として実装済み。[操作・公開・Dify接続手順](docs/file-search/仕様書.md)を参照。

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

## 検索API

本番導入は[検索APIの本番環境構築手順](docs/file-search/本番導入仕様.md)を参照。Ubuntu VMでのDB・設定・公開管理、systemd常駐化、NginxによるHTTPS接続、受入確認・復旧までをまとめている。

検索結果の順位・出典・公開状態の読み方は[検索APIの機能説明資料](docs/file-search/機能説明資料.md)を参照。

取り込みCLIから独立した`file-search`を追加した。ソースは取り込みが`src/file_scan/`、検索が`src/search/`の並列構成とする。上記の固定依存の導入には検索用extraも含まれる。取り込みv3の初期化後に次を実行する。

```sh
file-search init-db --config config.local.yaml
file-search create-release --config config.local.yaml --release '<対象業務版>'
```

確認した文書・世代を`file-search publish`で明示公開し、`FILE_SCAN_SEARCH_API_KEY`を設定して`file-search serve --config config.local.yaml`で起動する。管理者コマンド、入力例、実Embedding設定、Dify接続は[検索APIの管理CLI](docs/file-search/仕様書.md#管理cli)と[Dify接続手順](docs/file-search/本番導入仕様.md#dify)を参照。

誤公開は`file-search unpublish`で指定した業務版から取り下げられる。検索結果の`source_ref`と`related_assets[].image_ref`は、同じ接続キーで原本と対応画像を取得するための参照先となる。

検索表は取り込みv3と別の版で追加する。既存のdraft・世代選択・キャッシュは維持し、既存資料を自動公開しない。開発DBには検索表のみを追加済み。

DBを使わずAPI接続を試す場合は、`search.test_mode: true`を設定して起動する。[テスト用設定](config.search-test.example.yaml)と[起動・応答の説明](docs/file-search/仕様書.md#dbを使わない接続試験)を参照。固定のテスト文言を返し、DB検索・Embeddingは実行しない。

## 想定構成

- 文書変換：Python、Docling、Excel・Word等の形式別補完処理。
- 検索：PostgreSQL、pgvector、PGroonga、FastAPIによる検索API。
- 会話：DifyのChatflowと別途選定するLLM。外部検索APIを利用する。

## リポジトリの管理範囲

設計資料、アプリのソース、認証情報を含まない設定例・評価用コードをGitで管理する。

社内資料の原本、実データ、抽出画像、変換生成物、ログ、認証情報は会社が承認した保管先で管理する。`.gitignore`で`data/`、`artifacts/`、`logs/`、`var/`、`config.local.yaml`、`.env`等を除外する。独自の保管先をリポジトリ内へ追加する場合も、私有データがGit対象に入らないよう設定する。
