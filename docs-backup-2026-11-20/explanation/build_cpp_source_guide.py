"""C++／Qtソース取込のPDF・Markdown・図素材を生成する。アプリ・DBは操作しない。"""

from html import escape
import json
from pathlib import Path
import shutil
import subprocess
import tempfile

from PIL import Image
from reportlab.graphics import renderPDF, renderSVG
from reportlab.graphics.shapes import Drawing, Rect
from reportlab.lib import colors
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfgen import canvas
from reportlab.platypus import Paragraph, Table, TableStyle

import build_guide as layout

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
IMAGES = HERE / "images/cpp-source"
PDF = ROOT / "output/pdf/C++・Qtソース取込処理_図解ガイド.pdf"
MD = HERE / "C++・Qtソース取込処理_図解ガイド.md"
INK, TEAL, BLUE, PALE, MUTED, ORANGE = (
    layout.INK, layout.TEAL, layout.BLUE, layout.PALE, layout.MUTED, layout.ORANGE)
W, H, M, CW = layout.W, layout.H, layout.M, layout.CW
AMBER = colors.HexColor("#fff3e4")

PAGES = [
    dict(
        title="C++／Qtソースを、日本語の仕様書へ", tag="はじめて読む方へ",
        lead="ソースの構造とQt固有の記述を整理し、根拠付きの仕様書を作ります。その文章を、既存の取込・検索基盤へつなぎます。",
        diagram="intro",
        cards=[
            ("この資料でわかること", ["どのファイルから、何を取り出すか。", "CodeWikiとQt補完が分担する役割。", "生成した仕様書を、どう検索に使うか。"]),
            ("まず覚える3つの言葉", ["スナップショット：解析用に保存した原本の版。", "根拠：説明を照合するファイル・行・内容ハッシュ。", "世代：条件を記録して保存した解析・生成の結果。"]),
            ("読む順番", ["全体像と入力：2～5ページ。", "C++・Qt解析と文書化：6～12ページ。", "取込・検索・更新・操作：13～18ページ。"]),
        ],
        note="確認日：2026-11-03。現行実装の説明資料です。図の画面・コード・質問・数値は説明用で、社内アプリの解析結果ではありません。",
        sources=["docs/implementation/cpp-source-ingestion.md", "src/cpp_scan/cli.py"],
    ),
    dict(
        title="全体像：ソース解析を、既存取込の前に置く", tag="01 / 全体の流れ",
        lead="新しいcpp-scanがソースからMarkdownを作り、既存file-scanが文書の分割・下書き登録・ベクトル化を担当します。",
        diagram="pipeline",
        cards=[
            ("1. ソースを整理する", ["scanで棚卸しし、analyzeで原本を保存。", "CodeWiki構造解析とQt補完を実行します。", "この段階はLLMとDBを呼びません。"]),
            ("2. 日本語仕様書を作る", ["generateが指定したLLMへコード等を送ります。", "AIの説明とQtの抽出情報を資料へ整理。", "未確認事項を残し、出力の整合性を検証します。"]),
            ("3. 既存の基盤へ渡す", ["ingestが完成MarkdownをDBへ下書き登録。", "Embeddingはfile-scan embedで別に実行。", "検索公開もfile-searchで別に行います。"]),
        ],
        note="API送信の場面は2つ：仕様書生成でソース等をLLMへ、ベクトル化で仕様書の検索用テキストをEmbedding APIへ送ります。",
        sources=["src/cpp_scan/pipeline.py", "src/cpp_scan/handoff.py", "src/file_scan/embeddings.py"],
    ),
    dict(
        title="入力：C++に加え、Qtの周辺ファイルも読む", tag="02 / 重要ファイルの早見表",
        lead="画面やプラグインの情報はC++だけに揃っていません。関連ファイルを一緒に棚卸しして、役割を分けて扱います。",
        table=(["ファイル", "主な手掛かり", "現在の扱い"], [
            [".cpp / .h / .hpp等", "クラス、関数、Signal／Slot、Qtマクロ", "CodeWiki構造解析とQtの字句補完"],
            [".ui", "Widgetsの画面、部品名、表示文字、接続", "XMLから宣言を抽出"],
            [".qml / qmldir / 関連.js", "QML画面、import、イベント、C++公開との接点", "QMLは字句補完。.jsはCodeWiki側の対象"],
            ["参照されたプラグインJSON", "Q_PLUGIN_METADATAのFILE、独自キー", "参照・明示指定のあるJSON等を選別"],
            [".qrc / 設定 / 参考文書", "リソースURL・alias、設定参照、既存説明", "qrc等を補完。画像実体の意味解析は対象外"],
            [".pro / .pri / .prf", "qmake用の構成ファイルがあること", "参考保存・更新検知。条件評価・実行なし"],
        ]),
        col_widths=[160, 300, CW - 460],
        bottom="前提はQt 5とqmakeです。.proの細かな設定、Qt SDK、ビルド結果がなくても開始できます。テスト・第三者コード・生成物の除外は設定で調整します。",
        note="JSON・INI・XML・YAML等を拡張子だけで全件収集しません。読める参照かinclude_filesで範囲を指定します。",
        sources=["src/cpp_scan/collector.py", "src/cpp_scan/config.py", "config.cpp-source.example.yaml"],
    ),
    dict(
        title="棚卸し：まず、対象と除外を確認する", tag="03 / cpp-scan scan",
        lead="最初のscanは対象フォルダの一覧を調べる処理です。原本コピーや本格的な解析を始める前に、収集範囲を確認できます。",
        diagram="inventory",
        cards=[
            ("対象を選ぶ", ["明示したソースルートから再帰的に探します。", "C++・画面・参照設定等を種類別に記録。", "WidgetsとQMLの両方を検出します。"]),
            ("除外した理由も残す", ["ビルド出力、moc_・ui_等の生成物を除外。", "テスト・第三者コードは既定で除外します。", "リンク先へ無制限に探索しません。"]),
            ("読めない場合を区別する", ["一覧取得・読み取りに失敗した場所を記録。", "接続不能を、対象が0件の成功にしません。", "ソース本文を端末へ無制限に出しません。"]),
        ],
        note="scanの結果は棚卸し用JSONです。原本スナップショットは次のanalyzeで作成し、scanではLLM・DBを使いません。",
        sources=["src/cpp_scan/collector.py", "src/cpp_scan/pipeline.py", "src/cpp_scan/cli.py"],
    ),
    dict(
        title="原本固定：いつのコードか、後から戻れるように", tag="04 / スナップショットと文字コード",
        lead="analyzeは原本バイト列を保管し、解析用UTF-8コピーを作ります。日本語のCP932ソースでも、原本位置との対応を残します。",
        diagram="snapshot",
        cards=[
            ("原本を保持する", ["内容全体のSHA-256と相対パスを記録。", "取得中に変化した原本は再試行します。", "元フォルダと保存した原本は書き換えません。"]),
            ("解析用コピーへ変換する", ["UTF-8を優先し、CP932への切替を記録。", "ファイルごとの文字コード指定も可能です。", "原本の行・バイト位置との対応表を保持。"]),
            ("一覧と内容を再照合する", ["収集の前後で対象一覧・内容を確認します。", "変化やデコード失敗は部分失敗として記録。", "読めない場合も取得できた原本は保持します。"]),
        ],
        note="通常フォルダの二度の照合は、全ファイルが同一時点の版である保証ではありません。固定したGitアーカイブ等の入力を推奨します。",
        sources=["src/cpp_scan/snapshot.py", "src/cpp_scan/text.py", "src/file_scan/storage.py"],
    ),
    dict(
        title="CodeWiki：コードの骨組みを取り出す", tag="05 / LLMを使わない構造解析",
        lead="固定版CodeWikiの構造解析APIで、クラスや関数、依存の候補と位置を取得します。日本語の説明を生成する工程は後に分けます。",
        diagram="structure",
        cards=[
            ("構文から要素を取り出す", ["クラス・関数・メソッド等を索引化します。", "CodeWikiの位置を固定原本へ対応付けます。", "依存・呼出先の候補と未解決を保持します。"]),
            ("型・動作は別途確認する", ["初期版ではClangやコンパイルDBを必須にしません。", "ビルド条件・型・オーバーロードの完全解決は対象外。", "コード上の構造を、実行済みの動作とは扱いません。"]),
            ("解析範囲を可視化する", ["構文エラー候補や未索引のファイルを記録。", "一部を読めても、全構文の理解を意味しません。", "構造を取り出せないエラーは部分失敗にします。"]),
        ],
        note="CodeWiki 2.0.0の採用コミットを検査して別Python環境で実行。標準のcodewiki generateを、ローカル解析だけのコマンドとは扱いません。",
        sources=["src/cpp_scan/codewiki.py", "src/cpp_scan/codewiki_bridge.py", "src/cpp_scan/pipeline.py"],
    ),
    dict(
        title="Widgets：画面の項目と、操作の接点を拾う", tag="06 / .uiと手書きC++",
        lead=".uiのXMLには部品名や表示文字、接続宣言があります。手書きC++の接続式・Slotと照合するための手掛かりを整理します。",
        diagram="widgets",
        cards=[
            ("画面の宣言を読む", ["画面クラス、widget、objectNameを抽出。", "ボタン文字、タイトル等の一部の属性を取得。", ".uiに書かれた明示接続も保存します。"]),
            ("C++との対応は候補にする", ["setupUi等の呼出を対応付けの手掛かりに。", "on_部品名_シグナル名の自動接続も候補。", "Slot実装と実際の成立はレビューで確認します。"]),
            ("生成ヘッダーを主対象にしない", ["ui_*.hは既定で生成物として除外します。", "画面XMLと手書きソースを優先します。", "生成物しかない場合の欠落は確認が必要です。"]),
        ],
        note="図は接続宣言を読む例です。startButtonのクリックからMainWindow::startが実行されることまで、自動で保証するものではありません。",
        sources=["src/cpp_scan/qt.py", "examples/qt5-source/main_window.ui", "src/cpp_scan/collector.py"],
    ),
    dict(
        title="Signal／Slot：接続式と、動作の成立を分ける", tag="07 / Qtのイベント連携",
        lead="Qtの接続には、関数ポインタ、SIGNAL／SLOTマクロ、lambda等の書き方があります。まず接続式の存在と引数を記録します。",
        diagram="signals",
        cards=[
            ("複数の書き方を拾う", ["釣り合う括弧の呼出式から引数を読みます。", "コメントや文字列内の見せかけは除きます。", "Signal／Slot宣言も候補として整理します。"]),
            ("条件を残す", ["読み取れるプリプロセッサ条件を併記。", "実行時にどの分岐を通るかは評価しません。", "相手の型や複雑な式は追加確認が必要です。"]),
            ("仕様書で読み分ける", ["式が書かれていることは原本で確認できます。", "送信先の対応・到達する処理は候補です。", "動作を確定するには実装照合や実行確認へ。"]),
        ],
        note="接続の数や記述が取得できても、全呼出の実行順・並行動作・到達性を解析し終えたことにはなりません。",
        sources=["src/cpp_scan/text.py", "src/cpp_scan/qt.py", "tests/test_cpp_scan.py"],
    ),
    dict(
        title="QML：画面側と、公開したC++の接点を見る", tag="08 / QMLとC++の連携候補",
        lead="QMLのimport・property・イベント記述と、C++の型登録やcontext propertyを並べ、画面と処理を照合する手掛かりにします。",
        diagram="qml",
        cards=[
            ("QML側の記述", ["import、id、property、signal等を抽出。", "onXxxやConnectionsの記述を候補で保持。", "qmldirのmodule・plugin宣言も記録します。"]),
            ("C++側の公開情報", ["setContextPropertyやqmlRegister系を検出。", "Q_PROPERTY、Q_INVOKABLE等も補完します。", "同じ名前でも、同じ実体かは要確認です。"]),
            ("完全な実行評価は行わない", ["QMLは字句ベースの部分的な補完です。", "bindingやJavaScriptを実行して追跡しません。", "未対応・部分対応を網羅状況へ残します。"]),
        ],
        note="ui_modeの事前回答を実行条件にせず、Widgets／QMLの両方を検出します。初期版のui_mode指定は検出対象を絞るスイッチではありません。",
        sources=["src/cpp_scan/qt.py", "src/cpp_scan/config.py", "examples/qt5-source/Main.qml"],
    ),
    dict(
        title="プラグイン：入口・契約・読込箇所を整理する", tag="09 / 多数のサブ機能を読む",
        lead="宣言クラス、共通インターフェース、参照JSON、ローダーの記述を拾います。どの機能に所属し、いつ動くかは候補として照合します。",
        diagram="plugins",
        cards=[
            ("入口と契約を確認する", ["Q_PLUGIN_METADATA等の宣言を抽出。", "Q_DECLARE_INTERFACE・Q_INTERFACESも保持。", "IIDは契約の識別子で、実装の一意IDではありません。"]),
            ("JSONとローダーも読む", ["FILEで参照するメタデータを収集します。", "QPluginLoader・QLibrary等の候補を検出。", "JSONのキー名だけで依存や順序を確定しません。"]),
            ("独自方式は未確認を残す", ["一部のFactory登録名を候補として検出。", "動的パスや独自マクロは追い切れない場合があります。", "所属・有効化・初期化順は原本と照合します。"]),
        ],
        note="DLLをロードしたり、プラグインを起動したりする処理ではありません。CodeWikiのモジュール分割と実プラグイン境界の一致も未確認です。",
        sources=["src/cpp_scan/qt.py", "examples/qt5-source/example_plugin.h", "src/cpp_scan/collector.py"],
    ),
    dict(
        title="文書生成：AIの説明と、Qtの抽出資料をまとめる", tag="10 / cpp-scan generate",
        lead="指定した解析版を入力に、CodeWikiとLLMで日本語の説明を生成します。独立したQt補完資料を組み合わせ、根拠と未確認事項を付けます。",
        diagram="generation",
        cards=[
            ("CodeWikiが説明を作る", ["モデル・接続先・キーを環境変数で指定します。", "コードや識別子は原文、日本語で本文を生成。", "元リポジトリや利用者の設定を上書きしません。"]),
            ("Qt補完を別の資料として合流", ["画面・プラグイン・接続・API等を表へ整形。", "補完JSONがCodeWikiへ自動統合される前提にはしません。", "全体・未確認事項・ファイル別資料も生成します。"]),
            ("完成した一式を検証する", ["必須ファイル、UTF-8、ハッシュ、リンクを検査。", "抽出根拠の原本パス・行・バイト範囲を照合。", "説明の意味の正しさはレビュー対象です。"]),
        ],
        note="CodeWiki本文は推定を含む未レビュー文書です。Mermaidの外部検証は無効にしており、図が出ても検証済みとは表示しません。",
        sources=["src/cpp_scan/codewiki_bridge.py", "src/cpp_scan/documents.py", "src/cpp_scan/pipeline.py"],
    ),
    dict(
        title="品質：根拠・候補・推定を、読み分ける", tag="11 / 仕様書を信頼するための確認",
        lead="処理が成功したことと、説明をレビューしたことは別です。原本で確認した記述から、どこまで解釈したかを分けて読みます。",
        table=(["区分", "例", "読み取れる範囲"], [
            ["原本の記述／source_fact", "Q_PLUGIN_METADATAの宣言がある", "記述自体と原本位置を確認。実行時の成立は別"],
            ["対応候補／candidate", "この画面のSlotに対応しそう", "字句や名前からの候補。型や実体を照合する"],
            ["未解決／unresolved", "依存・呼出先の相手を特定できない", "未解決として保持。存在しないと結論付けない"],
            ["推定を含む説明", "CodeWikiが説明した機能や処理の意図", "AI説明の意味は未確認。根拠と人手で照合する"],
            ["レビュー状態", "unreviewed／reviewed", "抽出の確実性と別の概念。現処理は未レビューで生成"],
        ]),
        col_widths=[175, 250, CW - 425],
        bottom="根拠には固定原本のパス、SHA-256、文字コード、開始・終了行、必要なバイト範囲を保存します。AIの各文へ根拠が自動対応する保証はなく、未対応の説明は未確認として扱います。",
        note="根拠位置の整合性検査は、業務要件・設計意図・AI説明の正しさを保証しません。実行確認済みの状態も自動付与しません。",
        sources=["src/cpp_scan/qt.py", "src/cpp_scan/documents.py", "docs/design/09-cpp-qt-source-ingestion-spec.md"],
    ),
    dict(
        title="受渡し：完成Markdownを、既存取込へ渡す", tag="12 / cpp-scan ingest",
        lead="新しい処理の出口をMarkdownに揃えることで、既存の文書変換・チャンク化・DB登録をそのまま活用します。",
        diagram="handoff",
        cards=[
            ("専用フォルダへ完成版だけ配置", ["世代とハッシュを検証してhandoff/currentを更新。", "C++原本や中間JSONを混ぜません。", "ソース・ナレッジのIDと文書パスを固定します。"]),
            ("既存の取込処理を呼ぶ", ["file_scan.pipeline.ingestで抽出・分割・登録。", "既存の原本版・抽出世代・キャッシュを使います。", "ソース解析用のDBスキーマ追加はありません。"]),
            ("DBとの対応を記録する", ["生成版・文書ID・選択抽出世代を照合。", "取込レポートを受渡し履歴へ残します。", "同じDBのstorage_dirは既存ルートに揃えます。"]),
        ],
        note="登録はdraftです。DBの確定単位は1文書であり、全仕様書の同時切替やDB全体の自動ロールバックは保証しません。",
        sources=["src/cpp_scan/handoff.py", "src/file_scan/pipeline.py", "config.cpp-docs.example.yaml"],
    ),
    dict(
        title="ベクトル化：仕様書の文章を、検索につなぐ", tag="13 / 既存のEmbeddingと検索公開",
        lead="ソースから作った仕様書も、既存の文書と同じ経路で分割・ベクトル化します。公開後は説明文を根拠として検索できます。",
        diagram="embedding",
        cards=[
            ("何をベクトル化するか", ["対象は仕様書のチャンクと見出し等の検索文脈。", "関数のASTを直接ベクトルDBへ登録する経路ではありません。", "既存のモデル・次元数・入力キャッシュを使います。"]),
            ("別のコマンドで実行する", ["確認後にfile-scan embedを実行します。", "生成・下書き取込だけでAPI送信は始まりません。", "仕様書のsearch_textをAPIへ送って数値化します。"]),
            ("公開してから検索する", ["file-searchで文書ID・DB抽出世代・業務版を指定。", "検索の引用先は生成Markdownです。", "本文のソース参照を手掛かりに原本を照合します。"]),
        ],
        note="公開の抽出世代IDはcpp-scanの生成IDとは別です。図の数値・質問は説明用で、Embeddingが推定や誤記を修正するわけではありません。",
        sources=["src/file_scan/embeddings.py", "src/file_scan/chunking.py", "docs/implementation/search-api.md"],
    ),
    dict(
        title="再実行：原本と条件を、版ごとに照合する", tag="14 / 再利用と変更検知",
        lead="入力や条件が同じなら完成済みの成果物を検証して再利用し、変わった場合は新しい解析・生成版を作ります。",
        diagram="versions",
        cards=[
            ("何を変更として見るか", ["ファイル一覧・内容・対象範囲・文字コード。", "CodeWiki・解析ルール・モデル等の条件。", "コメント・画面・.proだけの変更も検知します。"]),
            ("初期版の更新単位", ["変更時は対象プロジェクトを再解析・再生成。", "CodeWikiの--updateには依存しません。", "--forceで同じ条件でも新しい版を作れます。"]),
            ("既存取込側も再利用する", ["文書ID・配置先を毎回変えずに更新します。", "同じMarkdown・変換条件は既存世代を再利用。", "同じ検索入力・モデルのベクトルも再利用します。"]),
        ],
        note="スナップショットは内容等から識別し、解析・生成版は別IDで保存します。日時や実行IDを文書ファイル名へ増殖させる方式ではありません。",
        sources=["src/cpp_scan/pipeline.py", "src/cpp_scan/snapshot.py", "src/file_scan/profiles.py"],
    ),
    dict(
        title="操作：棚卸しから検索まで、段階を分けて実行", tag="15 / コマンドの早見表",
        lead="Sはソース解析用設定、Dは生成文書の取込設定です。解析IDと生成IDは、前のコマンドが返した実際の値を指定します。",
        table=(["順序", "コマンド例・公開操作", "主な結果・接続"], [
            ["事前準備", "file-scan init-db --config D", "既存DBを初期化。準備済みなら不要"],
            ["1. 棚卸し", "cpp-scan scan --config S", "一覧・除外・失敗。LLM／DBなし"],
            ["2. 解析", "cpp-scan analyze --config S", "原本固定・構造・Qt。LLM／DBなし"],
            ["3. 生成", "cpp-scan generate --config S --analysis analysis-...", "日本語仕様書。指定LLMへ送信"],
            ["4. 下書き取込", "cpp-scan ingest --config S --generation gen-...", "既存処理でDB登録。Embeddingなし"],
            ["5. ベクトル化", "file-scan embed --config D", "設定で有効な場合にEmbedding APIへ"],
            ["6. 公開", "file-searchのcreate-release・publish", "業務版・文書・抽出世代等の引数は検索手順へ"],
            ["状態確認", "cpp-scan status --config S", "工程履歴・選択版・受渡し。DB接続なし"],
        ]),
        col_widths=[98, 380, CW - 478],
        bottom="S＝config.cpp-source.local.yaml、D＝config.cpp-docs.local.yaml。...は仮表記です。モデルの環境変数、CodeWiki専用Python、文字辞書キャッシュ、既存DBの接続・保管先を事前に設定します。",
        note="操作の詳細は実装手順へ。終了コードは0＝成功、1＝工程エラー、2＝部分失敗、130＝中断。statusはDB内の最新状態を直接照会するコマンドではありません。",
        sources=["src/cpp_scan/cli.py", "docs/implementation/cpp-source-ingestion.md", "src/search/cli.py"],
    ),
    dict(
        title="失敗時：欠落を明示し、未完成の受渡しを止める", tag="16 / 運用と復旧の考え方",
        lead="読取不能・構文エラー・タイムアウトを成功へ置き換えずに記録します。既定では部分失敗した仕様書一式を取込へ渡しません。",
        diagram="failures",
        cards=[
            ("未完成を通常版へ流さない", ["設定・ツールの全体失敗と、個別の部分失敗を区別。", "未解決の候補があるだけで失敗とはしません。", "本文・網羅状況・品質レポートを併せて確認します。"]),
            ("部分取込を明示した場合", ["allow_partial_ingest=trueで下書きとして許可。", "欠けた文書は直前のMarkdownで保持します。", "retained_pathsと、残った旧版の根拠を確認します。"]),
            ("変更・中断を管理する", ["範囲変更は専用オプションで意図を明示します。", "排他と履歴で配置中断・再実行を管理。", "DB開始後、ファイルだけ旧版へ戻しません。"]),
        ],
        note="LLMの期限・応答長・エージェント単位の回数を制限できます。プロジェクト全体の金額上限が必要な場合はAPI側の予算制限も使います。",
        sources=["src/cpp_scan/handoff.py", "src/cpp_scan/store.py", "src/cpp_scan/pipeline.py"],
    ),
    dict(
        title="詳しく読む：操作手順と、現在の実装の範囲", tag="17 / 根拠資料と読み順",
        lead="このガイドは処理の仕組みを説明する資料です。導入条件・設定・個別の制限は、実装手順と各モジュールで確認できます。",
        table=(["順序", "参照先", "確認すること"], [
            ["1", "docs/implementation/cpp-source-ingestion.md", "導入・操作・対応範囲・社内受入確認"],
            ["2", "config.cpp-source.example.yaml / config.cpp-docs.example.yaml", "専用設定と、既存文書取込への接続"],
            ["3", "src/cpp_scan/cli.py / config.py / collector.py", "コマンド・設定・対象と除外"],
            ["4", "src/cpp_scan/snapshot.py / text.py", "原本固定・文字コード・根拠の位置"],
            ["5", "src/cpp_scan/codewiki_bridge.py / qt.py", "固定版CodeWikiの呼出とQt補完"],
            ["6", "src/cpp_scan/pipeline.py / documents.py / handoff.py", "状態・文書検証・既存取込への受渡し"],
            ["7", "src/file_scan/pipeline.py / embeddings.py", "既存の文書取込・分割・ベクトル化"],
        ]),
        col_widths=[48, 400, CW - 448],
        bottom="合成ソース、固定版CodeWiki、ローカル固定応答API、実PostgreSQLで工程間の接続を検証しています。社内実ソースと実モデルの生成品質・費用・検索精度は、社内環境での受入確認が必要です。",
        note="C++・Qtの完全な意味解析、ビルド再現、実行時トレース、プラグイン所属の完全確定、C++原本の直接検索・引用APIは初期版の対象外です。",
        sources=["docs/implementation/cpp-source-ingestion.md", "tests/test_cpp_scan.py", "docs/design/09-cpp-qt-source-ingestion-spec.md"],
    ),
]


def text(drawing, x, y, value, size=11, color=INK, max_width=None):
    if max_width and pdfmetrics.stringWidth(value, "JP", size) > max_width:
        raise ValueError(f"Diagram text overflow: {value}")
    layout.dtext(drawing, x, y, value, size, color)


def box(drawing, x, y, width, height, title, lines=(), *, fill=BLUE, accent=TEAL, size=12):
    drawing.add(Rect(x, y, width, height, rx=8, ry=8, fillColor=fill, strokeColor=None))
    drawing.add(Rect(x, y + height - 4, width, 4, fillColor=accent, strokeColor=None))
    text(drawing, x + 13, y + height - 25, title, size, accent, width - 26)
    for index, line in enumerate(lines):
        baseline = y + height - 47 - index * 17
        if baseline < y + 8:
            raise ValueError(f"Diagram vertical overflow: {title}")
        text(drawing, x + 13, baseline, line, 10.5, INK, width - 26)


def arrow(drawing, x1, y1, x2, y2, **kwargs):
    layout.arrow(drawing, x1, y1, x2, y2, **kwargs)


def flow(items, *, height=144):
    drawing = Drawing(CW, height)
    gap = 27
    width = (CW - (len(items) - 1) * gap) / len(items)
    for i, (title, lines) in enumerate(items):
        x = i * (width + gap)
        box(drawing, x, 20, width, height - 32, title, lines)
        if i + 1 < len(items):
            arrow(drawing, x + width + 3, height / 2, x + width + gap - 3, height / 2)
    return drawing


def diagram(kind):
    if kind == "intro":
        return flow([("C++／Qt 5のソース", ["本体・画面・多数のプラグイン", "どんな処理かを把握したい"]),
                     ("根拠付きの日本語仕様書", ["構造 + Qt補完 + AIの説明", "原本位置・候補・未確認を残す"]),
                     ("既存の取込・検索基盤", ["Markdown → チャンク → DB", "ベクトル化・公開して検索"] )])
    if kind == "pipeline":
        d = Drawing(CW, 146)
        widths = [157, 175, 175, CW - 157 - 175 - 175 - 3 * 24]
        x = 0
        for i, (title, lines) in enumerate([
            ("scan / analyze", ["原本固定・CodeWiki構造", "Qt補完／LLMなし"]),
            ("generate", ["指定LLMで日本語化", "Markdownを検証"]),
            ("cpp-scan ingest", ["既存file-scanを呼ぶ", "分割・DB下書き登録"]),
            ("既存embed / 公開", ["Embedding → pgvector", "明示公開 → 検索"]),
        ]):
            box(d, x, 31, widths[i], 103, title, lines,
                fill=PALE if i >= 2 else BLUE, size=11.5)
            if i < 3:
                arrow(d, x + widths[i] + 3, 80, x + widths[i] + 21, 80)
            x += widths[i] + 24
        text(d, 0, 8, "新設：ソースから文書を作る", 10, TEAL)
        text(d, 380, 8, "再利用：完成した文書の取込・検索", 10, TEAL)
        return d
    if kind == "inventory":
        return flow([("明示したソースフォルダ", ["main.cpp / plugin.h", "画面.ui / 画面.qml", ".pro / 参照された設定"]),
                     ("対象と除外を仕分け", ["対象：C++・画面等", "除外：生成物・第三者等", "失敗：列挙できない場所"]),
                     ("棚卸しレポート", ["ファイル一覧と種類", "除外理由・失敗箇所", "Widgets / QMLの検出"] )])
    if kind == "snapshot":
        d = Drawing(CW, 150)
        box(d, 0, 18, 200, 120, "社内の原本", ["UTF-8 / CP932", "ファイルの変化も確認", "元フォルダは変更しない"])
        arrow(d, 204, 78, 233, 78)
        box(d, 239, 80, 264, 58, "保存した原本バイト列", ["SHA-256・相対パス"], size=11.5)
        box(d, 239, 10, 264, 58, "解析用UTF-8コピー", ["位置対応表と一緒に保存"], size=11.5)
        arrow(d, 508, 110, 535, 110)
        arrow(d, 508, 39, 535, 39)
        box(d, 541, 18, CW - 541, 120, "原本へ戻る手掛かり", ["snapshot ID", "ファイル名・行・バイト範囲", "解析は作業コピー上で実施"])
        return d
    if kind == "structure":
        return flow([("解析用コード", ["class Example { ... }", "void start() { ... }", "別の関数を呼ぶ記述"]),
                     ("CodeWikiの構造解析", ["構文を読む・マクロを回復", "クラス／関数／位置", "依存候補・構文診断"]),
                     ("自前の索引・関係へ整理", ["要素IDと原本位置", "候補／未解決を保持", "実行確認済みとはしない"] )])
    if kind == "widgets":
        d = Drawing(CW, 148)
        box(d, 0, 20, 233, 116, ".uiの画面宣言", ["QPushButton：startButton", "表示文字：「実行」", "clicked()の接続宣言"])
        arrow(d, 237, 79, 261, 79)
        box(d, 267, 20, 223, 116, "Qt補完で抽出", ["objectNameと表示名", "送信元・受信先・Slot", "setupUi等も手掛かり"])
        arrow(d, 494, 79, 519, 79, color=ORANGE, dashed=True)
        box(d, 525, 20, CW - 525, 116, "手書きC++と照合", ["MainWindow::startの実装", "自動接続は候補", "実行時の成立は未確認"], fill=AMBER, accent=ORANGE)
        return d
    if kind == "signals":
        d = Drawing(CW, 142)
        box(d, 0, 23, 236, 107, "記述を拾う", ["connect(sender, &S::changed,", "        receiver, &R::handle)", "マクロ形式・lambda形式も"])
        arrow(d, 240, 77, 262, 77)
        box(d, 268, 23, 220, 107, "接続式と引数を記録", ["原本に式があること", "宣言・条件・位置", "相手の対応は候補"])
        arrow(d, 492, 77, 516, 77, color=ORANGE, dashed=True)
        box(d, 522, 23, CW - 522, 107, "実際に処理が動くか", ["型・分岐・寿命・接続状態", "実装照合・実行確認へ", "初期版では自動確定しない"], fill=AMBER, accent=ORANGE)
        return d
    if kind == "qml":
        d = Drawing(CW, 148)
        box(d, 0, 22, 233, 114, "QMLの記述", ["property int currentCount", "Connections { target: backend }", "onCountChanged: ..."])
        arrow(d, 237, 78, 261, 78, color=ORANGE, dashed=True)
        box(d, 267, 22, 223, 114, "公開名を手掛かりに照合", ["setContextProperty", "公開名：backend", "qmlRegister系の型登録"])
        arrow(d, 494, 78, 519, 78, color=ORANGE, dashed=True)
        box(d, 525, 22, CW - 525, 114, "C++の公開情報", ["Q_PROPERTY", "Q_INVOKABLE", "候補として対応を確認"])
        text(d, 0, 5, "破線：名前・記述からの対応候補。bindingやJavaScriptは実行しません。", 9.5, ORANGE)
        return d
    if kind == "plugins":
        d = Drawing(CW, 154)
        box(d, 0, 25, 225, 116, "共通インターフェース", ["Q_DECLARE_INTERFACE", "契約：IExample", "共通のIIDを宣言"])
        arrow(d, 229, 83, 253, 83)
        box(d, 259, 83, 240, 58, "実装A：宣言 + JSON", ["Q_PLUGIN_METADATA等"], size=11.5)
        box(d, 259, 13, 240, 58, "実装B：同じIIDも可能", ["IIDだけを実装IDにしない"], size=11.5)
        arrow(d, 503, 83, 527, 83, color=ORANGE, dashed=True)
        box(d, 533, 25, CW - 533, 116, "ローダーの記述", ["QPluginLoader等", "読込先・条件の手掛かり", "所属・依存・順序は要照合"])
        return d
    if kind == "generation":
        d = Drawing(CW, 154)
        box(d, 0, 84, 237, 58, "CodeWiki + 指定LLM", ["日本語のモジュール説明"], size=11.5)
        box(d, 0, 12, 237, 58, "Qt補完の抽出情報", ["画面・接続・プラグイン"], size=11.5)
        arrow(d, 241, 112, 269, 89)
        arrow(d, 241, 41, 269, 64)
        box(d, 275, 23, 216, 119, "整形と根拠の検査", ["未レビュー・推定を明記", "ファイル・行・リンクを検証", "意味の正しさは別途確認"])
        arrow(d, 495, 83, 521, 83)
        box(d, 527, 23, CW - 527, 119, "日本語Markdown一式", ["全体／画面／プラグイン", "処理／API／未確認事項", "wiki／Qtファイル別資料"])
        return d
    if kind == "handoff":
        return flow([("新処理の出口", ["検証したMarkdown", "専用handoff/currentへ", "固定のIDと相対パス"]),
                     ("既存file-scanの取込", ["文書 → ブロック → チャンク", "原本版・抽出世代を管理", "既存処理で下書き登録"]),
                     ("既存DB・文書保管先", ["DB：winnap内の既存表", "Markdown原本と抽出結果", "生成版と文書世代を対応"] )])
    if kind == "embedding":
        d = Drawing(CW, 146)
        widths = [163, 175, 175, CW - 163 - 175 - 175 - 3 * 24]
        x = 0
        for i, (title, lines) in enumerate([
            ("仕様書Markdown", ["「実行ボタンの接続候補」", "見出し + 本文"]),
            ("チャンクと検索文脈", ["search_textを作成", "既存の分割ルール"]),
            ("Embedding API", ["文章 → [0.12, -0.08, ...]", "pgvectorへ保存"]),
            ("明示公開して検索", ["「どの処理を呼ぶ？」", "生成Markdownを引用"]),
        ]):
            box(d, x, 30, widths[i], 103, title, lines, size=11.5)
            if i < 3:
                arrow(d, x + widths[i] + 3, 80, x + widths[i] + 21, 80)
            x += widths[i] + 24
        text(d, 0, 8, "文書取込", 10, MUTED)
        text(d, 387, 8, "file-scan embed", 10, TEAL)
        text(d, 587, 8, "file-search", 10, TEAL)
        return d
    if kind == "versions":
        return flow([("原本の版", ["ファイル一覧 + 内容ハッシュ", "収集範囲 + 文字コード", "同じ完成版は検証して再利用"]),
                     ("解析・生成の版", ["CodeWiki・Qtルール・モデル", "変更時はプロジェクト再処理", "完成した旧成果物を保持"]),
                     ("既存取込・Embedding", ["固定文書IDで更新", "同じ文書・変換条件を再利用", "同じ検索入力の数値も再利用"] )])
    if kind == "failures":
        d = Drawing(CW, 148)
        box(d, 0, 23, 212, 114, "工程の結果", ["成功／部分失敗／失敗", "失敗・未解決の範囲を記録", "成功とレビュー済みは別"])
        arrow(d, 216, 108, 247, 108)
        box(d, 253, 82, 250, 55, "完成：受渡しの検証へ", ["MarkdownをDB下書き登録"], size=11.5)
        arrow(d, 216, 51, 247, 51, color=ORANGE)
        box(d, 253, 12, 250, 55, "部分失敗：既定で停止", ["欠落を明示・確認して再実行"], size=11.5, fill=AMBER, accent=ORANGE)
        arrow(d, 507, 39, 531, 39, color=ORANGE, dashed=True)
        box(d, 537, 23, CW - 537, 114, "明示的な部分取込", ["欠けた文書は旧版を保持", "retained_pathsを確認", "読取失敗から削除しない"], fill=AMBER, accent=ORANGE)
        return d
    raise ValueError(kind)


def paragraph(value, x, top, width, **kwargs):
    return layout.para(value, x, top, width, **kwargs)


def draw_cards(items, top):
    gap = 18
    width = (CW - 2 * gap) / 3
    bottom = top
    for index, (title, points) in enumerate(items):
        x = M + index * (width + gap)
        C.setStrokeColor(colors.HexColor("#dbe6ec"))
        C.line(x, top, x + width, top)
        y = top - 13
        y -= paragraph(title, x, y, width, size=12.7, leading=19, color=TEAL) + 8
        for point in points:
            C.setFillColor(TEAL)
            C.circle(x + 2, y - 6, 1.4, fill=1, stroke=0)
            y -= paragraph(point, x + 12, y, width - 12, size=10.5, leading=16) + 8
        bottom = min(bottom, y)
        if y < 106:
            raise ValueError(f"Card overflow: {title}: {y:.1f}")
    return bottom


def draw_table(page, top):
    headings, rows = page["table"]
    style = ParagraphStyle("cell", fontName="JP", fontSize=10.2, leading=14.2, wordWrap="CJK")
    header = ParagraphStyle("header", parent=style, textColor=colors.white)
    data = [[Paragraph(escape(value), header) for value in headings]]
    data.extend([Paragraph(escape(value), style) for value in row] for row in rows)
    table = Table(data, colWidths=page["col_widths"], hAlign="LEFT")
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), INK),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, layout.PAPER]),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 11), ("RIGHTPADDING", (0, 0), (-1, -1), 11),
        ("TOPPADDING", (0, 0), (-1, -1), 6), ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("LINEBELOW", (0, 0), (-1, 0), 1, TEAL),
    ]))
    _, height = table.wrap(CW, 1000)
    table.drawOn(C, M, top - height)
    return top - height


def build():
    global C
    PDF.parent.mkdir(parents=True, exist_ok=True)
    IMAGES.mkdir(parents=True, exist_ok=True)
    C = canvas.Canvas(str(PDF), pagesize=(W, H))
    layout.C = C
    C.setTitle("C++・Qtソース取込処理 図解ガイド")
    C.setAuthor("2026-file-scan")
    C.setSubject("Qt 5ソースの棚卸し・原本固定・CodeWiki解析・Qt補完・日本語仕様書・既存取込への接続")
    md = ["# C++・Qtソース取込処理 図解ガイド\n",
          "確認日：2026-11-03。現行実装に基づく、初めて読む方向けの説明資料です。\n",
          "[図解PDFを開く](../../output/pdf/C++・Qtソース取込処理_図解ガイド.pdf)\n",
          "図の画面・コード・質問・数値は説明用です。社内アプリの解析結果や生成品質の測定結果ではありません。\n",
          "## 目次\n"]
    md.extend(f"{number}. {page['title']}" for number, page in enumerate(PAGES, 1))
    bounds, metrics = [], []
    for number, page in enumerate(PAGES, 1):
        C.bookmarkPage(f"page-{number}")
        C.addOutlineEntry(page["title"], f"page-{number}", level=0)
        C.setFillColor(colors.white)
        C.rect(0, 0, W, H, fill=1, stroke=0)
        C.setFillColor(TEAL)
        C.rect(M, H - 36, 34, 4, fill=1, stroke=0)
        paragraph(page["tag"], M + 46, H - 27, CW - 46, size=10, leading=14, color=TEAL)
        if paragraph(page["title"], M, H - 57, CW, size=23, leading=32) > 33:
            raise ValueError(f"Title wraps: {number}")
        lead_height = paragraph(page["lead"], M, H - 102, CW, size=12, leading=20, color=MUTED)
        md.extend([f"\n## {number}. {page['title']}\n", page["lead"] + "\n"])
        if page.get("diagram"):
            drawing = diagram(page["diagram"])
            top = min(H - 141, H - 102 - lead_height - 16)
            y = top - drawing.height
            renderPDF.draw(drawing, C, M, y)
            svg = IMAGES / f"{number:02d}_{page['diagram']}.svg"
            renderSVG.drawToFile(drawing, str(svg))
            svg.write_text(svg.read_text().replace(
                "font-family: JP", "font-family: 'Arial Unicode MS', 'Hiragino Kaku Gothic ProN', sans-serif"))
            bounds.append(dict(page=number, x=M, y=y, w=CW, h=drawing.height, name=svg.stem))
            bottom = draw_cards(page["cards"], y - 13)
            md.append(f"![{page['title']}](images/cpp-source/{svg.stem}.png)\n")
            for title, points in page["cards"]:
                md.extend([f"### {title}\n", *[f"- {point}" for point in points], ""])
        else:
            bottom = draw_table(page, H - 151) - 16
            bottom -= paragraph(page["bottom"], M, bottom, CW, size=11, leading=18)
            if bottom < 106:
                raise ValueError(f"Table overflow: {number}: {bottom:.1f}")
            heads, rows = page["table"]
            md.extend(["| " + " | ".join(heads) + " |", "| " + " | ".join(["---"] * len(heads)) + " |"])
            md.extend("| " + " | ".join(row) + " |" for row in rows)
            md.append("\n" + page["bottom"] + "\n")
        C.setFillColor(PALE)
        C.roundRect(M, 65, CW, 32, 5, fill=1, stroke=0)
        if paragraph(page["note"], M + 10, 89, CW - 20, size=9, leading=12) > 24:
            raise ValueError(f"Note overflow: {number}")
        if paragraph("参照：" + " ／ ".join(page["sources"]), M, 55, CW - 50,
                     size=7.5, leading=10, color=MUTED) > 20:
            raise ValueError(f"Sources overflow: {number}")
        C.setFont("JP", 9)
        C.setFillColor(MUTED)
        C.drawRightString(W - M, 29, f"{number:02d} / {len(PAGES):02d}")
        md.extend(["> " + page["note"] + "\n", "根拠：" + "、".join(
            f"[{source}](../../{source})" for source in page["sources"]) + "\n"])
        metrics.append(dict(page=number, title=page["title"], content_bottom=round(bottom, 1)))
        C.showPage()
    C.save()
    MD.write_text("\n".join(md), encoding="utf-8")
    poppler = shutil.which("pdftoppm")
    if not poppler:
        raise RuntimeError("図のPNG作成にはPopplerのpdftoppmが必要です")
    with tempfile.TemporaryDirectory(prefix="cpp-source-guide-") as directory:
        prefix = Path(directory) / "page"
        subprocess.run([poppler, "-r", "120", "-png", str(PDF), str(prefix)], check=True, capture_output=True)
        for item in bounds:
            with Image.open(Path(directory) / f"page-{item['page']:02d}.png") as rendered:
                sx, sy = rendered.width / W, rendered.height / H
                crop = (int(item["x"] * sx), int((H - item["y"] - item["h"]) * sy),
                        int((item["x"] + item["w"]) * sx), int((H - item["y"]) * sy))
                rendered.crop(crop).save(IMAGES / (item["name"] + ".png"))
    report = ROOT / "artifacts/reports/cpp-source-guide-layout.json"
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(json.dumps(metrics, ensure_ascii=False, indent=2) + "\n")
    print(f"Created {PDF}\nCreated {MD}\nPages: {len(PAGES)} / Diagrams: {len(bounds)}")


if __name__ == "__main__":
    build()
