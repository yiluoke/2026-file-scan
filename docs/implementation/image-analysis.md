# PDF・Excel・Wordの画像認識

`file-scan ingest`は、設定で有効にした形式の画像をOpenAIのResponses APIで解析する。生成した説明は原図と位置にひもづく下書きとして保存し、既存のチャンク分割・Embedding・明示公開の手順で検索に載せられる。

## 設定と運用

既存の設定ファイルに以下を追加する。画像を外部送信できる収集元に適用し、実行環境の`OPENAI_API_KEY`に認証情報を設定する。

```yaml
image_analysis:
  enabled: true
  formats: [pdf, xlsx, xlsm, docx]
  model: gpt-5.4-mini-2026-03-17
  api_key_env: OPENAI_API_KEY
  render_office: true
  max_images_per_document: 20
  timeout_seconds: 60
```

- `enabled`の既定は`false`。たとえば`formats: [xlsx, xlsm]`とするとExcelだけを対象とする。画像認識と`embedding.enabled`は別々に制御する。
- `render_office: true`では、未処理の図形・グラフが検出されたOfficeファイルをLibreOfficeでプレビューPDFに変換する。対象ホストの実行ユーザーのPATHに`soffice`を用意する。貼り付け画像の処理だけを使う場合は`render_office: false`を指定する。
- `max_images_per_document`は文書ごとに解析を試みる異なる画像の上限。同じ画像の結果は同一文書の処理中に再利用する。APIのタイムアウトとOffice変換のタイムアウトは、それぞれ`timeout_seconds`を適用する。

設定後に通常の`file-scan ingest --config config.local.yaml`を実行する。設定を有効にした形式では変換プロファイルが変わり、既存原本にも新しい世代が作られる。同じ原本と設定では既存世代を再利用する。APIエラー、上限到達、判読不能などで部分完了になった文書を同じ設定で再試行する場合は`ingest --retry-partial`を使う。

## 認識する画像と保存結果

- PDFでは検出した図領域と取り出せた原画像を対象とする。OCR本文にできた全面スキャンの背景は従来のOCR処理で扱う。
- Excel・Wordの貼り付け画像は埋め込み画像を対象とする。Office図形を画像化する場合は、プレビューPDF上で検出できた描画領域をページごとに切り出す。位置にはプレビューのページ番号・座標を記録する。同じページの複数の図形が一つの領域に入る場合や、印刷範囲・改ページ・フォントの違いで図形が欠ける場合がある。マクロ付きや外部リンクを持つ文書などは描画できない場合があり、その場合は警告を残す。
- Responses APIに送るのは、対象画像をPNGに整えたものと認識指示。切り出し範囲に見える周辺文字も画像に含まれ得る。`store: false`を指定する。
- 読めた文字、見えている配置や接続の説明、判読不能箇所を別の項目に保存する。文字が読めない場合でも、見えている構造の説明をチャンクにできる。APIで読み取った文字と説明には、原図から生成した情報であることを示す`artifact_type=derived`を付ける。元画像と抽出済みの原文はそれぞれ原本由来の情報として保存する。
- 解析結果は`document.json`と`review.md`で確認できる。画像への参照、出現位置、使用モデル、警告は`document.json`に記録する。未処理のOffice要素の警告は、描画領域の認識に成功しても保持する。

## 検索への反映

1. `embedding.enabled: true`で`file-scan embed`を実行すると、説明チャンクもベクトル化される。
2. 原図と説明を照合したうえで、通常の`file-search publish`に`--include-image-descriptions`を付ける。部分完了の世代を公開するときは`--allow-partial`も指定する。
3. 検索結果には説明であることを示す`artifact_type: derived`、元画像の参照、原本またはプレビュー上の位置が付く。`artifact_type`条件で原文と説明を選べる。

[OpenAI Docsの画像入力ガイド](https://developers.openai.com/api/docs/guides/images-vision#analyze-images)・[既定モデルの仕様](https://developers.openai.com/api/docs/models/gpt-5.4-mini)
