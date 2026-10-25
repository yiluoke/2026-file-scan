# DoclingによるPDF抽出

PDFの標準抽出方式はDocling。事前取得したレイアウト解析モデルとTableFormerをCPUで実行し、本文・見出し・表・原本ページ・領域座標を保存する。`native`を明示指定した場合だけ従来のpypdf本文抽出を使用する。

## 導入と設定

公式`docling-slim==2.140.0`の`convert-core`・`extract-core`・`format-pdf`・`models-local`を依存に含める。OpenCVも明示導入する。実行するDocling本体と実モデルは従来と同じ。独自チャンク処理で使わないchunking・OCR用追加依存を避ける。Python 3.14.4で実PDFの本文・表・bboxを検証した。版の固定理由は[Python版整合の記録](ubuntu-26.04-python.md)を参照。

```sh
source .venv/bin/activate
python -m pip install -r requirements/ubuntu-26.04.lock
python -m pip install --no-deps --no-build-isolation -e '.[dev,search]'
python scripts/download_docling_models.py --config config.local.yaml
```

設定例と実際のローカル設定は次のとおり。

```yaml
pdf_backend: docling
docling_artifacts_path: data/models/docling
```

モデルの取得先はYAMLの所在を基準に解決する。取得コマンドはDocling公式配布の`docling-project/docling-layout-heron`と、`docling-project/docling-models`のTableFormer accurateをダウンロードする。使用しない画像説明・OCR・VLM用モデルは取得しない。

取得対象の固定コミットと期待ファイル・SHA-256は`src/file_scan/docling-models.lock.json`で管理する。通常の取得はlockを再現し、配置記録を`data/models/docling/model-manifest.json`へ保存する。利用前に版・ファイル一覧・サイズ・ハッシュを照合する。変更のない配置はプロセス内で検査結果を再利用し、モデルの更新時はPDFだけ別の抽出プロファイルになる。

```sh
# 配置済み実体の検査（ネットワーク接続なし）
python scripts/download_docling_models.py --config config.local.yaml --verify-only
# モデルを更新する場合だけ、固定コミットと期待ハッシュも更新する
python scripts/download_docling_models.py --config config.local.yaml --update-lock
```

lockの更新は依存版・抽出結果と合わせてレビューする。通常の取得でmainを再解決しない。詳細は[取り込み改善設計](../design/05-ingestion-improvements.md)。

## 実行内容

- PDFのレイアウトと本文の読み順を解析する。
- 見出し、段落、表等を共通ブロックへ変換する。表はMarkdown本文とセル構造を保持する。
- ページ番号、bbox、座標系を原本位置として保持する。
- 図として検出した領域はPNGとbboxを保存し、画像の意味解析待ちを記録する。
- PDF内の原画像はpypdfの画像取得機能で別途保持する。pypdfから抽出した本文をDoclingの本文へ混入させない。
- 見出しだけの短いPDFも、原本ブロックと文字範囲を保持したチャンクを作る。
- OCR、外部画像説明、外部サービス、外部プラグインは無効。文字のないページはOCR待ちの部分抽出として扱う。
- モデル不足やDoclingの変換失敗をpypdf本文抽出へ自動で置き換えない。

原本はローカルで処理する。インターネット接続が必要なのは、ライブラリ導入と明示的なモデル取得時である。

## 取り込みと検証

```sh
set -a
source .env
set +a
file-scan ingest --config config.local.yaml
python scripts/test_postgres.py
```

モデル初回読込を含め、文書ごとに`parse_timeout_seconds`の制限を適用する。解析子プロセスとDocling Converterは文書間で再利用し、タイムアウト・タスク数・処理後メモリ閾値でワーカーを交換する。既定では1プロセスで文書を順に処理する。公開PDFの`dummy.pdf`と`table.pdf`を実モデルで検証し、後者の表構造と両方のbboxを確認する。画像だけのページは画像を保持し、OCR未実施を成功扱いにしない。

`tests/test_docling.py`は実モデルを使う検証であり、モデルや公開サンプルが未配置の場合はスキップする。`tests/test_public_samples.py`は同じ公開PDFを含む8原本の実PostgreSQL登録・再利用を検証する。`native`の単体テストは明示設定で残す。

[Docling公式：モデル事前取得とオフライン利用](https://docling-project.github.io/docling/usage/advanced_options/)
