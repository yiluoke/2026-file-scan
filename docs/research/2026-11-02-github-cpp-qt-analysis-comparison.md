# GitHub上のC++／Qt解析・仕様書化・RAGプロジェクト比較

調査日：2026-11-02。

追補：利用者の指摘を受けCodeWiki 2.0を追加確認した。前回一覧への未掲載は調査漏れであり、評価して除外した結果ではない。仕様ドキュメント生成では優先検証候補へ追加し、下記の推奨を更新した。

対象条件：利用者の訂正によりQt 5、多数のプラグイン、ビルド可能な環境あり。以前のQt 3という想定は取り下げた。Qt 5の詳細版とWidgets／QMLの利用範囲は未確認。既存システムはPython、PostgreSQL＋pgvectorによる文書取込・Embedding・検索公開を持つ。

追加の目視情報：`.pro`内に`QMAKE_EXTRA_TARGETS`等があり、`CMakeLists.txt`は見つからない。qmake用プロジェクトとして導入方法を検討する。社内ソースは個人PCからアクセスできず、現段階で対象ソースへの実行検証はできていない。

調査は開発元のGitHub、公開ソース、公式マニュアルを対象にした。以下の適合性・優先順位はそれらを読んだ評価であり、対象アプリや旧Qt SDKを使った実行結果ではない。参照した`main`／`master`は変更されるため、実際の検証時にリリース・コミットと依存を固定する。

## 1. 結論

**仕様ドキュメント生成の優先検証候補にCodeWiki 2.0を追加する。** Doxygenは構造抽出の比較基準・補助、clang-umlは図、ClangまたはCode-Graph-RAGは意味解析・関係検索の参考として評価する。pgvector登録の実装例としてはCocoIndexが近い。対象Qtアプリでの実測前に、採用製品を確定するものではない。

調査範囲では、対象Qtアプリの画面・Signal／Slot・プラグイン機構をまとめて復元し、根拠付きの業務仕様書を生成して既存のDBへ登録するところまで、そのまま使えると確認できた完成品はない。汎用C++対応と対象アプリのQt機構の解析精度は別に評価する。

第1版の接続候補は「CodeWiki 2.0による文書生成＋Qt固有部分の補完 → Markdown仕様書 → 既存file-scan」。Doxygen等の構造化出力から文書を組み立てる案と、生成範囲・根拠の正しさ・必要な開発量を比較する。既存の取込・世代管理・Embeddingは利用できる。

## 2. 候補比較

| プロジェクト | 公開資料で確認した役割 | 今回の使い方・評価 |
| --- | --- | --- |
| [FSoft-AI4Code/CodeWiki](https://github.com/FSoft-AI4Code/CodeWiki) | C++を含む依存関係解析、モジュール階層、日本語Markdown・図の生成、構成要素単位の差分更新 | **仕様書生成の優先検証候補。** Qt 5の画面・接続・プラグイン補完と既存DB登録を評価する |
| [doxygen/doxygen](https://github.com/doxygen/doxygen) | C++の構造・コメントから資料を作成。XML出力あり | 最初の棚卸し・API一覧・Markdown生成の入力に向く。業務処理仕様は追加生成する |
| [bkryza/clang-uml](https://github.com/bkryza/clang-uml) | Clangによるクラス・シーケンス・パッケージ・include図。JSON等の出力 | プラグイン内部の構造・代表処理の図に有力。Qtの動的な接続を自動復元できるとは未確認 |
| [llvm/llvm-project](https://github.com/llvm/llvm-project) | Clangの解析基盤 | 詳細な型・呼出・位置情報と独自ルールが必要な段階の基盤。完成した仕様書生成器ではない |
| [vitali87/code-graph-rag](https://github.com/vitali87/code-graph-rag) | 多言語の構造をグラフへ取り込み、自然言語で調査。C++向けlibclang経路もある | コード関係・検索の参考として有力。既存pgvectorへの接続とQt固有解析は別途必要 |
| [cocoindex-io/cocoindex](https://github.com/cocoindex-io/cocoindex/tree/main/examples/code_embedding) | コード分割・Embedding・ファイル位置・pgvector保存の例 | DB登録・差分処理の参考に近い。確認した例の収集設定にはC++拡張子が入っていない |
| [tree-sitter/tree-sitter-cpp](https://github.com/tree-sitter/tree-sitter-cpp) | C++文法の構文解析 | ビルド条件を再現できない範囲の代替。型解決・呼出先確定の代替とはしない |
| [rizsotto/Bear](https://github.com/rizsotto/Bear) | コンパイルコマンドを捕捉し`compile_commands.json`を生成 | ビルド情報取得の補助。解析器や仕様書生成器ではない |
| [sourcegraph/scip-clang](https://github.com/sourcegraph/scip-clang) | ClangによるC/C++の参照索引、SCIP出力 | 宣言・参照を扱う共通索引の候補。仕様書・ベクトル生成は別。READMEではBeta |
| [joernio/joern](https://github.com/joernio/joern) | Code Property Graph、構文・制御・データフローの調査 | 入力検証・エラー・データの流れを詳しく調べる場合の追加候補 |
| [KDAB/codebrowser](https://github.com/KDAB/codebrowser) | 静的HTMLと参照情報によるソース閲覧 | 人が根拠を確認する画面の参考。既存のWoboq URLはKDABへ転送される |
| [AsyncFuncAI/deepwiki-open](https://github.com/AsyncFuncAI/deepwiki-open) | リポジトリからWiki・図・質問応答を作る | 説明資料の構成・画面・RAGの参考。C++意味解析の中核としては採用を推奨しない |
| [CoatiSoftware/Sourcetrail](https://github.com/CoatiSoftware/Sourcetrail) | グラフとソースを組み合わせた閲覧 | UI・関連表示の参考。公式リポジトリは2021-12-14にアーカイブ済み |

## 3. 優先候補の確認内容

### CodeWiki 2.0：仕様ドキュメント生成の優先検証候補

対象は[FSoft-AI4Code/CodeWiki](https://github.com/FSoft-AI4Code/CodeWiki)。ソースから依存グラフを作り、階層的にモジュールへまとめ、AIエージェントが下位モジュールから上位・全体概要へ順に文書化する。モジュール別Markdown、Mermaid図、モジュール木、グラフ、生成metadata等を出力する。MITライセンスで、日本語出力も案内されている。[README](https://github.com/FSoft-AI4Code/CodeWiki/blob/main/README.md)

2.0で強化された点は、C/C++等の自由関数を含む構造抽出・スコープ等を考慮した呼出解決、ビルド・設定等のartifactの文書化、構成要素単位の差分を基にした文書更新である。[開発ガイド](https://github.com/FSoft-AI4Code/CodeWiki/blob/main/guides/development.md)、[artifactガイド](https://github.com/FSoft-AI4Code/CodeWiki/blob/main/guides/artifact-aware-generation.md)、[差分更新ガイド](https://github.com/FSoft-AI4Code/CodeWiki/blob/main/guides/incremental-updates.md)

今回の「既存ソースから、全体構成・機能・モジュールの説明を生成する」目的に近い。DoxygenのAPI抽出やCode-Graph-RAGの質問応答とは役割が異なり、仕様書生成を一から実装する量を減らせる可能性があるという評価である。前回比較に含めなかった技術的な除外理由はない。

公開ソースを確認した範囲での検証点は次のとおり。いずれも候補から除外する理由ではなく、対象Qtアプリで確認する事項である。

| 検証点 | 確認結果と追加作業 |
| --- | --- |
| C++解析方式 | `cpp.py`はTree-sitter C++を利用。構文エラー時に大文字マクロを正規化して構造を回復する処理がある。実際のSDK・defineを使うClang意味解析と同等とは扱わない |
| Qt固有の関係 | 確認したC++解析ファイルにはQt／SIGNAL専用処理を見つけていない。関数ポインタ・lambda・旧形式の接続、自動接続、プラグインメタデータとローダーの対応を正解例と比較する |
| `.ui`・qmake・QML | 以前確認した既定パターンに`.ui`・`.pro`・`.pri`はなかった。Qt 5では`.qml`・`qmldir`・`.qrc`の収集・解析も含め、採用版で再確認する |
| 画面とC++ | Widgetsは`.ui`と対応する手書き`.h/.cpp`、QMLは画面とC++の型登録・公開機能を一緒に検証する |
| モジュール境界 | 自動クラスタリング結果が、実際のプラグイン・業務機能の境界と一致するか比較する |
| 既存DB接続 | まず生成Markdownを既存file-scanへ渡す。原本コードへの直接引用や構造化グラフを既存検索へつなぐにはアダプターを作る |

[C++解析実装](https://github.com/FSoft-AI4Code/CodeWiki/blob/main/codewiki/src/be/dependency_analyzer/analyzers/cpp.py)、[既定パターン](https://github.com/FSoft-AI4Code/CodeWiki/blob/main/codewiki/src/be/dependency_analyzer/utils/patterns.py)、[artifact実装](https://github.com/FSoft-AI4Code/CodeWiki/blob/main/codewiki/src/be/dependency_analyzer/analyzers/artifact.py)

ビルド・設定をグラフ化する機能も、コンパイルや動的ローダーを完全に再現する機能ではない。公式artifactガイドも、関連付けは浅いヒューリスティックであり、呼出グラフではないと明記している。

開発元はElectronを含むC++の文書評価を報告しているが、対象Qtアプリの解析率や仕様の正答率を保証するものではない。対象ソースによる生成・比較はまだ実施していない。

追加する検証は「同じ代表プラグインをCodeWiki 2.0とDoxygenベース案で文書化し、機能の説明、Qt接続の取りこぼし、根拠、更新時の差分を比較する」。CodeWikiの生成文書と構造化出力を保存し、不足するQt解析を補う構成を評価する。

### Doxygen：最初に試す抽出器

READMEは、コメントがないソースからも構造を抽出でき、include依存・継承・協調関係を可視化できると説明している。XMLは`GENERATE_XML`で出力できる。[README](https://github.com/doxygen/doxygen/blob/master/README.md)、[出力形式](https://www.doxygen.nl/manual/output.html)

XMLからクラス・関数・コメント等を読み取り、プラグイン・画面・処理単位のMarkdownへ変換するアダプターを作る案。通常のHTMLを再解析するより、機械向けのXMLを入力にする方が扱いやすいという設計判断である。

`CLANG_ASSISTED_PARSING`と`CLANG_DATABASE_PATH`による補助解析もある。ただしlibclangを有効にしてビルドされたDoxygenが必要であり、設定を加えるだけで全配布版で利用できるとは限らない。[設定の公式説明](https://www.doxygen.nl/manual/config.html#cfg_clang_assisted_parsing)

初期検証では、通常解析とClang補助解析の結果を比べる。対象SDKの互換性、マクロ展開、コメントの文字コード、画面から処理への対応漏れを確認する。API一覧が得られても、ボタンから更新処理までの実行経路が完成したとは扱わない。

### clang-uml：仕様書へ添付する図

READMEでクラス図、シーケンス図、パッケージ図、include図を確認した。出力形式はPlantUML、Mermaid、GraphML、JSON。解析にはコンパイルDBまたはコンパイルフラグ情報が必要と説明されている。[README](https://github.com/bkryza/clang-uml/blob/master/README.md)

代表プラグインの主要クラスと、入口関数からの処理を限定して図にする用途を推奨する。名前空間・ディレクトリに基づくパッケージ図を、そのままビルドターゲットや動的プラグインの依存図と同一視しない。

Qtの文字列形式Signal／Slotは通常のC++呼出と異なり、ローダーも実行時に対象を決め得る。この部分はQt専用解析の結果を別のエッジとして統合する。図は静的な構造・候補を表すものとし、実行トレースと混同しない。

### Code-Graph-RAG：独自実装の参考として特に有力

現在のREADMEはTree-sitterを基本にし、利用可能な場合にC/C++のlibclang解析や実行時観測を重ねる構成を説明している。グラフの既定保存先は組込みSQLiteで、Memgraph・Neo4jは選択肢とされている。[README](https://github.com/vitali87/code-graph-rag/blob/main/README.md)

公開コードの`cpp_frontend/frontend.py`で次を確認した。

- Pythonの`clang.cindex`を利用する実装がある。
- 対象パスと祖先、その`build/`から`compile_commands.json`を探す。
- libclangが使用できない場合、LIBCLANG／HYBRID指定からTree-sitterへ切り替える。
- `run_cpp_frontend`と`run_cpp_frontend_hybrid`の公開入口がある。

[C++ frontend実装](https://github.com/vitali87/code-graph-rag/blob/main/codebase_rag/parsers/cpp_frontend/frontend.py)、[公開入口](https://github.com/vitali87/code-graph-rag/blob/main/codebase_rag/parsers/cpp_frontend/__init__.py)

参考価値が高いのは、構文解析と意味解析の併用、解析方式の識別、シンボルと関係のグラフ化である。対象Qtアプリを渡せば接続・画面・プラグインが正確に復元されると確認したものではない。

言語対応資料はC++を対応対象に挙げる一方、Python以外はUTF-8で読むと記載している。CP932の既存ソースなら、変換と原本位置対応の検証が必要。また、一般の構成説明にはMemgraph中心の記述が残るため、READMEと実装・採用版を照合する。[言語対応と文字コード](https://github.com/vitali87/code-graph-rag/blob/main/docs/architecture/language-support.md)、[構成説明](https://github.com/vitali87/code-graph-rag/blob/main/docs/architecture/overview.md)、[設定実装](https://github.com/vitali87/code-graph-rag/blob/main/codebase_rag/config.py)

既存システムへ使う場合は、まず独立した検証環境で出力を評価し、必要な事実だけを自前の中間形式へ変換する。グラフDB・編集機能・RAG画面を一括導入することは必須にしない。

### CocoIndex：ベクトル登録処理の参考

`examples/code_embedding/main.py`は、ファイル列挙、言語判定、分割、Embedding、PostgreSQL＋pgvector保存、類似検索を実装している。保存データはファイル名・コード・ベクトル・開始行・終了行を含む。[確認したコード](https://github.com/cocoindex-io/cocoindex/blob/main/examples/code_embedding/main.py)

重要な相違点は次のとおり。

- 確認した例の収集パターンはPython・Rust・TOML・Markdown等で、`.cpp`・`.h`は含まれていない。
- 例のモデルは`sentence-transformers/all-MiniLM-L6-v2`。現在のOpenAI Embedding設定と同じではない。
- `RecursiveSplitter`で分割する例であり、QtのSignal／Slot・プラグイン関係を抽出する処理ではない。
- 保存先は例専用の`coco_examples.code_embeddings`。既存の下書き・原本・公開テーブルと互換な登録器ではない。

したがって、pgvectorへコードと位置を保存する実装の参考として評価する。既存のEmbedding経路があるため、採用するなら解析側の出力を既存経路につなぐ案と比較する。例を無変更でQtソースへ実行できるとは案内しない。

## 4. ビルド情報・厳密な参照索引の補助候補

### Bear

現在の`master`のREADMEはビルド中のコンパイラ呼出捕捉に加え、保存済みログ・dry-run出力からのDB作成も説明する。対応OSにWindowsも含まれる一方、方式ごとの制約があると記載している。古い紹介記事だけを根拠にUNIX専用と断定しない。[現在のREADME](https://github.com/rizsotto/Bear/blob/master/README.md)

対象環境で採用するリリースと、古いコンパイラ・ビルド方式での捕捉範囲を確認する。ビルド成功時の全翻訳単位、include、define、作業ディレクトリ、応答ファイルをそろえる。出力されたJSONの件数だけで網羅性を判断しない。

### LLVM／Clang、SCIP-clang

独自解析器を作る場合は[Clang LibTooling](https://clang.llvm.org/docs/LibTooling.html)が基盤となる。Pythonから扱うlibclangとC++のLibToolingはAPIが異なるため、必要なマクロ・構文情報をどちらで得るか検証して選ぶ。[LLVM公式リポジトリ](https://github.com/llvm/llvm-project)

[SCIP-clangのREADME](https://github.com/sourcegraph/scip-clang/blob/main/README.md)はC/C++/CUDA向け参照索引、`index.scip`出力、コンパイルDB入力を説明する。READMEのバイナリ配布対象はx86_64 Linuxとarm64 macOSで、プリコンパイル済みヘッダーは非対応とされる。旧Windows環境でそのまま動く前提にはしない。

SCIPは宣言・参照を扱う索引として検討できるが、全呼出グラフ、動的Qt接続、業務説明、Embeddingを単独で完成させる形式とは扱わない。

## 5. 追加の参考候補

### Joern

[Joern](https://github.com/joernio/joern)はC/C++等のCode Property Graphを扱い、[公式資料](https://docs.joern.io/c-syntaxtree/)は構文木・条件・呼出等の検索を例示する。処理中のチェックやデータの流れまで調べたい場合の候補。Qt固有のイベント・画面解析は実物で評価が必要であり、初期の仕様書生成に必須とはしない。

### KDAB Codebrowser

[README](https://github.com/KDAB/codebrowser/blob/master/README.md)はコンパイルDBを使って静的HTMLと参照情報を生成する構成を説明している。根拠ソースへ移動して人が確認する画面の参考になる。仕様書・ベクトルDBの登録器ではない。旧Woboq名のリポジトリを新しい別候補として重複計上しない。

### DeepWiki-Open

[README](https://github.com/AsyncFuncAI/deepwiki-open/blob/main/README.md)はWiki・説明図等の生成を案内する。確認した[現在の取込実装](https://github.com/AsyncFuncAI/deepwiki-open/blob/main/api/rag/pipeline.py)はUTF-8のファイル読込、位置を付けるTextSplitter、Embedding、LocalDBへの保存を行う。

説明資料の構成や引用位置の持ち方は参考になる。一方、この経路からClangによる型・呼出先の解決やQt固有の画面・接続・プラグイン解析が行われるとは確認できない。現在のpgvector・原本版・下書き公開の経路を置き換える完成品として扱わない。

### Sourcetrail

[公式リポジトリ](https://github.com/CoatiSoftware/Sourcetrail)は2021-12-14にアーカイブ済み。ソース・関係・画面を連動させる設計の参考として残すが、今回新しく作る取込基盤の第一候補から外す。

## 6. Qt向けに追加する処理

各候補のC++対応とは別に、次を対象ソースで検証・実装する。

| 解析 | 必要な結果 |
| --- | --- |
| Qtの版とビルド条件 | 実際に使うSDK、コンパイラ、include、define、対象構成 |
| Qt 5の接続 | 関数ポインタ・lambda・`SIGNAL`／`SLOT`形式と自動接続を宣言・実装へ対応付け。未解決は候補として保持 |
| Widgets／QML | `.ui`と利用者の`.h/.cpp`、または`.qml`・`qmldir`・`.qrc`とC++への公開を対応付ける |
| プラグイン | 本体ローダー、共通インターフェース、`Q_PLUGIN_METADATA`と参照先JSON、独自Factory、DLL、設定による有効化を対応付ける |
| 根拠と文字コード | スナップショット、原本ハッシュ、ファイル・行・元バイト範囲、CP932等の対応 |
| 差分・版 | ヘッダー・SDK・define・生成入力の変更と、仕様書・ベクトルの更新範囲 |

Qt 5への前提訂正に伴い、以前の`.ui.h`重点方針を変更した。優先するファイルと生成物の扱いは[Qt 5プロジェクトの確認ガイド](../design/08-qt5-project-analysis.md)を参照。

Qtのプラグインを多数使っていることだけを根拠に、すべてが同じ登録方式・ライフサイクルを持つとは扱わない。本体ローダーと共通インターフェースを最初の調査対象にする。

## 7. 推奨する検証順序

1. 本体ローダー・共通インターフェース・代表プラグイン1〜2個を固定したソース一式として用意する。
2. CodeWiki 2.0で日本語文書を生成する案と、Doxygenで抽出して文書化する案を比較する。期待するクラス・関数・画面処理、モジュール境界、Qt接続の説明を確認する。
3. コンパイルDBを整備し、clang-umlで主要クラスと入口処理の図を生成する。
4. 型解決・参照が不足する部分で、Code-Graph-RAGのlibclang／hybrid経路か独自Clang解析器を比較する。実際に使用した解析方式を記録する。
5. Qt専用補完と統合して構造化データを保存し、根拠付きMarkdownを作成する。
6. 生成文書を既存取込へ渡し、日本語の機能質問と識別子の質問で引用・更新検知を確認する。

比較項目は解析成功率、Signal／Slotの正解対応、プラグイン境界、CP932保持、引用先の正しさ、ヘッダー変更時の更新、処理時間。READMEの「対応」を実測した精度に置き換えて判断する。

## 8. ライセンス等の記録

GitHubの表示で確認した主なライセンスは、DoxygenがGPL-2.0、clang-umlとSCIP-clangとJoernがApache-2.0、CodeWiki 2.0とCode-Graph-RAGとTree-sitter C++とDeepWiki-OpenがMIT、BearがGPL-3.0。CocoIndexの[LICENSE](https://github.com/cocoindex-io/cocoindex/blob/main/LICENSE)はApache-2.0。

これは参照時点の表示・ファイルの記録。CLIとして使うか、ライブラリへ組み込むか、コードを転用するかは採用設計に記録し、採用する版のライセンスと依存を確認する。本調査ではライブラリの導入、ソースの転用、対象アプリの外部送信は行っていない。

既存システムへの接続条件とQtの解析方針は[専用取込の設計案](../design/06-cpp-qt-ingestion.md)を参照。
