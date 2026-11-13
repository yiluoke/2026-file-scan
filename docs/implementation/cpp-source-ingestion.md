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

UNCはWindows、Ubuntu VMではマウント先を使う。Windows実機での動作確認は未実施。

## 設定

相対パスは設定ファイルの所在が基準。ソース専用設定の主な項目は次のとおり。

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
| `max_file_bytes`／`max_files` | 上限超過を黙って切り詰めず、失敗として記録 |
| `codewiki` | 固定版、工程期限、LLM応答トークン数、エージェント単位のリクエスト数、深さ。`python` の既定値は `cpp-scan` の実行中のPython |
| `documentation.*_env` | モデル・APIキー・OpenAI互換APIのbase URLを読む環境変数名 |
| `ingestion.file_scan_config` | 生成Markdown専用の既存file-scan設定 |

`ui_mode`は注記として保持し、初期版は指定値にかかわらずWidgets／QMLを検出する。`.pro/.pri/.prf`は参考入力として保存・更新検知し、評価しない。

文書取込設定では`sources`を専用`handoff/current`の1件、`include_extensions`を`[.md]`にする。`source_id`・`knowledge_id`はコード仕様専用の固定IDとする。既存DBを共用するときの`storage_dir`はそのDBの既存設定と一致させる。ソース原本保管領域とは別の役割である。

社内のモデル名・APIキー・base URLを、設定した環境変数名に設定する。`.env`は自動読込せず、YAMLの`${...}`も展開しない。`generate`だけがLLM認証を必要とし、接続先が明示されていなければ実行しない。初期版のモデル接続はOpenAI互換APIに対応する。

## 実行順

```sh
# 1. 棚卸し。原本コピー・LLM・DB接続なし。
cpp-scan scan --config config.cpp-source.local.yaml

# 2. 原本固定・CodeWiki構造解析・Qt補完。LLM・DB接続なし。
cpp-scan analyze --config config.cpp-source.local.yaml

# 3. 上のJSONに表示されたanalysis_idを指定。LLMへ送信する段階。
cpp-scan generate --config config.cpp-source.local.yaml --analysis analysis-...

# 4. 既存DBを事前準備。初期化済みの環境では再実行不要。
file-scan init-db --config config.cpp-docs.local.yaml

# 5. 生成JSONのgeneration_idを指定。既存処理で下書き登録。
cpp-scan ingest --config config.cpp-source.local.yaml --generation gen-...
cpp-scan status --config config.cpp-source.local.yaml
```

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
