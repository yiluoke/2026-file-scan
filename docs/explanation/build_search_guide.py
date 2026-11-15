"""検索APIの図解PDF・Markdown・SVG・PNGを生成する。アプリ・DBは変更しない。"""

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
from reportlab.pdfgen import canvas
from reportlab.platypus import Paragraph, Table, TableStyle

import build_guide as layout

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
PDF = ROOT / "output/pdf/検索API_図解ガイド.pdf"
MD = HERE / "検索API_図解ガイド.md"
IMAGES = HERE / "images/search"
W, H, M, CW = layout.W, layout.H, layout.M, layout.CW
INK, TEAL, MUTED, BLUE, PALE = layout.INK, layout.TEAL, layout.MUTED, layout.BLUE, layout.PALE
ORANGE = layout.ORANGE
WARM = colors.HexColor("#fff3e4")
box, arrow, text = layout.box, layout.arrow, layout.dtext

PAGES = [
    dict(
        title="検索APIを、図で理解する", tag="はじめて読む方へ",
        lead="質問に合う公開済みの文章を探し、原本を確かめる手掛かりと、資料の準備状態を返します。",
        diagram="intro",
        cards=[
            ("この資料でわかること", ["どの資料を検索してよいか、どう決めるか。", "意味・言葉・識別子の3つで、どう探すか。", "根拠と出典、不足をどう呼出元へ渡すか。"]),
            ("まず覚える3つの言葉", ["チャンク：検索に使う小さな原文のまとまり。", "公開コピー：管理者が確認して採用した内容。", "coverage：予定した資料が準備できている状態。"]),
            ("読む順番", ["全体像と公開対象：2〜4ページ。", "探し方・順位・出典・不足：5〜11ページ。", "API・更新・試験・配置：12〜16ページ。"]),
        ],
        note="確認日：2026-11-15。図の文書・質問・順位・数値は、現行の仕組みを説明するための例です。",
        sources=["src/search/api.py", "src/search/service.py", "docs/implementation/search-api.md"],
    ),
    dict(
        title="全体像：質問から、根拠と状態を返す", tag="01 / 質問の流れ",
        lead="検索APIは関連する原文を探します。会話の回答文を組み立てる処理は、Difyなどの呼出側で行います。",
        diagram="overview",
        cards=[
            ("1. 質問を受ける", ["ナレッジ・質問・返却件数などをJSONで受けます。", "Bearer接続キーと、入力の形式・上限を検査します。", "検索対象の業務版はサーバー側で確定します。"]),
            ("2. 公開済みの根拠を探す", ["対象チャンクがある場合に、質問を数値化します。", "意味・全文・識別子で探し、候補の順位を統合します。", "承認した公開コピーの本文・位置を取得します。"]),
            ("3. 呼出側へ返す", ["原文・出典・関連する文脈を返します。", "未公開や更新待ちなどの不足も状態として返します。", "Difyは根拠と状態から回答の流れを決めます。"]),
        ],
        note="検索時に共有フォルダ全体を読み直したり、足りない文書のEmbeddingを自動生成したりする処理ではありません。",
        sources=["src/search/api.py", "src/search/service.py", "src/search/repository.py"],
    ),
    dict(
        title="公開：取込済みと、検索できるを分ける", tag="02 / 下書きから公開コピーへ",
        lead="ingest・embedの完了だけでは検索しません。管理者が業務版と採用世代を決め、publishで公開します。",
        diagram="publication",
        cards=[
            ("取込側で準備する", ["ingestが原本を解析し、下書きのチャンクを作ります。", "embedが検索用テキストを数値化して保存します。", "取込元の世代・本文はdraftのまま維持します。"]),
            ("管理者が明示する", ["create-releaseで業務版を作り、予定対象を登録。", "publishで文書・世代・承認者を指定します。", "部分抽出は不足を確認し、個別に公開承認します。"]),
            ("検索側へ凍結する", ["本文・位置・関係・一致するベクトルをコピーします。", "公開コピーと採用先の切替を、DBへ一括登録します。", "公開後にembedした場合は、再publishが必要です。"]),
        ],
        note="初回の業務版は既定です。既定版の切替は--make-default、公開の取り消しはunpublishを使います。",
        sources=["src/search/management.py", "src/search/schema.py", "docs/implementation/search-api.md"],
    ),
    dict(
        title="検索前に、対象範囲と条件を確かめる", tag="03 / 範囲を先に決める",
        lead="質問者が送った条件をそのままSQLへつなげず、許可した項目と、サーバーの設定から検索範囲を作ります。",
        diagram="scope",
        cards=[
            ("接続と入力を検査", ["Bearerキーが違えば401。未指定でも検索しません。", "質問は通常モードで4,000文字・2,048トークン以内。", "top_kは1〜20。未知の入力項目は422で拒否します。"]),
            ("ナレッジと業務版を確定", ["設定ナレッジ以外や未設定の範囲は404になります。", "現行検索は既定の業務版を使います。", "履歴は起動時の許可と、対象版の明示が必要です。"]),
            ("絞り込みは全経路で共通", ["文書ID・機能IDなど、許可した項目で絞れます。", "条件の組合せはAND。機能ラベルは公開時に付与。", "ラベル未整備の範囲への機能条件は422です。"]),
        ],
        note="初版は文書ACL・利用者別SSOを適用しません。接続キーは質問者ごとの閲覧権限を証明するものではありません。",
        sources=["src/search/models.py", "src/search/repository.py", "src/search/api.py"],
    ),
    dict(
        title="意味検索：質問と文章の、話題の近さを比べる", tag="04 / pgvectorの経路",
        lead="質問を保存済みのベクトルと同じモデル・次元数で数値化し、言い方が違っても近い話題の文章を探します。",
        diagram="semantic",
        cards=[
            ("質問だけを数値化する", ["公開・絞り込み条件に合うチャンクがあるとき、質問をOpenAIへ送ります。", "採用済みプロファイルのモデル・次元数を使います。", "対象がゼロのときは、外部APIを呼びません。"]),
            ("近さから候補を作る", ["cosine類似度が正で、設定した下限以上の行を採用。", "下限はsearch.semantic_min_cosine_similarity。既定は0.0です。", "pgvectorで近い順に最大50候補を選びます。"]),
            ("外部APIの待ちを分ける", ["質問Embeddingは4秒以内、自動再試行0回。", "外部APIを待つ間は、DB接続を返却します。", "Embeddingの失敗は検索障害として呼出元へ返します。"]),
        ],
        note="図は概念例です。意味検索の下限は候補選びに使い、統合後のscore_thresholdは別に適用します。",
        sources=["src/search/service.py", "src/search/repository.py", "src/search/config.py"],
    ),
    dict(
        title="全文検索：日本語の言葉で、本文を探す", tag="05 / PGroongaの経路",
        lead="質問を検索語へ分け、公開チャンクの検索用テキストに、その言葉が含まれるかをPGroongaで調べます。",
        diagram="lexical",
        cards=[
            ("質問から検索語へ", ["TokenBigramとNormalizerAutoで分割・正規化します。", "重複を除いた先頭16語まで。各2〜128文字です。", "図の語の並びは説明用で、分割結果の実測ではありません。"]),
            ("言葉が含まれる本文を取得", ["複数の語をORで検索します。全語一致とは限りません。", "検索語はリテラル値として渡し、式として解釈させません。", "この経路でも最大50候補を得ます。"]),
            ("意味検索を補う", ["業務用語や、本文に書かれた表現を拾う手掛かりです。", "検索対象はsearch_text。返す引用はcontent_textです。", "語の選び方と業務用語の精度は、実質問で評価します。"]),
        ],
        note="全文・識別子の必須索引が欠落または無効な場合は、通常モードのhealthと検索が503を返します。",
        sources=["src/search/repository.py", "src/search/schema.py", "docs/implementation/search-api.md"],
    ),
    dict(
        title="識別子検索：E001のような名前を、そのまま照合", tag="06 / コード・名前の経路",
        lead="文書中のエラーコードや名前を公開時に登録し、質問から抜き出した識別子と、文字列で完全一致させます。",
        diagram="identifier",
        cards=[
            ("公開時に登録する", ["チャンクの保存済み識別子と、本文等から補った名前を使います。", "例：エラーコードE001、文書中の英数字の名前。", "識別子一覧を検索用の公開チャンクへ保持します。"]),
            ("質問から取り出す", ["英数字の名前や、バッククォートで囲んだ表現を抽出。", "質問から使う識別子は最大16個です。", "大文字・小文字や記号を保ち、配列の値と照合します。"]),
            ("他の経路と合わせる", ["一致する識別子の数などから候補の順番を作ります。", "この経路も最大50候補で、順位統合へ渡します。", "E001の一致と、文書が答えとして正しいことは別です。"]),
        ],
        note="図は識別子経路だけの例です。他の経路の候補も統合されます。プログラムの構造解析をする検索APIではありません。",
        sources=["src/search/ranking.py", "src/search/repository.py", "src/search/management.py"],
    ),
    dict(
        title="順位統合：3つの順番を、1つにまとめる", tag="07 / RRFによる統合",
        lead="意味・全文・識別子は点数の尺度が違います。各経路の順位を使い、複数の経路で上位の根拠を押し上げます。",
        diagram="rrf",
        cards=[
            ("点数を直接足さない", ["各経路は同じ候補でも、違う尺度で評価します。", "RRFは1 / (60＋順位)を経路ごとに足します。", "経路内の重複は除き、同じチャンクをまとめます。"]),
            ("0〜1の互換スコアにする", ["3経路で全て1位の上限を使って正規化します。", "score_thresholdは、この統合後スコアに適用します。", "質問ごとの最高点を必ず1にする方式ではありません。"]),
            ("返却量をさらに整える", ["最終結果には関連文脈も含み、top_k以内にします。", "同じ文書からのヒットは最大3件に抑えます。", "スコアは正答確率でも、cosine距離そのものでもありません。"]),
        ],
        note="A〜Dは説明用のチャンクです。この例ではBが3経路で上位となり、統合後の先頭に来ます。実測の順位ではありません。",
        sources=["src/search/ranking.py", "src/search/evidence.py", "src/search/models.py"],
    ),
    dict(
        title="根拠づくり：近くの文脈も、一緒に返す", tag="08 / ヒットと補助文脈",
        lead="短いヒットだけでは意味が伝わらない場合に備え、保存済みの見出しや関係を使って、補助文脈を付けます。",
        diagram="context",
        cards=[
            ("必要な範囲だけ広げる", ["同じ公開コピー内で、親・見出し・表ヘッダー等を探します。", "確認済み関係や近隣も候補にし、1段だけ取得します。", "関係のない文書全体や、全シートへは展開しません。"]),
            ("ヒットと文脈を区別する", ["evidence_roleでhitとcontextを区別します。", "文脈には、どのヒットを補うかのIDを付けます。", "文脈も返却件数に含め、文脈枠は最大5件です。"]),
            ("引用の予算を守る", ["1件2,000、全体6,000トークン。JSONは256KiBまで。", "原文を切り出す場合も、元の文字範囲を保持します。", "追跡可能な形で収まらない表は落とし、次候補を試します。"]),
        ],
        note="APIが本文を生成要約へ置き換える処理ではありません。文脈も原本に対応する文章として返します。",
        sources=["src/search/evidence.py", "src/search/config.py", "src/search/management.py"],
    ),
    dict(
        title="出典：根拠の位置と、原本・画像を確認する", tag="09 / 引用と保管ファイル",
        lead="どの文書・版・ブロックが根拠かを示します。原本と関連画像は、検索APIの接続キーを使って取得できます。",
        diagram="source",
        cards=[
            ("引用を追跡する", ["文書・世代・公開コピー・原本版のIDを返します。", "原文の文字範囲はsource_spansで示します。", "画像説明はartifact_typeがderivedとなります。"]),
            ("原本の位置を示す", ["TXT/MDは行、Excelはシート・セル範囲を使います。", "Wordは段落や表番号、PDFはページ・領域座標です。", "PDFの座標やセルを、引用文字だけの位置へ推測で狭めません。"]),
            ("原本と関連画像を取得する", ["原本はsource_ref、関連画像はimage_refで取得。", "採用中の公開コピーと、サイズ・SHA-256を検証します。", "履歴のリンクには?mode=historyが付きます。"]),
        ],
        note="図のセル位置と本文は例です。接続キーはURLに含めず、Authorizationヘッダーで送ります。",
        sources=["src/search/evidence.py", "src/search/api.py", "src/file_scan/storage.py"],
    ),
    dict(
        title="検索結果と、資料の準備状態を分ける", tag="10 / coverageを読む",
        lead="ヒットがあっても、調べる予定の資料が揃っているとは限りません。根拠の配列と、資料の不足を別に返します。",
        diagram="coverage",
        cards=[
            ("complete：準備完了", ["予定範囲で、必要な公開・索引等が揃っています。", "ヒットが0件なら、条件に合う根拠が返らなかった状態です。", "準備完了でも、世の中に答えがないと証明はできません。"]),
            ("incomplete：不足あり", ["更新待ち、失敗、未収集、partial、ベクトル不足等を数えます。", "例のAがヒットしても、B・Cの不足を残します。", "文書や機能で絞っても、予定対象の不足を消しません。"]),
            ("unknown：確認できない", ["原本の列挙が未確認など、台帳の状態が不明です。", "不明な件数を0として正常に見せません。", "呼出側は不足を伝え、返った根拠の範囲で扱います。"]),
        ],
        note="result_stateはmatched / no_match / budget_exhausted。coverage_stateとは別軸で、空結果と障害も区別します。",
        sources=["src/search/repository.py", "src/search/service.py", "src/search/models.py"],
    ),
    dict(
        title="APIの入口：状態付きと、Dify互換を選ぶ", tag="11 / 呼出元とのJSON契約",
        lead="状態付きAPIを主経路にすると、ヒットと不足を一緒に扱えます。Dify標準連携には互換APIも用意しています。",
        col_widths=[225, 250, CW - 475],
        table=(["入口", "返すもの・用途", "判断のポイント"], [
            ["POST /search-with-status", "records＋retrieval_status。主経路向け", "不足があっても状態を付けた200を返せる"],
            ["POST /retrieval", "recordsのみ。Dify External Knowledge互換", "coverage未完了・返却予算不足は409"],
            ["GET /sources/{公開ID}", "採用中の公開コピーから保管原本を取得", "Bearer。履歴は?mode=history"],
            ["GET /sources/{公開ID}/images/{出現ID}", "公開コピーに含まれる画像を添付で取得", "原本と同じ認証・業務版の条件"],
            ["GET /healthz", "DB・検索管理版・拡張・必須索引を検査", "公開資料やEmbeddingの確認は別途実施"],
            ["/docs・/openapi.json", "入力・出力・Bearer認証のAPI定義", "運用時は必要な範囲だけから閲覧する"],
        ]),
        bottom="recordsの基本項目はcontent（原文）、score（統合順位の点数）、title、metadata（出典等）。正常0件、資料不足、実行障害を呼出側で区別します。",
        note="接続認証401、入力422、同時実行上限429、検索障害503、期限超過504。条件や設定を無視して正常結果にはしません。",
        sources=["src/search/api.py", "src/search/models.py", "src/search/errors.py"],
    ),
    dict(
        title="更新・削除・公開取り消し", tag="12 / 版の切替と鮮度",
        lead="原本と選択世代が変わると、古い公開コピーを現行検索から外します。新しい内容の確認と再公開を待ちます。",
        diagram="freshness",
        cards=[
            ("更新を取込が検出", ["原本版・選択世代・変換設定・更新時刻等を照合します。", "古い公開コピーの本文は、そのまま凍結して保持します。", "現行では古い成功内容へ自動で戻りません。"]),
            ("検索中の一貫性を守る", ["根拠とcoverageは同じDBスナップショットで読みます。", "返す直前にも、採用版や対象の変化を再確認します。", "途中で切り替わった結果は、503等で再検索を促します。"]),
            ("削除と公開取り消し", ["削除・利用不能・対象外の資料は履歴でも除外します。", "unpublishで指定版の採用を解除し、公開コピーを保持します。", "解除後は検索・原本・画像の取得から外れます。"]),
        ],
        note="DBへ反映されるまでの時間は取込の起動頻度等に依存します。更新検出から再公開までの運用も決めます。",
        sources=["src/search/repository.py", "src/search/service.py", "src/search/management.py"],
    ),
    dict(
        title="テストモード：DBなしで、接続だけ試す", tag="13 / 固定応答での試験",
        lead="設定ファイルのsearch.test_modeをtrueにして再起動すると、検索の代わりに固定のテスト文言を返します。",
        diagram="test_mode",
        cards=[
            ("設定で切り替える", ["search配下にtest_mode: trueを記載します。", "falseまたは未指定なら、通常検索を行います。", "HTTPリクエストからモードは切り替えられません。"]),
            ("外部処理を呼ばない", ["DBエンジン・検索サービス・質問Embeddingを使いません。", "トークナイザーの取得や監視表への書込みもしません。", "接続キーとJSON形式・文字数・本文上限は検査します。"]),
            ("実検索と区別する", ["両検索APIで、固定文言を1件返します。", "metadata.test_mode=true。状態はexecution_mode=test。", "healthもDBを使わず、原本・画像の取得は404です。"]),
        ],
        note="接続試験であり、検索精度・資料の公開準備・実Embeddingの検査ではありません。通常モードへ戻して受入確認します。",
        sources=["src/search/config.py", "src/search/stub.py", "config.search-test.example.yaml"],
    ),
    dict(
        title="配置と設定：取込と検索は、並列の機能", tag="14 / ソース・DB・共通設定",
        lead="本番例のルートは/opt/file-scan/です。ソースは直下のsrc/、共通設定と環境変数ファイルはルート直下に置きます。",
        diagram="configuration",
        cards=[
            ("ソースと役割", ["src/file_scan：ファイルの収集・解析・Embedding。", "src/search：質問検索・根拠・状態・公開管理・API。", "検索側は既存のDB定義・設定・原本処理を参照します。"]),
            ("共有するデータと設定", ["config.local.yamlでナレッジ・保管先等を共有。", "DBの物理配置はwinnap。取込v3と検索v1を別管理。", "本番例は原本の保存領域を/var/lib/file-scan/へ置きます。"]),
            ("機能ごとに制御する", [".envは取込・公開管理用、.env.searchは常駐API用。", "接続先DBは同じで、DBロールを分けます。", "検索のポートと履歴許可は起動コマンドで指定。"]),
        ],
        note="共通YAMLのsearch配下でテストモードと意味検索の下限を設定します。期限・候補数・返却予算はコードの既定値です。",
        sources=["src/search/config.py", "src/file_scan/namespace.py", "docs/implementation/search-api-production.md"],
    ),
    dict(
        title="Difyと本番運用：根拠を受けて、回答へつなぐ", tag="15 / 接続と現在の境界",
        lead="API単独で検索を実装し、DifyからHTTPで呼べます。本番ではHTTPSの接続入口と、停止・復旧の運用も用意します。",
        diagram="deployment",
        cards=[
            ("Difyで回答へつなぐ", ["HTTP Requestノードから状態付きAPIへ送ります。", "非200や不足・空結果を分岐し、根拠をLLMへ渡します。", "Dify本体の配備・Chatflow作成は別途行います。"]),
            ("本番環境で起動する", ["Ubuntu VMでsystemd常駐化し、APIはlocalhost待受。", "NginxでHTTPSと接続元制限。test_mode=falseを確認。", "DBとstorageを揃えて保護し、復元試験を行います。"]),
            ("適用範囲を守る", ["文書ACL・利用者別SSOは保留。共有可能な範囲で利用。", "画像説明を公開する場合は、原図と照合して承認します。", "Difyでの画像表示や回答への入力は、呼出側で構成します。"]),
        ],
        note="同時検索4件、全体10秒が1プロセスの既定。healthの200だけで、実資料の準備・精度・本番受入を完了としません。",
        sources=["docs/implementation/search-api-production.md", "docs/design/03-llm-chat.md", "src/search/cli.py"],
    ),
    dict(
        title="根拠資料と、プログラムの読み順", tag="16 / さらに詳しく知るには",
        lead="この資料は現行実装の説明です。操作は実装手順、本番配置は構築手順、将来の拡張は設計書で確認します。",
        table=(["順序", "参照先", "確認すること"], [
            ["1", "docs/implementation/search-api.md", "公開・起動・API契約・テストモード"],
            ["2", "src/search/api.py・models.py", "HTTP受付、認証、入力と出力、原本取得"],
            ["3", "src/search/service.py・repository.py", "質問Embedding、範囲、3経路、coverage、鮮度"],
            ["4", "src/search/ranking.py・evidence.py", "順位統合、文脈、原文位置、返却予算"],
            ["5", "src/search/management.py・schema.py", "承認コピー・業務版・検索v1の表"],
            ["6", "docs/implementation/search-api-production.md", "Ubuntu VM、常駐化、HTTPS、監視、復旧"],
        ]),
        bottom="設計書02・03・04には将来案も含まれます。本資料の例は説明用で、実OpenAI API・Dify本体・検索品質・Ubuntu本番環境の検証結果を示すものではありません。",
        note="確認対象は2026-11-15の作業ツリーです。PDF、Markdown、図は同じ定義から生成します。",
        sources=["docs/design/02-search-platform.md", "docs/implementation/search-api.md", "src/search/"],
    ),
]


def flow(items, *, accents=None):
    drawing = Drawing(CW, 144)
    gap = 26
    width = (CW - gap * (len(items) - 1)) / len(items)
    for index, (title, lines) in enumerate(items):
        accent = (accents or {}).get(index, TEAL)
        fill = WARM if accent == ORANGE else BLUE
        box(drawing, index * (width + gap), 14, width, 116, title, lines, fill=fill, accent=accent)
        if index < len(items) - 1:
            x = index * (width + gap) + width
            arrow(drawing, x + 3, 72, x + gap - 3, 72)
    return drawing


def diagram(kind):
    if kind == "intro":
        return flow([
            ("質問を受ける", ["「E001の入力条件は？」", "接続キーと検索条件"]),
            ("公開済みの文章を探す", ["意味 / 全文 / 識別子", "候補の順番を1つに統合"]),
            ("根拠と状態を返す", ["原文・出典・関連文脈", "資料が揃っているかも通知"]),
        ])
    if kind == "overview":
        drawing = Drawing(CW, 146)
        items = [
            (0, 135, "呼出元", ["画面・Dify", "質問を送る"]),
            (163, 177, "検索API", ["範囲と条件を確定", "対象あり → 質問Embedding"]),
            (368, 180, "公開済みのDB", ["3経路で候補取得", "順位・原文・不足"]),
            (576, CW - 576, "呼出元へ", ["根拠 + 状態", "回答生成は呼出側"]),
        ]
        for x, width, title, lines in items:
            box(drawing, x, 27, width, 103, title, lines)
        for left, right in [(135, 163), (340, 368), (548, 576)]:
            arrow(drawing, left + 3, 77, right - 3, 77)
        text(drawing, 165, 8, "外部APIへ送るもの：質問のテキスト", 10, ORANGE)
        return drawing
    if kind == "publication":
        drawing = Drawing(CW, 144)
        items = [
            ("ingest", ["本文・位置を解析", "下書きへ保存"]),
            ("embed", ["検索文を数値化", "ベクトルを保存"]),
            ("publish", ["管理者が採用", "公開コピーへ凍結"]),
            ("検索API", ["採用された内容", "だけを検索"]),
        ]
        width, gap = (CW - 3 * 24) / 4, 24
        for index, (title, lines) in enumerate(items):
            x = index * (width + gap)
            box(drawing, x, 25, width, 105, title, lines, fill=PALE if index == 2 else BLUE)
            if index < 3:
                arrow(drawing, x + width + 3, 77, x + width + gap - 3, 77)
        text(drawing, 0, 7, "取込データはdraftのまま", 10, MUTED)
        text(drawing, (width + gap) * 2, 7, "公開の承認は別の操作", 10, TEAL)
        return drawing
    if kind == "scope":
        return flow([
            ("呼出元の指定", ["接続キー・ナレッジ", "質問・件数・絞り込み"]),
            ("サーバーで照合", ["設定ナレッジ・業務版", "公開・原本の現在状態"]),
            ("全経路に同じ範囲", ["意味検索 / 全文検索", "識別子検索 / 文脈取得"]),
        ])
    if kind == "semantic":
        return flow([
            ("質問を数値化", ["「IDなしで登録できる？」", "→ [0.12, -0.08, …]"]),
            ("保存済みの数値と比較", ["「ユーザーIDは必須です」", "→ 同じモデル・次元数"]),
            ("意味が近い候補", ["cosine類似度が正で下限以上", "この経路は最大50件"]),
        ])
    if kind == "lexical":
        return flow([
            ("質問", ["「登録時の入力条件は？」", "TokenBigramで語へ分割"]),
            ("検索語を使って照合", ["説明例：登録 / 入力 / 条件", "各語をリテラルのOR検索"]),
            ("PGroongaで候補取得", ["検索用テキストの全文索引", "この経路は最大50件"]),
        ])
    if kind == "identifier":
        drawing = flow([
            ("質問", ["「E001の条件は？」", "識別子：E001"]),
            ("公開時に登録した名前", ["チャンクA：[E001, ID]", "チャンクB：[E002]"]),
            ("完全一致の候補", ["この経路ではAが一致", "大文字・記号も保持"]),
        ])
        return drawing
    if kind == "rrf":
        drawing = Drawing(CW, 160)
        gap = 26
        width = (CW - gap * 2) / 3
        for index, (title, lines) in enumerate([
            ("意味検索の順位", ["1位 A / 2位 B / 3位 C"]),
            ("全文検索の順位", ["1位 B / 2位 A / 3位 D"]),
            ("識別子検索の順位", ["1位 B / 2位 D"]),
        ]):
            x = index * (width + gap)
            box(drawing, x, 88, width, 66, title, lines)
            arrow(drawing, x + width / 2, 83, x + width / 2, 60)
        drawing.add(Rect(0, 8, CW, 46, rx=8, ry=8, fillColor=PALE, strokeColor=None))
        text(drawing, 16, 25, "RRFでまとめた順位の例：     1位 B     2位 A     3位 D     4位 C", 13, TEAL)
        return drawing
    if kind == "context":
        return flow([
            ("見つかったヒット", ["「E001：ID未入力」", "evidence_role = hit"]),
            ("保存済みの文脈を補う", ["見出し：ユーザー登録", "列見出し：コード / 内容"]),
            ("根拠セットで返す", ["hit + context", "全体をtop_k・予算内に"]),
        ])
    if kind == "source":
        return flow([
            ("返す本文", ["A2: E001  B2: ID未入力", "元の文章の文字範囲を保持"]),
            ("出典の手掛かり", ["error.xlsx / 定義!A2:B2", "文書・原本版・公開ID"]),
            ("原本・関連画像を確認", ["source_ref / image_ref", "Bearer・採用先・SHAを確認"]),
        ])
    if kind == "coverage":
        drawing = Drawing(CW, 151)
        box(drawing, 0, 20, 220, 116, "ヒットした原文", ["資料Aが1件ヒット", "records：根拠あり", "不足とは分けて通知"])
        box(drawing, 260, 20, 230, 116, "予定した資料全体", ["A：準備済み", "B：更新待ち", "C：処理失敗"])
        box(drawing, 530, 20, CW - 530, 116, "返す状態", ["result：matched", "coverage：incomplete", "不足を隠さず返す"], fill=WARM, accent=ORANGE)
        arrow(drawing, 224, 78, 256, 78)
        arrow(drawing, 494, 78, 526, 78, color=ORANGE)
        return drawing
    if kind == "freshness":
        return flow([
            ("変更前", ["原本v1 → 公開コピーv1", "現行検索に利用"]),
            ("取込が原本v2を検出", ["v1は現行の対象から外す", "coverageは更新待ち"]),
            ("新世代を確認して公開", ["embed → publish v2", "新しい公開内容で検索"]),
        ], accents={1: ORANGE})
    if kind == "test_mode":
        drawing = Drawing(CW, 147)
        box(drawing, 0, 19, 195, 114, "設定ファイル", ["search:", "  test_mode: true", "変更後に再起動"])
        box(drawing, 227, 19, 207, 114, "検索API", ["DB検索を呼ばない", "Embeddingを呼ばない", "接続キーと入力は検査"])
        box(drawing, 466, 19, CW - 466, 114, "固定のテスト文言", ["これは検索APIのテスト応答です。", "DB検索は実行していません。", "recordsに1件・test_mode=true"])
        arrow(drawing, 199, 76, 223, 76)
        arrow(drawing, 438, 76, 462, 76)
        return drawing
    if kind == "configuration":
        drawing = Drawing(CW, 150)
        box(drawing, 0, 21, 226, 113, "config.local.yaml", ["DB・ナレッジ・保管先", "Embedding・収集元", "テストモード・意味検索の下限"])
        box(drawing, 270, 83, 220, 55, "src/file_scan", ["取込・変換・ベクトル化"])
        box(drawing, 270, 17, 220, 55, "src/search", ["公開管理・質問検索・API"])
        box(drawing, 534, 21, CW - 534, 113, "共通の保存先", ["DB：winnap", "取込v3 / 検索v1", "原本・画像の保管先"])
        arrow(drawing, 232, 108, 264, 108)
        arrow(drawing, 232, 44, 264, 44)
        arrow(drawing, 496, 108, 528, 108)
        arrow(drawing, 496, 44, 528, 44)
        return drawing
    if kind == "deployment":
        return flow([
            ("Dify / 許可した呼出元", ["HTTPSで質問を送る", "Bearer接続キーを使用"]),
            ("Ubuntu VMの検索API", ["Nginx → localhostのAPI", "systemdで常駐・DBへ検索"]),
            ("呼出側で回答を組み立てる", ["根拠と不足を判断", "根拠をLLMへ渡して回答"]),
        ])
    raise ValueError(kind)


def paragraph(content, x, top, width, **kwargs):
    return layout.para(content, x, top, width, **kwargs)


def draw_cards(items, top):
    gap = 18
    width = (CW - gap * 2) / 3
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
            height = paragraph(point, x + 12, y, width - 12, size=10.5, leading=16)
            y -= height + 8
        bottom = min(bottom, y)
        if y < 106:
            raise ValueError(f"Card overflow: {title}: {y:.1f}")
    return bottom


def draw_table(page, top):
    if "col_widths" not in page:
        return layout.table(*page["table"], top=top)
    headings, rows = page["table"]
    style = ParagraphStyle("cell", fontName="JP", fontSize=10.3, leading=14.3, wordWrap="CJK")
    header = ParagraphStyle("header", parent=style, textColor=colors.white)
    data = [[Paragraph(escape(value), header) for value in headings]]
    data.extend([Paragraph(escape(value), style) for value in row] for row in rows)
    table = Table(data, colWidths=page["col_widths"], hAlign="LEFT")
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), INK),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, layout.PAPER]),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 12),
        ("RIGHTPADDING", (0, 0), (-1, -1), 12),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
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
    C.setTitle("検索API 図解ガイド")
    C.setAuthor("2026-file-scan")
    C.setSubject("公開対象・3経路検索・順位統合・出典・coverage・テストモードの現行実装を説明")
    md = [
        "# 検索API 図解ガイド\n",
        "確認日：2026-11-15。現行の作業ツリーをもとに、初めて読む方向けに検索APIを説明します。\n",
        "[図解PDFを開く](../../output/pdf/検索API_図解ガイド.pdf)\n",
        "図の文書・質問・順位・数値は説明用です。実データの検索結果や、検索品質の測定結果ではありません。\n",
        "## 目次\n",
    ]
    md.extend(f"{index}. {page['title']}" for index, page in enumerate(PAGES, 1))
    md.append("")
    bounds = []
    metrics = []
    for number, page in enumerate(PAGES, 1):
        C.bookmarkPage(f"page-{number}")
        C.addOutlineEntry(page["title"], f"page-{number}", level=0)
        C.setFillColor(colors.white)
        C.rect(0, 0, W, H, fill=1, stroke=0)
        C.setFillColor(TEAL)
        C.rect(M, H - 36, 34, 4, fill=1, stroke=0)
        paragraph(page["tag"], M + 46, H - 27, CW - 46, size=10, leading=14, color=TEAL)
        title_height = paragraph(page["title"], M, H - 57, CW, size=23, leading=32)
        if title_height > 33:
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
                "font-family: JP", "font-family: 'Arial Unicode MS', 'Hiragino Kaku Gothic ProN', sans-serif"
            ))
            bounds.append(dict(page=number, x=M, y=y, w=CW, h=drawing.height, name=svg.stem))
            bottom = draw_cards(page["cards"], y - 13)
            md.append(f"![{page['title']}](images/search/{svg.stem}.png)\n")
            for title, points in page["cards"]:
                md.append(f"### {title}\n")
                md.extend(f"- {point}" for point in points)
                md.append("")
        else:
            bottom = draw_table(page, top=H - 151)
            bottom -= 16
            bottom -= paragraph(page["bottom"], M, bottom, CW, size=11, leading=18)
            if bottom < 106:
                raise ValueError(f"Table overflow: {number}: {bottom:.1f}")
            heads, rows = page["table"]
            md.extend(["| " + " | ".join(heads) + " |", "| " + " | ".join(["---"] * len(heads)) + " |"])
            md.extend("| " + " | ".join(row) + " |" for row in rows)
            md.append("\n" + page["bottom"] + "\n")
        C.setFillColor(PALE)
        C.roundRect(M, 65, CW, 32, 5, fill=1, stroke=0)
        note_height = paragraph(page["note"], M + 10, 89, CW - 20, size=9, leading=12)
        if note_height > 24:
            raise ValueError(f"Note overflow: {number}")
        source_height = paragraph("参照：" + " ／ ".join(page["sources"]), M, 55, CW - 50, size=7.5, leading=10, color=MUTED)
        if source_height > 20:
            raise ValueError(f"Sources overflow: {number}")
        C.setFont("JP", 9)
        C.setFillColor(MUTED)
        C.drawRightString(W - M, 29, f"{number:02d} / {len(PAGES):02d}")
        md.append("> " + page["note"] + "\n")
        refs = [f"[{source}](../../{source})" for source in page["sources"]]
        md.append("根拠：" + "、".join(refs) + "\n")
        metrics.append(dict(page=number, title=page["title"], content_bottom=round(bottom, 1)))
        C.showPage()
    C.save()
    MD.write_text("\n".join(md), encoding="utf-8")
    poppler = shutil.which("pdftoppm")
    if not poppler:
        raise RuntimeError("PNG図の生成にはPopplerのpdftoppmが必要です")
    with tempfile.TemporaryDirectory(prefix="search-api-guide-") as directory:
        prefix = Path(directory) / "page"
        subprocess.run([poppler, "-r", "120", "-png", str(PDF), str(prefix)], check=True, capture_output=True)
        for entry in bounds:
            with Image.open(Path(directory) / f"page-{entry['page']:02d}.png") as rendered:
                sx, sy = rendered.width / W, rendered.height / H
                crop = (int(entry["x"] * sx), int((H - entry["y"] - entry["h"]) * sy),
                        int((entry["x"] + entry["w"]) * sx), int((H - entry["y"]) * sy))
                rendered.crop(crop).save(IMAGES / (entry["name"] + ".png"))
    report = ROOT / "artifacts/reports/search-guide-layout.json"
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(json.dumps(metrics, ensure_ascii=False, indent=2) + "\n")
    print(f"Created {PDF}\nCreated {MD}\nPages: {len(PAGES)} / Diagrams: {len(bounds)}")


if __name__ == "__main__":
    build()
