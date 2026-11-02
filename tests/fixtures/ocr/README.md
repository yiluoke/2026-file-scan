# 日本語横書きOCRテスト画像

`japanese-horizontal.png`はこのプロジェクト用に作成した架空の文章を1500×2000 pixelsの白背景へ描画した画像。実書籍・業務資料は含まない。対応する原文は`japanese-horizontal.txt`。

作成時はPillow 12.1.1、macOSのArial Unicodeフォント52px、左上(100, 180)から行間150pxで描画した。フォントファイルは同梱していない。PDF生成時は540×720 points全面へ配置し、文字層を加えない。再現用のPDF生成はtests/test_rapidocr.pyのscan_pdfを参照する。

文字層のない日本語画像を実OCRできることの回帰テスト用であり、スキャン品質・ルビ・表組・書籍全体の精度を代表する評価セットではない。
