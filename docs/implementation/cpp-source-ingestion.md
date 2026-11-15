# C++／Qt 5ソース解析・仕様書取込

2026-11-03。[初期版仕様](../design/09-cpp-qt-source-ingestion-spec.md)を基に、独立した`cpp-scan`を追加した。棚卸し、原本固定、CodeWiki構造解析、Qt補完、日本語Markdown生成、既存file-scanによる下書き登録に対応する。

社内ソースへ本環境からは接続していない。実際のソースとLLM接続先は社内で指定する。Qt・SDK・ビルド環境の導入や`.pro`の詳細確認は開始条件にしない。ビルド、追加ターゲット、アプリ起動は実行しない。

## 導入

`file-scan`、`file-search`、`cpp-scan`、固定版CodeWikiは同じPython 3.14の仮想環境で動く。依存は`requirements/ubuntu-26.04.lock`で一括管理する。新規導入では、リポジトリのルートで次を実行する。

```sh
python3.14 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements/ubuntu-26.04.lock
python -m pip install --no-deps --no-build-isolation -e '.[dev,search]'
python -m pip check
python -m pip install --no-deps -r requirements/codewiki-source.txt
python scripts/check_python_environment.py --codewiki
cp config.cpp-source.example.yaml config.cpp-source.local.yaml
cp config.cpp-docs.example.yaml config.cpp-docs.local.yaml
cpp-scan --help
```

既存の `.venv` を更新する場合は、`python3.14 -m venv` と設定例のコピーを省略する。設定済みのYAMLで `codewiki.python` が旧 `.venv-codewiki` を指している場合は、その1行を削除する。以後 `cpp-scan` は自身のPythonでCodeWikiの子プロセスを起動する。依存更新により変換プロファイルが変わるため、既存ナレッジを再取込する際は新しい抽出世代と検索公開の切り替えを確認する。

CodeWiki本体は `2.0.0` のコミット `1b4af43d6d20c8dbd758fe1d5f888cc6947ed11d` を使用し、実行時に照合する。統合lockにはC++／JavaScript構造解析とOpenAI互換API経路の依存を収録している。`--no-deps` でCodeWikiを導入した後の `pip check` には、MCP、他言語、他プロバイダー、レンダラーなど、今回の経路で使わない上流パッケージが不足として表示される。導入時はCodeWikiのインストール前の `pip check` と、導入後の `check_python_environment.py --codewiki` で確認する。閉域環境での準備は[オフライン導入手順](environment-setup.md#11-インターネットへ接続できない環境の場合)を参照する。

トークナイザーは、オフライン実行に備えて事前に準備する。

```sh
export TIKTOKEN_CACHE_DIR="$PWD/data/models/tiktoken"
python -c 'import tiktoken; tiktoken.get_encoding("cl100k_base")'
```

`TIKTOKEN_CACHE_DIR`は解析・生成・文書取込の実行時も設定する。トークナイザーの初回ダウンロードをオフライン運用時に行わないよう事前準備する。

`analyze` でも、CodeWikiの初期化時に `cl100k_base` の辞書を読む。`codewiki_tokenizer_download_failed` が出た場合は、接続できる環境で上のコマンドを実行し、作成されたキャッシュを実行環境へコピーして、そのフォルダを `TIKTOKEN_CACHE_DIR` に指定する。`codewiki_tokenizer_cache_invalid` が出た場合は、フォルダのパス、アクセス権、辞書を確認する。

辞書の初回取得で `SSLError` が出た場合も、準備済みキャッシュをコピーすれば `analyze` で辞書を再取得する必要はない。コピーするファイルは `cl100k_base` 用の `9b5ad71b2ce5302211f9c61530b329a4922fc6a4`。ファイル名を保持し、`TIKTOKEN_CACHE_DIR` にはその親フォルダを指定する。準備環境でも `CERTIFICATE_VERIFY_FAILED` が出て、組織のCAが必要な場合は、組織から配布されたCAバンドルのパスを `REQUESTS_CA_BUNDLE` に設定して取得する。

`codewiki_not_installed` や `ModuleNotFoundError` が出た場合は、`cpp-scan` を実行する仮想環境を有効にして `python scripts/check_python_environment.py --codewiki` を実行する。`codewiki_process_failed (analyze: ...)` の括弧内には工程名と例外の型が表示される。

UNCはWindows、Ubuntu VMではマウント先を使う。Windows実機での動作確認は未実施。

## 設定

相対パスは設定ファイルの所在が基準。3つの設定例には、各処理の運用項目を記載している。

| 本番設定 | 指定する項目 |
| --- | --- |
| `config.local.yaml` | 一般文書の収集元・除外・拡張子、一般文書用のID、原本保管先・レポート先、PDF・OCR・画像解析の有効化、Embeddingの有効化 |
| `config.cpp-source.local.yaml` | C++／Qtの `project_id`、原本フォルダ `source_root`、成果物フォルダ `artifact_root`、文字コード・収集範囲・上限、`codewiki` と `documentation`。DB取込まで行う場合は `ingestion.file_scan_config` で生成文書の設定を参照する |
| `config.cpp-docs.local.yaml` | 生成仕様書専用の `knowledge_id`・`source_id`、収集元、`.md` の拡張子指定、原本保管先・レポート先、Embeddingの有効化 |

一般文書と生成仕様書で同じDBを使う場合、接続先は環境変数 `FILE_SCAN_DATABASE_URL` の1か所で設定する。生成仕様書側で `database_url` を省略した場合、環境変数が未設定のときは組み込みのローカルDB接続先を使う。`storage_dir` は両方のYAMLに記載し、同じDBを使う場合は同じ実パスを指定する。`report_dir`、`knowledge_id`、`source_id` は用途ごとに設定する。

生成仕様書の設定例ではEmbeddingのモデル・次元数・キーの環境変数名に組み込みの値を使う。モデルは `text-embedding-3-small`、次元数は1536、キーの環境変数名は `OPENAI_API_KEY`。一般文書側でこれらを変更した場合、生成仕様書にも同じ変更を適用するには、生成仕様書側のYAMLにも指定する。`embedding.enabled` と `sources[].recursive` はそれぞれの設定ファイルで指定する。

稼働済み環境では既存のYAMLを使い続けられる。設定を整理する場合も、使用中のID・パスと既定値を上書きしている項目は引き継ぐ。

C++／Qtの主な設定は以下のとおり。

| 項目 | 内容 |
| --- | --- |
| `project_id` | 固定ID。実行日時・版を付けない |
| `source_root` | 社内ソースルート。設定例では同梱の`examples/qt5-source` |
| `artifact_root` | 原本・解析・生成・状態の専用領域。入力ルートとの親子関係は禁止 |
| `source_encoding` | `auto`／`utf-8`／`cp932`。autoはUTF-8優先、失敗時は`fallback_encoding` |
| `encoding_overrides` | `{"legacy/*.cpp": "cp932"}`等のパスglobごとの指定 |
| `include_tests`／`include_third_party` | 既定は除外。既知ディレクトリ名で判断するので独自配置は追加除外する |
| `include_files` | 明示収集する設定等の相対パスglob。JSONは参照または明示指定があるものだけ |
| `exclude_patterns` | パス・ファイル名glob。ディレクトリを指定すると配下も探索しない |
| `max_file_bytes`／`max_files` | C++原本は既定で1件10 MiB、最大5万件。文書取込の `max_file_bytes` は別設定で、既定は1件100 MiB |
| `codewiki` | 固定版、工程期限、LLM応答トークン数、エージェント単位のリクエスト数、深さ。`python` の既定値は `cpp-scan` の実行中のPython |
| `documentation.*_env` | 仕様書生成の既定は `CPP_SCAN_LLM_MODEL`、`CPP_SCAN_LLM_API_KEY`、`CPP_SCAN_LLM_BASE_URL`。既存の環境変数名に合わせる場合に指定する |
| `ingestion.file_scan_config` | 生成Markdown専用の既存file-scan設定 |

`ui_mode`は注記として保持し、初期版は指定値にかかわらずWidgets／QMLを検出する。`.pro/.pri/.prf`は参考入力として保存・更新検知し、評価しない。

生成仕様書の `sources[0].root` は、C++／Qt設定の `artifact_root` 配下の `handoff/current` を指す。`artifact_root` を変更したときは取込側の `root` も合わせる。受渡しは収集元1件、再帰あり（既定）、`include_extensions: [.md]`、除外なしを前提とする。

社内のモデル名・APIキー・OpenAI互換APIのベースURLを、仕様書生成用の環境変数に設定する。`.env.example` を参照し、`.env` を使う場合は実行前にシェルへ読み込んで変数をエクスポートする。

OpenAI APIに直接接続してGPT-6.1 Terraを使う場合は、次の値と `CPP_SCAN_LLM_API_KEY` にOpenAIのAPIキーを設定する。

```sh
export CPP_SCAN_LLM_MODEL=gpt-6.1-terra
export CPP_SCAN_LLM_BASE_URL=https://api.openai.com/v1
```

`gpt-6.1-terra` を指定すると、モジュール分類とツールを使う仕様書生成は Responses API (`/v1/responses`) に切り替わる。`store: false` を指定し、ツールの実行結果と継続に必要な暗号化済み推論情報を次のリクエストに含める。他のモデルIDは従来のChat Completions経路を使用する。[GPT-6.1 TerraのAPI仕様](https://developers.openai.com/api/docs/models/gpt-6.1-terra)

辞書の取得には `REQUESTS_CA_BUNDLE`、Python OpenAI SDKのHTTPS接続には `SSL_CERT_FILE` または `SSL_CERT_DIR` を使う。後者の両方が未設定の場合、`cpp-scan generate` は `REQUESTS_CA_BUNDLE` のCAをOpenAI SDKにも渡す。`codewiki_llm_tls_failed` が出た場合は、接続先の証明書を検証できるCAがバンドルに含まれているか確認する。[HTTPXの環境変数](https://www.python-httpx.org/environment_variables/#ssl_cert_file)

社内プロキシ経由でHTTPSに接続する場合は、社内IT指定のプロキシURLを `HTTPS_PROXY` に設定し、実行シェルにエクスポートする。`cpp-scan` は標準の `HTTP_PROXY`、`HTTPS_PROXY`、`ALL_PROXY`、`NO_PROXY` と、それぞれの小文字表記をCodeWikiの子プロセスに渡す。API接続先が `NO_PROXY` に該当すると、その接続にはプロキシを使わない。[HTTPXのプロキシ設定](https://www.python-httpx.org/environment_variables/#proxies)

`codewiki_llm_proxy_failed` が出た場合は、プロキシのURL、認証情報、API接続先への通信許可を確認する。`codewiki_llm_dns_failed` が出た場合は、API接続先またはプロキシのホスト名とDNS設定を確認する。`codewiki_llm_connection_failed` が出た場合は、同じシェルからAPI接続先への通信経路を確認する。HTTP 401は認証、403は権限、404はモデルまたは接続先、429はレート制限や利用枠を確認する。`codewiki_llm_quota_exhausted` が出た場合はAPIプロジェクトの請求設定と利用枠を確認する。

## 実行順

```sh
# 1. 棚卸し。原本コピー・LLM・DB接続なし。
cpp-scan scan --config config.cpp-source.local.yaml

# 2. 原本固定・CodeWiki構造解析・Qt補完。LLM・DB接続なし。
cpp-scan analyze --config config.cpp-source.local.yaml

# 3. 保存済みの解析IDを取得して仕様書を生成する。LLMへ送信する段階。
analysis_id=$(cpp-scan status --config config.cpp-source.local.yaml | \
  python -c 'import json, sys; print(json.load(sys.stdin)["selected"].get("analysis", ""))') &&
  [ -n "$analysis_id" ] &&
  cpp-scan generate --config config.cpp-source.local.yaml --analysis "$analysis_id"

# 4. 既存DBを事前準備。初期化済みの環境では再実行不要。
file-scan init-db --config config.cpp-docs.local.yaml

# 5. 生成JSONのgeneration_idを指定。既存処理で下書き登録。
cpp-scan ingest --config config.cpp-source.local.yaml --generation gen-...
cpp-scan status --config config.cpp-source.local.yaml
```

`generate` の `--analysis` には `analyze` の出力にある `analysis_id` の値全体を指定する。形式は `analysis-` に続く32桁の小文字16進数。手入力する場合も、表示された値を省略せずに使う。`invalid_version_id` が出た場合は `--analysis` に渡した値の形式を確認する。上のコマンドは同じ設定で保存済みの解析IDを読み込むため、解析の再実行を省ける。

`cpp-scan ingest` がDBスキーマの確認で止まった場合は、同じシェルで `file-scan status --config config.cpp-docs.local.yaml` を実行する。DBを更新せずに検査結果を表示する。設定ファイル名は `ingestion.file_scan_config` の指定に合わせる。対象DBを確認し、`init-db` が必要と表示された場合は上の手順4を実行して、元の `generation_id` で取込を再試行する。

`analyze`は安定コピー、原本ハッシュ、UTF-8作業コピー、文字コードと行・バイト位置対応を保存する。デコードできない原本も保持する。再度ファイル一覧と内容を照合して収集中の変化を検出する。通常フォルダは全ファイルが同一時点であることを保証しないため、固定したGitアーカイブ等を推奨する。

CodeWikiの`DependencyGraphBuilder`で構造を取得し、C++字句・XML・QML字句からQt情報を補完する。標準の`codewiki generate`を構造解析だけのコマンドとして呼ぶことはしない。原本フォルダとスナップショットは書き換えない。

`generate`は固定版CodeWikiの`CLIDocumentationGenerator`に日本語・モデル・制限を渡す。専用HOMEと一時領域を使い、利用者のCodeWiki設定を書き換えない。キーは環境からメモリーへ読み、引数・設定ファイルへ保存しない。全体、プラグイン、画面、処理・接続、API、未確認事項、ファイル別Qt補完をMarkdownへまとめる。

出力一覧、ハッシュ、UTF-8、必須資料、Markdownの内部リンクと見出しを検証する。`main.cpp:12-18` のように示された根拠パスと行範囲は、固定した原本に照合する。コードブロック内の記述は原文を保持する。CodeWikiの説明と根拠の一致、Qtの実行時対応はレビューで確認する。

同一入力・条件の完成済み成果物は検証して再利用する。`analyze --snapshot snap-...`で保存版を再解析でき、`analyze`／`generate`の`--force`で新しい版を作る。旧版は上書きしない。CodeWikiの`--update`は使わず、コメント・画面・参考設定の変更も検知する。CodeWikiページは所有ファイル集合等から安定名へ対応付け、構成変更時の改名はmanifestで確認できる。

`ingest`は完成Markdownだけを専用収集先へ配置し、既存`file_scan.pipeline.ingest`を呼ぶ。既存の変換、分割、原本版、抽出世代、選択世代、キャッシュを利用する。**C++原本・中間JSONは既存文書パーサーへ渡さず、DBスキーマも追加しない。**

プロジェクト排他、配置中断の記録、直前一式の保持を行う。DB開始後はファイルだけ旧版へ戻さない。既存DBの確定単位は1文書であり、全仕様書の同時切替・自動DBロールバックは保証しない。対象スナップショットと全体の解析状態は生成manifestに、取込結果には生成版、文書ID、Markdownハッシュ、選択抽出世代を記録する。

既定は部分失敗の受渡しを拒否する。`allow_partial_ingest: true`の場合のみ、欠落を記載した資料を下書き登録できる。この場合は欠けた文書を直前のMarkdownで保持する。受渡し結果の`retained_paths`を確認する。収集ID・ナレッジID・受渡し先の変更は拒否する。

入力ルート・収集範囲の変更や、登録済み文書の削除候補がある場合は、配置・DB更新の前に停止する。`cpp-scan status` の `latest.ingest.planned_removals` で対象文書を確認し、意図した変更は同じ生成版の `ingest` に `--accept-scope-change` を付けて適用する。入力ルートを記録していない従来版からの初回切り替えでも、入力ルートを確認して同じオプションを指定する。

Markdownの内容と変換条件が同じ取込済み文書は、選択抽出世代と既存の公開状態を維持する。従来のMarkdown本文に含まれていた版番号を外すため、導入時の再生成では内容の変更として一度更新される場合がある。更新文書は `file-scan embed --config config.cpp-docs.local.yaml` でEmbeddingを作成し、[file-searchの管理コマンド](search-api.md)で明示公開する。全件の公開を要する検索では、更新文書の再公開まで公開範囲不足になる場合がある。検索の引用先は生成Markdownとする。

## 保存先・状態

```text
<artifact_root>/
  objects/                       内容ハッシュで保管する原本
  snapshots/snap-.../             original、utf8、maps、manifest.json
  analyses/analysis-.../          analysis.json、codewiki生出力、manifest.json
  generations/gen-.../            codewiki、documents、manifest、quality-report、usage、review
  handoff/current/               既存文書取込が読む完成Markdown
  state/                         工程履歴・選択版・受渡し・中断記録
```

終了コード：`0`成功、`1`設定・ツール・工程全体の失敗、`2`部分失敗を含む完了、`130`中断。失敗した新世代は完成済みの選択版を置き換えない。外部プロセスの期限超過は子プロセスを終了して失敗記録する。認証値・本文・外部例外全文は通常ログへ出さない。原本や成果物は社内の保管領域で管理する。

## 制限と検証

Qt補完はClangによる型解決やQMLの完全な構文・実行評価ではない。独自マクロ、複雑なテンプレート引数、動的Factory、設定から作るパス、プラグイン所属は候補・未解決としてレビューする。CodeWikiとQt補完の説明の意味的矛盾は自動確定しない。

C++パーサーのマクロ回復後も構文エラー候補が残る場合は、ファイル別の部分対応と件数をレポート・未確認事項へ記載する。構造を一切取り出せなかったエラーファイルは、工程の部分失敗として扱う。クラス等を取得できた場合でも、そのファイル全体の完全な解析を意味しない。

Mermaidの外部検証を`MERMAID_VALIDATE=0`で無効化し、図を検証済みと表示しない。CodeWikiのトークン・リクエスト制限は応答／エージェント単位で、プロジェクト全体の金額上限ではない。金額制限が必要な環境ではAPIゲートウェイ側の予算制限を併用する。

```sh
export TIKTOKEN_CACHE_DIR="$PWD/data/models/tiktoken"
python -m pytest tests/test_cpp_scan.py -q
python -m ruff check src tests scripts
```

テストは原本保存・Qt補完・仕様書検証・既存の実取込を動かす。同じ仮想環境にCodeWikiが入っていれば、実構造解析と、ローカル固定応答APIでの文書生成・ツール編集も実行する。実モデルによる説明の精度・費用と社内ソースの網羅率は、社内接続先を設定した後の受入確認が必要。
