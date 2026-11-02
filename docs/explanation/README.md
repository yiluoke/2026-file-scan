# ファイル取込処理の説明資料

現行設計とプログラムを照合し、初めて読む人向けに文書の解析・チャンク化・ベクトル化を説明した資料です。プログラム解析等を対象外とする仕様に加え、2026-11-02に専用スキーマ`winnap`対応を反映しました。確認対象は現在の作業ツリーです。

- [図解PDF（15ページ）](../../output/pdf/ファイル取込処理_図解ガイド.pdf)：閲覧・配布・印刷用。
- [Markdown版](ファイル取込処理_図解ガイド.md)：本文の参照・編集用。PDFと同じ内容で、根拠ファイルへのリンク付き。
- [図の素材](images/)：11図のPNGとSVG。SVGは拡大や図の編集に使用できます。

TXT・Markdown、Excel、Word、PDFの解析例と、画像・スキャンの現在の制限を図解しています。共通する分割、Embedding、保存先、再実行、運用状態、設計書と現在の実装の違いも含みます。

## 確認内容

- 設計書・実装説明と `src/file_scan/` の処理を照合。検索公開の境界について `src/search/` も確認。
- 実PostgreSQL・pgvector・PGroongaを使った全218テストが成功、スキップなし。文書取込・検索・Docling実モデル、スキーマ移行時のデータ保持とrollbackも確認。
- トークン辞書の自動取得はネットワーク制約で失敗したため、既存の `data/tokenizer-cache/cl100k_base.tiktoken` を一時キャッシュへ配置して再実行し、全件成功を確認。
- PDF全15ページをPNGへ変換し、文字化け、重なり、はみ出し、図表、ページ番号を目視確認。PDF内の日本語テキストとMarkdownのリンク先も検査。
- 実OpenAI API送信・業務DBの移行、検索精度・処理性能の測定は実施していません。

資料作成のための業務データ取込・業務DB移行・公開は行っていません。プログラム本体・同梱設定・ローカル設定も文書専用の対象範囲へ修正済みです。既存環境への適用は[文書形式への限定](../implementation/document-only-ingestion.md)を参照してください。

専用スキーマの作成・旧public配置からの移行は[winnapの手順](../implementation/winnap-schema.md)を参照。

## 資料を更新する場合

本文・図の定義は [build_guide.py](build_guide.py) にあります。この定義を編集して再生成すると、PDF・Markdown・SVG・PNGをまとめて更新できます。

必要なもの：Python、`reportlab`、`Pillow`、Popplerの `pdftoppm`、日本語を表示できるTrueTypeフォント。既定ではmacOSのArial Unicodeを使用します。別環境では `GUIDE_FONT_PATH` に対応フォントのパスを指定してください。

```sh
python docs/explanation/build_guide.py
```

生成後はPDFを再度画像化し、全ページの見た目を確認してください。設定値や確認済み件数・コミットも、確認し直した内容に合わせて更新します。
