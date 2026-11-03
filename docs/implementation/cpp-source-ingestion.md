# C++／Qt 5ソース解析・仕様書取込

2026-11-03。[初期版仕様](../design/09-cpp-qt-source-ingestion-spec.md)を基に、独立した`cpp-scan`を追加した。棚卸し、原本固定、CodeWiki構造解析、Qt補完、日本語Markdown生成、既存file-scanによる下書き登録に対応する。

社内ソースへ本環境からは接続していない。実際のソースとLLM接続先は社内で指定する。Qt・SDK・ビルド環境の導入や`.pro`の詳細確認は開始条件にしない。ビルド、追加ターゲット、アプリ起動は実行しない。

## 導入

既存のPython 3.14環境でCLIを登録する。

```sh
source .venv/bin/activate
python -m pip install --no-deps --no-build-isolation -e '.[dev,search]'
cp config.cpp-source.example.yaml config.cpp-source.local.yaml
cp config.cpp-docs.example.yaml config.cpp-docs.local.yaml
cpp-scan --help
```

CodeWikiは依存が異なるため専用環境にする。以下はPython 3.12で検証した、C++／JavaScript構造解析とOpenAI互換APIに絞ったアダプター用環境。閉域環境では接続可能な準備環境で検証し、環境とキャッシュを配布する。

```sh
python3.12 -m venv .venv-codewiki
.venv-codewiki/bin/python -m pip install --no-deps -r requirements/codewiki-source.txt
.venv-codewiki/bin/python -m pip install -r requirements/codewiki-api.lock
export TIKTOKEN_CACHE_DIR="$PWD/data/models/tiktoken"
.venv-codewiki/bin/python -c 'import tiktoken; tiktoken.get_encoding("cl100k_base")'
mkdir -p artifacts
.venv-codewiki/bin/python -m pip freeze > artifacts/codewiki-environment.txt
```

`TIKTOKEN_CACHE_DIR`は解析・生成・文書取込の実行時も設定する。トークナイザーの初回ダウンロードをオフライン運用時に行わないよう事前準備する。

CodeWiki本体は`2.0.0`、コミット`1b4af43d6d20c8dbd758fe1d5f888cc6947ed11d`を採用し、実行時にインストール情報と照合する。API用lockは実際に検証した依存版を記録している。版を変更する場合は互換性を検証して設定を更新する。

上記はCodeWikiが宣言する全機能の依存を入れる方法ではない。レンダラー、MCP、他言語・他プロバイダー等を除いており、`pip check`は省いた上流依存を報告する。`cpp-scan`の固定アダプター専用として利用する。標準CLIやMCPを使うため上流の全依存を導入する場合は、別環境で`pip install -r requirements/codewiki-source.txt`を行い、上流のNode.js/npm要件も満たす。

Windowsでは`codewiki.python`を`.venv-codewiki/Scripts/python.exe`へ変更する。UNCはWindows、Ubuntu VMではマウント先を使う。Windows実機での動作確認は未実施。

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
| `codewiki` | 専用Python、固定版、工程期限、LLM応答トークン数、エージェント単位のリクエスト数、深さ |
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

出力一覧・ハッシュ・UTF-8・必須資料・Markdown内部リンク・抽出根拠位置を検証する。これは説明の意味の正しさを保証するものではない。CodeWiki説明は推定を含む未レビュー文書、Qtの実行時対応も未確認と記載する。

同一入力・条件の完成済み成果物は検証して再利用する。`analyze --snapshot snap-...`で保存版を再解析でき、`analyze`／`generate`の`--force`で新しい版を作る。旧版は上書きしない。CodeWikiの`--update`は使わず、コメント・画面・参考設定の変更も検知する。CodeWikiページは所有ファイル集合等から安定名へ対応付け、構成変更時の改名はmanifestで確認できる。

`ingest`は完成Markdownだけを専用収集先へ配置し、既存`file_scan.pipeline.ingest`を呼ぶ。既存の変換、分割、原本版、抽出世代、選択世代、キャッシュを利用する。**C++原本・中間JSONは既存文書パーサーへ渡さず、DBスキーマも追加しない。**

プロジェクト排他、配置中断の記録、直前一式の保持を行う。DB開始後はファイルだけ旧版へ戻さない。既存DBの確定単位は1文書であり、全仕様書の同時切替・自動DBロールバックは保証しない。取込結果に生成版、文書ID、Markdownハッシュ、選択抽出世代を記録する。

既定は部分失敗の受渡しを拒否する。`allow_partial_ingest: true`の場合のみ、欠落を記載した資料を下書き登録できる。この場合は欠けた文書を直前のMarkdownで保持し、取得失敗を削除扱いにしない。受渡し結果の`retained_paths`を確認する。対象範囲を変更した版は停止し、意図した変更を確認して適用する場合に`--accept-scope-change`を指定する。収集ID・ナレッジID・受渡し先の変更は拒否する。

Embeddingは自動実行しない。確認後、既存設定の`embedding.enabled`と認証を設定し、`file-scan embed --config config.cpp-docs.local.yaml`を実行する。既存のpgvector、モデル・次元・入力キャッシュを使う。公開は[file-searchの管理コマンド](search-api.md)で明示する。引用先は生成Markdownであり、C++直接引用APIは追加しない。

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
CPP_SCAN_CODEWIKI_PYTHON="$PWD/.venv-codewiki/bin/python" python -m pytest tests/test_cpp_scan.py -q
python -m ruff check src tests scripts
```

通常テストはCodeWiki境界だけfixtureへ置換し、原本保存・Qt補完・仕様書検証・既存の実取込を動かす。専用Pythonを明示すると固定版CodeWikiの実構造解析と、ローカル固定応答APIでの文書生成・ツール編集も検証する。実モデルによる説明の精度・費用と社内ソースの網羅率は、社内接続先を設定した後の受入確認が必要。
