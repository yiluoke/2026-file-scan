"""図解ガイドのPDF・Markdown・SVGを生成する。既存プログラムやDBは変更しない。"""
from pathlib import Path
from html import escape
import os
import shutil
import subprocess
import tempfile

from reportlab.pdfgen import canvas
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.lib import colors
from reportlab.lib.styles import ParagraphStyle
from reportlab.platypus import Paragraph, Table, TableStyle
from reportlab.graphics.shapes import Drawing, Rect, String, Line, Polygon, Circle
from reportlab.graphics import renderPDF, renderSVG
from PIL import Image

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
IMAGES = HERE / "images"
PDF = ROOT / "output/pdf/ファイル取込処理_図解ガイド.pdf"
MD = HERE / "ファイル取込処理_図解ガイド.md"
FONT_PATH = Path(os.environ.get("GUIDE_FONT_PATH", "/System/Library/Fonts/Supplemental/Arial Unicode.ttf"))
pdfmetrics.registerFont(TTFont("JP", str(FONT_PATH)))
INK = colors.HexColor("#17324d")
TEAL = colors.HexColor("#087f8c")
BLUE = colors.HexColor("#eaf3fa")
PALE = colors.HexColor("#edf7f5")
MUTED = colors.HexColor("#526779")
ORANGE = colors.HexColor("#b56a18")
PAPER = colors.HexColor("#f6f8fb")
W, H = 841.89, 595.28
M, CW = 42, W - 84

PAGES = [
 dict(title="ファイル取込処理を、図で理解する", tag="はじめて読む方へ", lead="ファイルの種類に合わせて読み取り、検索しやすい小さな文章へ整理し、その意味を数値の列に変えます。", diagram="intro",
 cards=[("この資料でわかること", ["種類ごとに何をどう読み取るか。", "本文・表を、どう分割するか。", "どの文章をベクトル化し、どこに保存するか。"]),
 ("まず覚える3つの言葉", ["ブロック：段落・表など、原本のひとまとまり。", "チャンク：検索・ベクトル化に使う小さな文章。", "Embedding：文章の意味を数値の列で表す処理。"]),
 ("読む順番", ["全体像・対応形式：2～3ページ。", "形式ごとの図解：4～8ページ。", "分割・ベクトル化・保存・運用：9～14ページ。"] )],
 note="確認基準：2026-11-01／文書取込に限定した修正版。値は同梱設定・コードの既定値。図中の業務内容と数値ベクトルは説明用です。", sources=["README.md", "src/file_scan/domain.py:16", "config.example.yaml"]),
 dict(title="全体像：取込とベクトル化は別の処理", tag="01 / 全体の流れ", lead="ingest はローカルで抽出・分割し、embed が抽出済みの検索用テキストをOpenAI APIへ送信します。", diagram="pipeline",
 cards=[("1. 対象を決めて保管", ["指定フォルダを列挙し、拡張子・除外規則で絞ります。", "原本全体のSHA-256で変更を確認。原本コピーは内容ハッシュで保管します。", "共有元のファイルは変更しません。"]),
 ("2. 読み取り・整理", ["形式別の解析器が、本文・表・位置を抽出します。", "ブロックを小さなチャンクへ分割し、DBに下書き保存します。", "画像実体は保存しますが、画像の文字・意味はまだ解析しません。"]),
 ("3. 意味を数値にする", ["embed が選択中の世代のチャンクを対象にします。", "同じモデル・入力の保存済みベクトルは再利用します。", "新しい入力だけをAPIへ送り、pgvectorに保存します。"] )],
 note="ingest と embed の実行だけでは検索公開されません。公開は検索側の file-search publish で別途行います。", sources=["src/file_scan/pipeline.py:405", "src/file_scan/embeddings.py:121", "docs/implementation/search-api.md"]),
 dict(title="対応する文書と、解析の入口", tag="02 / 対応形式の早見表", lead="OfficeやPDFを一律に画像化せず、文字・セル・構造を読み取れる形式では、その情報を直接利用します。",
 table=(["種類", "読み取り方式", "検索用テキストになるもの", "原本へ戻る手掛かり"], [
 ["TXT / MD", "文字コードを判定して読込", "段落／MDの見出しの文脈", "開始・終了行"],
 ["Excel (.xlsx / .xlsm)", "openpyxl + OOXML", "セル番地付きの行テキスト", "シート名・セル範囲"],
 ["Word (.docx)", "OOXMLを直接解析", "段落・表・見出しの文脈", "段落番号・表番号"],
 ["PDF", "標準：Doclingのローカルモデル", "本文・表・見出しの文脈", "ページ・領域座標"],
 ["画像・スキャン領域", "実体と出現位置を保存", "現時点では文字・説明を生成しない", "文書内の位置・画像参照"],
 ]),
 bottom="対象はTXT・MD・XLSX・XLSM・DOCX・PDF。別拡張子.markdownも設定できます。プログラム解析は別処理とし、SQL・JSON・YAML・CSV・LOG等も対象外です。対象外の形式は設定で追加してもエラーになります。",
 note="YAMLの実行設定、JSON/JSONLの確認用出力、DB用SQLは引き続き使用します。原本の取込対象とは別です。", sources=["src/file_scan/parsers.py:44", "src/file_scan/config.py:23", "config.example.yaml"]),
 dict(title="テキスト・Markdown：段落と見出しを拾う", tag="03 / TXT・MD", lead="文字を直接読み取り、空行で段落に分けます。Markdownは # 形式の見出しを見て、章・節の文脈を付けます。", diagram="text",
 cards=[("どう解析するか", ["UTF-8（BOM付きも可）、次にCP932で読み取ります。", "行番号を保持し、空行やMarkdown見出しでブロックを分けます。", "コードフェンス内の # は見出し扱いにしません。"]),
 ("どうベクトル化するか", ["本文にファイル名・章節の見出しを添えた search_text を作ります。", "長い段落は共通の文字数・トークン上限で分割します。", "形式ごとに別のEmbeddingモデルを使う構成ではありません。"]),
 ("理解しておきたい点", ["対象はTXT・Markdown文書です。設定・ログ・データファイルは取り込みません。", "文書内のコード片も原文として扱い、プログラムの構造解析はしません。", "読めない文字コードやNULを含む入力は明示的にエラーにします。"] )],
 note="例は説明用です。MDの見出しは主に検索文脈に使用し、通常は見出し自体を本文チャンクに重ねません。", sources=["src/file_scan/parsers.py:99", "src/file_scan/parsers.py:112", "src/file_scan/chunking.py:30"]),
 dict(title="Excel：シート・セル・数式を分けて読む", tag="05 / XLSX・XLSM", lead="各シートの実在セルを調べ、行ごとに「セル番地：値」のテキストへ変換します。数式と保存済み結果は別に保持します。", diagram="excel",
 cards=[("どう解析するか", ["openpyxlで数式用・保存済み結果用の2通りを読みます。", "結合セル情報はシートに保持し、表示形式・コメント等はセル情報に残します。", "既定では非表示シート・行・列を本文から除外します。"]),
 ("どうベクトル化するか", ["例：A2: E001、B2: ID未入力。行テキストがチャンク本文です。", "ファイル名・シート名・該当する列見出しを検索文脈に添えます。", "現実装では行が別のtableブロックとなり、行ごとのチャンクを基本にします。"]),
 ("理解しておきたい点", ["数式を再計算しません。保存済み結果がなければ警告を付けます。", "数式の結果は構造データに保持しますが、検索本文へ自動追加しません。", "見出しはExcelテーブル定義を優先。先頭行からの推定は候補です。VBAも実行しません。"] )],
 note="埋込画像は位置と実体を保存します。図形・グラフ・VBA等の未処理要素は警告で残します。", sources=["src/file_scan/parsers.py:232", "src/file_scan/parsers.py:372", "src/file_scan/chunking.py:14"]),
 dict(title="Word：本文の順に、段落・表・見出しを読む", tag="06 / DOCX", lead="DOCXの内部XML（OOXML）を直接読み、本文中に現れる段落・表の順番と、見出しの階層を保持します。", diagram="word",
 cards=[("どう解析するか", ["見出しスタイルとアウトラインレベルから章節を取得します。", "表は行・セルの文字を取り出し、結合情報も保持します。", "目次は案内用のnavigationとし、ブックマークで本文へ関連付けます。"]),
 ("どうベクトル化するか", ["段落・表の本文に、ファイル名と見出しの文脈を添えます。", "目次を通常の本文チャンクに重ねず、本文の重複を抑えます。", "Word表の列見出しは自動判定・再付与が保証されません。"]),
 ("理解しておきたい点", ["原本位置は段落番号・表番号等。Wordの表示ページ番号は再現しません。", "自動採番、変更履歴、補助文書、浮動要素、入れ子表等は警告対象です。", "テキストボックスで文字が取れても、読み順未確認の警告を残します。"] )],
 note="WordをPDFや画像へ変換して読み直す経路ではありません。本文の構造と、画面上のレイアウト再現は別です。", sources=["src/file_scan/parsers.py:400", "src/file_scan/parsers.py:509", "src/file_scan/chunking.py:14"]),
 dict(title="PDF：Doclingで本文・表・領域を解析する", tag="07 / 文字のあるPDF", lead="事前配置したレイアウト解析・表構造モデルをCPUで使い、本文や表をブロックへ変換します。OCRは無効です。", diagram="pdf",
 cards=[("どう解析するか", ["Doclingが本文・見出し・表・図の領域と読み順を解析します。", "表はMarkdown本文とセル構造を保持します。", "ページ番号・bbox（長方形の領域座標）・座標系を残します。"]),
 ("どうベクトル化するか", ["抽出できた本文・表に、ファイル名と見出しを添えて分割します。", "取得できた列見出しを表の後続チャンクにも添えます。", "見出しの文脈は直近の見出し。完全な多階層復元ではありません。"]),
 ("理解しておきたい点", ["図の領域画像と、PDF内の原画像は別々に保存する場合があります。", "文字のないページはOCR待ちとして部分抽出にします。", "nativeを明示設定した場合だけpypdfで文字を抽出。Docling失敗時の自動代替はありません。"] )],
 note="モデルは利用前に固定版・ハッシュを検査します。原本のレイアウトや表を完全に復元できる保証はありません。", sources=["src/file_scan/parsers.py:729", "src/file_scan/parsers.py:750", "src/file_scan/models.py", "docs/implementation/docling-pdf.md"]),
 dict(title="画像・スキャン：保存済みでも、解析済みとは限らない", tag="08 / 現在の画像処理の境界", lead="画像の実体と出現位置は保管します。画像の中の文字や図の意味をテキストにする処理は、まだ実装されていません。", diagram="image",
 cards=[("現在できること", ["Word・Excelの埋込画像、PDFの図領域・原画像を保存します。", "本文ブロックと画像の参照を結び付けます。", "pending_images と image_analysis_pending で未解析を記録します。"]),
 ("現在できないこと", ["画面画像内の「登録」ボタンなどをOCRで読むこと。", "フロー図の矢印や、図形の意味を説明文にすること。", "画像を直接Embeddingすること。単独PNG・JPEGの取込も対象外です。"]),
 ("検索・運用への影響", ["本文はベクトル化できても、画像にしかない情報は意味検索に載りません。", "部分抽出の文書でも、取れた本文チャンクはembedの対象になります。", "再試行だけでOCR待ちが解消するわけではありません。"] )],
 note="pending_imagesは処理対象領域・画像の件数です。PDFでは同じ見た目の図が領域画像と原画像の両方に現れる場合があります。", sources=["src/file_scan/parsers.py:187", "src/file_scan/parsers.py:661", "src/file_scan/domain.py:61", "src/file_scan/state.py:42"]),
 dict(title="チャンク化：本文と検索用の文脈を分ける", tag="09 / 全形式に共通する分割", lead="本文は原文に対応する形で保持し、ファイル名・見出し・取得できた列見出しは、検索用テキストにだけ添えます。", diagram="chunk",
 cards=[("何を単位に分けるか", ["章節・親要素・由来の違うブロックを安易に混ぜません。", "本文は既定1,800文字以内。長い本文は改行等を優先して分割します。", "固定幅の重なりを入れる実装ではありません。"]),
 ("トークンも測る", ["トークンはモデルが読む小さな文字片。文字数とは異なります。", "cl100k_baseで、文脈を含む入力全体を実測します。既定上限は8,191です。", "文脈は既定1,024トークン以内、かつ全体予算の半分以下です。"]),
 ("表と原本対応を守る", ["表は行の境界を優先。1行が長すぎる場合は分割し、警告します。", "長すぎる検索文脈は省略を記録し、本文の続きを別チャンクに残します。", "chunk_blocksに原文とチャンクの文字範囲を保存して一致を検査します。"] )],
 note="content_text＝チャンク本文。search_text＝文脈＋本文。Embeddingへ送るのはsearch_textです。", sources=["src/file_scan/chunking.py:14", "src/file_scan/chunking.py:30", "src/file_scan/tokens.py", "src/file_scan/pipeline.py:238"]),
 dict(title="ベクトル化：文章の意味を、数値の列へ変える", tag="10 / OpenAI Embedding", lead="既定ではtext-embedding-3-smallを使い、1つのsearch_textから1,536個の数値を得ます。全文書形式で共通の処理です。", diagram="vector",
 cards=[("どの文章を送るか", ["原本・収集状態・選択世代・変換設定が一致する、下書きチャンクです。", "処理状態succeededとpartialが対象。failed・stale等は対象にしません。", "実APIへ送る前に、入力が1～8,191トークン内か再検査します。"]),
 ("どう呼び出すか", ["DB読込は既定128件ずつ。APIへは既定16入力ずつ送ります。", "1要求の合計トークンも制限します。同時API要求は1つです。", "同じ入力はキャッシュや要求内のまとめ処理で再利用します。"]),
 ("どう保存するか", ["応答の件数・順序・次元数・有限数・非ゼロを確認します。", "モデル等のプロファイルと入力ハッシュを付け、pgvectorに保存します。", "質問も同じモデル・次元で数値化すると、意味の近さを比較できます。"] )],
 note="同梱設定はembedding.enabled=false。図の座標は概念図、数値は架空です。近さは関連性の手掛かりであり、正しさの保証ではありません。", sources=["src/file_scan/embeddings.py:69", "src/file_scan/embeddings.py:121", "src/file_scan/config.py:40", "src/search/service.py"]),
 dict(title="保存：原本、抽出結果、ベクトルをつなぐ", tag="11 / 保存先と根拠の追跡", lead="原本と画像のバイト列はファイル保管先へ。抽出本文・構造・原本位置・ベクトルはDBへ保存します。", diagram="storage",
 cards=[("ファイルとして保存", ["artifacts/storage：同梱設定の原本コピー・抽出画像。内容ハッシュで管理します。", "DBには実体参照を保存します。DBと保管先を一緒に扱います。", "storage_dirは1つのDBの初回登録先に固定されます。"]),
 ("DBに保存", ["documents / document_versions：文書の識別と原本の版。", "blocks / chunks / chunk_blocks：構造・検索用本文・文字対応。", "chunk_embeddings：チャンクのベクトル。embedding_profiles：モデル等の条件。"]),
 ("人が確認する出力", ["manifest.json：処理状態・件数等。document.json：ブロック・位置・画像参照。", "chunks.jsonl：検索用テキスト。review.md：抽出内容の確認用。", "generations/<世代ハッシュ>/に出力。編集してもDBには逆反映されません。"] )],
 note="出典追跡は「チャンク→原本ブロック→原本位置→保管原本」。位置の精度は形式ごとに異なります。", sources=["src/file_scan/db.py", "src/file_scan/pipeline.py:282", "src/file_scan/pipeline.py:353", "config.example.yaml"]),
 dict(title="再実行：変わった段階だけ、やり直す", tag="12 / 世代とキャッシュ", lead="原本、抽出条件、分割条件、Embedding入力を分けて識別し、同じ結果を再利用します。過去の履歴も保持します。", diagram="reuse",
 cards=[("原本が同じ", ["変更確認では毎回、共有元を全文ハッシュします。", "同じ原本・同じ変換設定なら既存世代を再利用します。", "mtime・サイズだけで同一とは判断しません。ネットワーク読込は残ります。"]),
 ("分割条件だけが変わった", ["抽出キャッシュからブロックを読み、新たな分割条件を適用します。", "画像参照・キャッシュの整合性も検査します。", "モデルやパーサーの変更なら、該当する抽出もやり直します。"]),
 ("Embeddingをやり直す", ["モデル・次元・入力版が同じでsearch_textも同じなら再送しません。", "成功済みバッチを残し、失敗後は未完了の入力から再開できます。", "選択中世代を明示管理し、失敗時に古い世代へ自動で戻りません。"] )],
 note="同じ条件で部分抽出をやり直す指定は ingest --retry-partial。変更のない通常再実行ではpartialも再利用されます。", sources=["src/file_scan/profiles.py:18", "src/file_scan/profiles.py:49", "src/file_scan/pipeline.py:405", "docs/design/05-ingestion-improvements.md"]),
 dict(title="運用：どこまで終わったかを確認する", tag="13 / 実行順序と状態", lead="準備済みのPython環境・DB・モデル・設定を前提に、棚卸し→取込→抽出確認→ベクトル化→状態確認の順に進めます。",
 table=(["順序", "コマンド例（設定ファイルはconfig.local.yaml）", "確認すること"], [
 ["1", "file-scan scan --config config.local.yaml", "対象・除外・列挙エラー。DB登録しない"],
 ["2", "file-scan init-db --config config.local.yaml", "初回導入・既存DBのスキーマ移行"],
 ["3", "file-scan ingest --config config.local.yaml", "registered / unchanged / failed と partial"],
 ["4", "generations/<世代>/review.md 等を読む", "抽出本文・原本位置・画像待ち・品質警告"],
 ["5", "file-scan embed --config config.local.yaml", "有効化・実キー設定済みならAPIへ送信"],
 ["6", "file-scan status --config config.local.yaml", "現在の世代状態・ready / pending / total"],
 ]),
 bottom="succeeded＝品質警告・画像待ちなし、partial＝警告または画像待ちあり、failed＝失敗。これらは処理状態です。draft（下書き）は別の公開状態です。終了コードは0：正常、1：実行全体のエラー、2：部分完了。",
 note="資料作成では実API呼出・業務データ取込・DB変更・公開を行っていません。.envはCLIが自動読込しません。", sources=["src/file_scan/cli.py", "src/file_scan/domain.py:61", "docs/implementation/first-version.md", "docs/implementation/openai-embedding.md"]),
 dict(title="設計書を読むときの注意：現状と将来構想", tag="14 / 実装との照合結果", lead="設計書には目標構成や古い時点の記述も含まれます。本資料は現在のプログラムと、後から追加された実装資料を優先しています。",
 table=(["論点", "設計上の記述・構想", "現在のプログラム"], [
 ["画像OCR・説明", "外部APIで文字と意味を取得する方針", "未実装。実体・位置・解析待ちを保存"],
 ["分割の数値", "400～800トークン等のPoC候補", "既定1,800文字／入力8,191トークン以内"],
 ["検索公開", "改善設計の冒頭には未実装と記載", "検索側にpublish実装あり。取込側はdraftのまま"],
 ["Word / PDF構造", "階層・表・レイアウトを幅広く扱う構想", "Wordはページ再現なし。PDF見出しは直近のみ"],
 ["並列処理", "parse_workersを設定可能", "現在は文書ごとに順次確定。同時解析は1文書"],
 ["権限・対話", "利用者ACL、レビュー画面、Dify等", "文書ACL・SSO・レビュー画面・Chatflow等は未実装"],
 ]),
 bottom="ベクトル化済みでも自動公開されません。検索側は管理者が承認した世代の本文・出典・完成ベクトルを凍結コピーします。公開後にembedした場合は、同じ世代を再公開して反映します。",
 note="「警告がない」ことは、原本内容を100%正しく理解できたという意味ではありません。実物との照合は引き続き必要です。", sources=["docs/design/01-document-conversion.md", "docs/design/05-ingestion-improvements.md", "docs/implementation/search-api.md", "src/search/management.py:116", "src/file_scan/workers.py"]),
 dict(title="根拠資料と、プログラムの読み順", tag="15 / 保守担当者向けの参照先", lead="業務担当者は図解から、保守担当者は以下のファイルから実装の詳細へ進めます。本文各ページの下部にも参照先を記載しました。",
 table=(["知りたいこと", "主な確認先（プロジェクトルート基準）"], [
 ["入口・全体の進行", "src/file_scan/ 内：cli.py / pipeline.py:405"],
 ["形式別の解析", "src/file_scan/parsers.py：112（TXT・MD）、232（Excel）"],
 ["Word・PDFの解析", "src/file_scan/parsers.py：509（Word）、750（Docling）、683（native PDF）"],
 ["分割・トークン・ベクトル", "src/file_scan/ 内：chunking.py:30 / tokens.py / embeddings.py:121"],
 ["条件・世代・保存", "src/file_scan/ 内：config.py / profiles.py / state.py / db.py / storage.py"],
 ["設計・運用", "docs/design/01-document-conversion.md / 05-ingestion-improvements.md"],
 ["操作と検索公開", "docs/implementation/first-version.md / openai-embedding.md / search-api.md"],
 ]),
 bottom="今回の確認：全体テスト160件が成功、実DB接続が必要な46件はスキップ。文書の抽出・分割・再取込、対象外形式の拒否、Docling実モデルを確認しました。実OpenAI API・実PostgreSQL、検索精度・処理性能の測定は今回の範囲外です。",
 note="確認日：2026-11-01／基準コミットc482b66から取込範囲を修正した作業ツリー。プログラム・設定・設計書も更新済み。", sources=["tests/test_collection.py", "tests/test_parsers.py", "tests/test_pipeline.py"]),
]


for index, page in enumerate(PAGES):
    if index:
        page["tag"] = f"{index:02d} / " + page["tag"].split(" / ", 1)[1]


def dtext(d, x, y, text, size=12, fill=INK, anchor="start"):
    d.add(String(x, y, text, fontName="JP", fontSize=size, fillColor=fill, textAnchor=anchor))


def box(d, x, y, w, h, title, lines=(), fill=BLUE, accent=TEAL, size=12):
    d.add(Rect(x, y, w, h, rx=8, ry=8, fillColor=fill, strokeColor=None))
    d.add(Rect(x, y+h-4, w, 4, fillColor=accent, strokeColor=None))
    dtext(d, x+13, y+h-25, title, size, accent)
    for i, line in enumerate(lines):
        dtext(d, x+13, y+h-47-i*17, line, 10.5)


def arrow(d, x1, y1, x2, y2, color=TEAL, dashed=False):
    d.add(Line(x1,y1,x2,y2,strokeColor=color,strokeWidth=1.7,strokeDashArray=[4,3] if dashed else None))
    if abs(x2-x1) >= abs(y2-y1):
        s = 1 if x2 >= x1 else -1
        pts = [x2,y2,x2-6*s,y2+3,x2-6*s,y2-3]
    else:
        s = 1 if y2 >= y1 else -1
        pts = [x2,y2,x2-3,y2-6*s,x2+3,y2-6*s]
    d.add(Polygon(pts,fillColor=color,strokeColor=None))


def flow(items, labels=(), h=136):
    d = Drawing(CW,h)
    n=len(items); gap=27; width=(CW-(n-1)*gap)/n
    for i,(title,lines) in enumerate(items):
        x=i*(width+gap)
        box(d,x,16,width,105,title,lines)
        if i<n-1:
            arrow(d,x+width+3,68,x+width+gap-3,68)
    for x,y,label in labels:
        dtext(d,x,y,label,9,MUTED)
    return d


def diagram(kind):
    if kind=="intro":
        return flow([("原本を読む",["Word・Excel・PDF", "TXT・Markdown"]),("検索する文章に整理",["原本位置付きのブロック", "小さなチャンクへ分割"]),("意味を数値にする",["文章 → [0.12, -0.08, …]", "ベクトルとしてDBへ保存"])])
    if kind=="pipeline":
        d=Drawing(CW,150)
        box(d,0,27,120,92,"原本",["指定フォルダ", "原本コピー"])
        box(d,150,27,210,92,"ingest：ローカル処理",["解析 → ブロック → チャンク", "DBへdraft登録"])
        box(d,390,27,205,92,"embed：外部API",["search_text → OpenAI", "戻ったベクトルを検査"])
        box(d,625,27,CW-625,92,"PostgreSQL",["pgvector保存", "検索公開は別途"])
        for a,b in [(120,150),(360,390),(595,625)]: arrow(d,a+3,72,b-3,72)
        dtext(d,391,9,"外部へ送るもの：抽出済みの検索用テキスト",10,ORANGE)
        return d
    if kind=="text":
        return flow([("原本：manual.md",["# ユーザー登録", "## 入力項目", "ユーザーIDは必須です。"]),("ブロック",["種別：段落", "節：ユーザー登録 / 入力項目", "位置：開始行～終了行"]),("search_text → ベクトル",["manual.md / ユーザー登録", "/ 入力項目", "ユーザーIDは必須です。"])])
    if kind=="excel":
        return flow([("原本：error.xlsx",["       A           B", "1   コード       内容", "2   E001         ID未入力"]),("行ごとのtableブロック",["A2: E001  B2: ID未入力", "位置：定義!A2:B2", "数式と保存済み結果は別保持"]),("search_text → ベクトル",["error.xlsx / 定義", "列：A1: コード / B1: 内容", "A2: E001  B2: ID未入力"])])
    if kind=="word":
        return flow([("原本：manual.docx",["見出し：ユーザー登録", "段落：IDは必須です。", "表：項目 / 制約"]),("OOXML → ブロック",["heading → paragraph → table", "目次は本文への案内として保持", "位置：段落番号・表番号"]),("search_text → ベクトル",["manual.docx / ユーザー登録", "IDは必須です。", "表本文は別のチャンクへ"])])
    if kind=="pdf":
        d=Drawing(CW,146)
        box(d,0,13,175,120,"原本PDFの1ページ",["見出し：操作方法", "本文：IDを入力します。", "表：項目 / 制約", "図：画面イメージ"])
        box(d,205,13,255,120,"Docling（CPU・OCR無効）",["文字領域 → 段落ブロック", "表領域 → 表本文 + セル構造", "図領域 → 画像保存・解析待ち", "ページ + bbox を保持"])
        box(d,490,13,CW-490,120,"search_text → ベクトル",["ファイル名・直近の見出し", "+ 抽出できた本文・表", "画像内文字は未収録", "図は自動で説明文にしない"])
        arrow(d,178,72,201,72);arrow(d,463,72,486,72)
        return d
    if kind=="image":
        d=Drawing(CW,140)
        box(d,0,12,220,116,"埋込画像・スキャンPDF",["[画面]  ユーザーID ______", "          [登録]   [取消]", "画像の中にしかない情報"])
        box(d,260,12,230,116,"現在：実体と位置を保存",["画像ファイルを保管", "元の段落・シート・ページへ対応", "状態：画像の解析待ち"])
        box(d,530,12,CW-530,116,"将来：OCR・画像説明",["「登録」「取消」の文字", "図の観測・説明を生成", "現在は未実装"],fill=colors.HexColor("#fff3e4"),accent=ORANGE)
        arrow(d,225,70,255,70);arrow(d,495,70,525,70,color=ORANGE,dashed=True)
        return d
    if kind=="chunk":
        d=Drawing(CW,140)
        box(d,0,14,210,114,"元のブロック",["長い本文 A……", "続きの本文 B……", "節：入力項目／位置を保持"])
        box(d,245,76,235,52,"チャンク1：本文 A……",[],size=11)
        box(d,245,14,235,52,"チャンク2：本文 B……",[],size=11)
        box(d,515,76,CW-515,52,"同じ文脈 + 本文 A……",[],size=11)
        box(d,515,14,CW-515,52,"同じ文脈 + 本文 B……",[],size=11)
        dtext(d,258,87,"content_text：本文のみ",9,MUTED)
        dtext(d,258,25,"原文の文字範囲と対応",9,MUTED)
        dtext(d,528,87,"search_text → ベクトル1",9,TEAL)
        dtext(d,528,25,"search_text → ベクトル2",9,TEAL)
        arrow(d,215,72,240,72);arrow(d,485,72,510,72)
        return d
    if kind=="vector":
        d=Drawing(CW,148)
        box(d,0,25,235,112,"検索用テキスト",["「ユーザーIDは必須です」", "→ Embedding API", "→ [0.12, -0.08, …]", "既定1,536次元の数値列"])
        arrow(d,245,80,280,80)
        box(d,294,25,205,112,"意味の近さを比較",["近い話題ほど近くに置かれる", "イメージで理解する", "実際は1,536次元"])
        arrow(d,510,80,540,80)
        d.add(Rect(554,17,CW-554,123,fillColor=PALE,strokeColor=None,rx=8,ry=8))
        arrow(d,570,33,738,33,color=MUTED);arrow(d,570,33,570,127,color=MUTED)
        for x,y,label,c in [(605,107,"IDの入力条件",TEAL),(614,81,"IDは必須",TEAL),(693,48,"DBバックアップ",ORANGE)]:
            d.add(Circle(x,y,4,fillColor=c,strokeColor=None));dtext(d,x-16,y+11,label,9,c)
        dtext(d,555,3,"2次元の概念図・実測値ではありません",9,MUTED)
        return d
    if kind=="storage":
        return flow([("ファイル保管先",["原本コピー・画像のバイト列", "内容ハッシュで保管", "artifacts/storage"]),("DB：本文と対応関係",["チャンク → ブロック", "→ 原本位置 → 原本参照", "ベクトルもチャンクに対応"]),("確認用出力",["document.json / chunks.jsonl", "manifest.json / review.md", "artifacts/reports"])])
    if kind=="reuse":
        return flow([("抽出の再利用",["原本ハッシュ", "+ 形式別の抽出条件", "→ 抽出キャッシュ"]),("世代・分割の再利用",["文書ID + 原本版", "+ 抽出・分割条件", "→ 変換世代とチャンク"]),("ベクトルの再利用",["モデル・次元・入力版", "+ search_textのハッシュ", "→ 保存済みベクトル"])])
    raise ValueError(kind)


def para(text,x,top,width,size=11.5,leading=18,color=INK):
    s=ParagraphStyle("p",fontName="JP",fontSize=size,leading=leading,textColor=color,wordWrap="CJK",splitLongWords=True)
    p=Paragraph(escape(text).replace("\n","<br/>"),s)
    _,height=p.wrap(width,1000)
    p.drawOn(C,x,top-height)
    return height


def cards(items,top):
    gap=18; width=(CW-2*gap)/3
    for i,(title,points) in enumerate(items):
        x=M+i*(width+gap)
        C.setStrokeColor(colors.HexColor("#dbe6ec"));C.line(x,top,x+width,top)
        y=top-14
        y-=para(title,x,y,width,size=13,leading=20,color=TEAL)+9
        for point in points:
            C.setFillColor(TEAL);C.circle(x+2,y-6,1.5,fill=1,stroke=0)
            h=para(point,x+12,y,width-12,size=10.7,leading=16.4)
            y-=h+9
        if y<100: raise ValueError(f"Card overflow on {title}: {y}")


def table(header,rows,top):
    n=len(header)
    widths=([115,190,255,CW-560] if n==4 else [130,285,CW-415])
    if header[0]=="順序": widths=[50,405,CW-455]
    if n==2: widths=[170,CW-170]
    style=ParagraphStyle("cell",fontName="JP",fontSize=10.3,leading=14.3,wordWrap="CJK")
    head=ParagraphStyle("head",parent=style,textColor=colors.white)
    content=[[Paragraph(escape(v),head) for v in header]]
    content += [[Paragraph(escape(v),style) for v in row] for row in rows]
    t=Table(content,colWidths=widths,hAlign="LEFT")
    t.setStyle(TableStyle([("BACKGROUND",(0,0),(-1,0),INK),("ROWBACKGROUNDS",(0,1),(-1,-1),[colors.white,PAPER]),("VALIGN",(0,0),(-1,-1),"TOP"),("LEFTPADDING",(0,0),(-1,-1),12),("RIGHTPADDING",(0,0),(-1,-1),12),("TOPPADDING",(0,0),(-1,-1),6),("BOTTOMPADDING",(0,0),(-1,-1),6),("LINEBELOW",(0,0),(-1,0),1,TEAL)]))
    _,height=t.wrap(CW,1000);t.drawOn(C,M,top-height)
    return top-height


def build():
    global C
    PDF.parent.mkdir(parents=True,exist_ok=True);IMAGES.mkdir(parents=True,exist_ok=True)
    C=canvas.Canvas(str(PDF),pagesize=(W,H))
    C.setTitle("ファイル取込処理 図解ガイド");C.setAuthor("2026-file-scan")
    C.setSubject("現行設計とプログラムに基づく、形式別解析・チャンク化・Embeddingの説明")
    md=["# ファイル取込処理 図解ガイド\n", "確認日：2026-11-01／基準コミット`c482b66`から文書取込に限定した修正版。初めて読む方向けの説明資料です。\n", "[図解PDFを開く](../../output/pdf/ファイル取込処理_図解ガイド.pdf)\n", "図中の業務内容・数値ベクトル・座標は説明用です。設定値は同梱設定とコードの既定値を示し、実行環境の設定値を保証するものではありません。\n", "## 目次\n"]
    md += [f"{i}. {p['title']}" for i,p in enumerate(PAGES,1)]
    md.append("")
    bounds=[]
    for num,p in enumerate(PAGES,1):
        C.setFillColor(colors.white);C.rect(0,0,W,H,fill=1,stroke=0)
        C.setFillColor(TEAL);C.rect(M,H-36,34,4,fill=1,stroke=0)
        para(p["tag"],M+46,H-27,CW-46,size=10,leading=14,color=TEAL)
        para(p["title"],M,H-57,CW,size=23,leading=32)
        para(p["lead"],M,H-102,CW,size=12,leading=20,color=MUTED)
        md += [f"\n## {num}. {p['title']}\n",p["lead"]+"\n"]
        if p.get("diagram"):
            d=diagram(p["diagram"]);y=H-141-d.height
            renderPDF.draw(d,C,M,y)
            svg=IMAGES/f"{num:02d}_{p['diagram']}.svg"
            renderSVG.drawToFile(d,str(svg))
            svg.write_text(svg.read_text().replace("font-family: JP", "font-family: 'Arial Unicode MS', 'Hiragino Kaku Gothic ProN', sans-serif"))
            bounds.append(dict(page=num,x=M,y=y,w=CW,h=d.height,name=svg.stem))
            cards(p["cards"],y-14)
            md.append(f"![{p['title']}](images/{svg.stem}.png)\n")
            for title,points in p["cards"]:
                md.append(f"### {title}\n")
                md += [f"- {q}" for q in points];md.append("")
        else:
            bottom=table(*p["table"],top=H-151)
            h=para(p["bottom"],M,bottom-18,CW,size=11,leading=18)
            if bottom-18-h<100: raise ValueError(f"Table overflow {num}")
            heads,rows=p["table"]
            md.append("| "+" | ".join(heads)+" |")
            md.append("| "+" | ".join(["---"]*len(heads))+" |")
            md += ["| "+" | ".join(r)+" |" for r in rows]
            md.append("\n"+p["bottom"]+"\n")
        C.setFillColor(PALE);C.roundRect(M,65,CW,32,5,fill=1,stroke=0)
        para(p["note"],M+10,89,CW-20,size=9.2,leading=13)
        src="参照："+" ／ ".join(p["sources"])
        para(src,M,55,CW-50,size=7.5,leading=10,color=MUTED)
        C.setFont("JP",9);C.setFillColor(MUTED);C.drawRightString(W-M,29,f"{num:02d} / {len(PAGES):02d}")
        md.append("> "+p["note"]+"\n")
        refs=[]
        for source in p["sources"]:
            file,sep,line=source.partition(":")
            refs.append(f"[{source}](../../{file}{'#L'+line if sep else ''})")
        md.append("根拠："+"、".join(refs)+"\n")
        C.showPage()
    C.save()
    expected_stems = {entry["name"] for entry in bounds}
    for pattern in ("[0-9][0-9]_*.png", "[0-9][0-9]_*.svg"):
        for asset in IMAGES.glob(pattern):
            if asset.stem not in expected_stems:
                asset.unlink()
    MD.write_text("\n".join(md),encoding="utf-8")
    poppler=shutil.which("pdftoppm") or str(Path.home()/".cache/codex-runtimes/codex-primary-runtime/dependencies/bin/override/pdftoppm")
    if not Path(poppler).is_file():
        raise RuntimeError("図のPNG作成にはpdftoppmが必要です。Popplerを用意してください。")
    with tempfile.TemporaryDirectory(prefix="file-ingestion-guide-") as folder:
        prefix=Path(folder)/"page"
        subprocess.run([poppler,"-r","120","-png",str(PDF),str(prefix)],check=True,capture_output=True)
        for b in bounds:
            with Image.open(Path(folder)/f"page-{b['page']:02d}.png") as im:
                sx,sy=im.width/W,im.height/H
                rect=(int(b['x']*sx),int((H-b['y']-b['h'])*sy),int((b['x']+b['w'])*sx),int((H-b['y'])*sy))
                im.crop(rect).save(IMAGES/(b['name']+".png"))
    print(f"Created {PDF}\nCreated {MD}\nPages: {len(PAGES)} / Diagrams: {len(bounds)}")


if __name__=="__main__":
    build()
