> この比較は改善適用前の調査記録。調査後の8改善は[取り込み改善設計（v3）](../design/05-ingestion-improvements.md)へ反映済み。

# ファイル取り込み処理に近いGitHubプロジェクトの調査

調査日：2026年10月25日（日本時間）

現在の取り込み処理に近い公開プロジェクトは複数ある。調査範囲では、**処理全体の構成はDocs2DB、差分更新の仕組みはCocoIndex、Docling＋OpenAI Embedding＋pgvectorという組み合わせはDocpipeが特に近い**と判断した。

この順位は機能・構成の比較による定性的な評価であり、コード一致率や性能測定値ではない。候補プロジェクトのREADME、公式ドキュメント、主要ソースを確認した。候補をインストールしたり、同じ原本で抽出精度・性能を比較したりする実行検証は行っていない。

## 1. 比較対象となる現在の実装

| 観点 | 現在の処理 |
| --- | --- |
| 収集 | マウント済み共有フォルダを想定したルートの再帰列挙。拡張子・フォルダ・ファイル名の除外 |
| 原本保全 | コピー中の変更を検出し、内容ハッシュ単位で原本・画像を保管。保管物の欠落・改変を確認 |
| 形式別抽出 | PDFはDocling実モデル、WordはOOXML、Excelはopenpyxl＋OOXML、PythonはAST、テキストは文字コードを判定 |
| 原本との対応 | 行、ページとbbox、シートとセル、段落等を保持。チャンクから原本ブロックの文字範囲を追跡 |
| 差分・世代 | 文書ID、原本ハッシュ、パーサー実装・設定・モデルmanifestを含む変換プロファイルから世代を識別 |
| Embedding | 明示的なembedコマンドでOpenAI APIを使用。入力ハッシュとモデル・次元別プロファイルで再利用 |
| DB | PostgreSQL＋pgvector。文書、原本版、抽出世代、ブロック、チャンク、画像参照、ジョブを管理 |
| 品質・運用 | 成功、部分抽出、失敗、画像解析待ちを区別。ジョブ、排他、再試行、確認用JSON・Markdownを出力 |
| 公開 | draftだけを保存。検索API・公開承認・利用者ACLは未実装 |

根拠となる本プロジェクトのソース：[collector.py](../../src/file_scan/collector.py)、[storage.py](../../src/file_scan/storage.py)、[parsers.py](../../src/file_scan/parsers.py)、[pipeline.py](../../src/file_scan/pipeline.py)、[embeddings.py](../../src/file_scan/embeddings.py)、[db.py](../../src/file_scan/db.py)。OpenAIは実装済みだが仮キーで無効化しており、実キーでの生成は未検証。

## 2. 候補の一覧

| プロジェクト | 類似性の評価 | 特に近い部分 | 主な違い |
| --- | --- | --- | --- |
| [rhel-lightspeed/docs2db](https://github.com/rhel-lightspeed/docs2db) | 非常に高い：段階別の取り込み処理 | フォルダ→Docling→チャンク→Embedding→PostgreSQL/pgvector。中間JSON、処理metadata、再利用 | ディスク上の中間ファイルを正本としてDBを生成する設計。確認したEmbedding実装はGranite |
| [cocoindex-io/cocoindex](https://github.com/cocoindex-io/cocoindex) | 高い：差分処理エンジン | 再帰的ファイル収集、Docling PDF変換、チャンク、pgvector。入力・コード変更を追跡する増分処理 | 宣言型の汎用エンジン。PDF例のEmbeddingはローカルMiniLM。原本保管・draftの業務モデルは別途必要 |
| [thesunnysinha/docpipe](https://github.com/thesunnysinha/docpipe) | 高い：利用技術と処理段階 | Docling、OpenAI Embedding、pgvector、source fingerprint、再利用、CLI/SDK/API | 文書・ページ中心の標準経路。現在の詳細な原本対応・抽出世代・フォルダ同期仕様とは個別に比較が必要 |
| [run-llama/llama_index](https://github.com/run-llama/llama_index) | 高い：部品を組み合わせる場合 | SimpleDirectoryReader、DoclingReader、IngestionPipeline、OpenAIEmbedding、PGVectorStore | 完成した同等アプリではなくフレームワーク。安定ID、docstore永続化、原本保全等を組み立てる必要 |
| [docling-project/docling-pipelines](https://github.com/docling-project/docling-pipelines) | 高い：Docling中心のETL構成 | ローカル・各種共有サービス、Docling、分割、Embedding、差分、削除同期、ジョブ・レポート | 確認した標準ベクトル保存先はOpenSearch/Milvus。PostgreSQLの増分状態管理とpgvector保存を区別する必要 |
| [Unstructured-IO/unstructured-ingest](https://github.com/Unstructured-IO/unstructured-ingest) | 中〜高：収集・加工・保存の流れ | 列挙、フィルタ、ダウンロード、partition、chunk、embed、upload。OpenAIとpgvector対応 | 抽出はUnstructuredが中心。現在のDocling・Word/Excel補完処理と抽出結果の意味が異なる |

## 3. 特に近いプロジェクトの確認結果

### 3.1 Docs2DB

構成全体の比較では最も近い。`ingest`、`chunk`、`embed`、`load`をCLIの段階として分け、Docling JSON、チャンク、Embedding、処理metadataを保存してからPostgreSQLへロードする。検索側は別のdocs2db-apiに分けている。[README](https://github.com/rhel-lightspeed/docs2db/blob/3e0315b74d3d9c7c25c016f626b12b2ac7a87553/README.md)、[設計](https://github.com/rhel-lightspeed/docs2db/blob/3e0315b74d3d9c7c25c016f626b12b2ac7a87553/docs/DESIGN.md)

確認したコードには、元ファイルのハッシュをmetadataへ保存する処理と、チャンクのハッシュ・モデル・次元が変わった場合のEmbedding再生成判定がある。CLI取り込みの入口では中間ファイルと原本のmtimeを比較してスキップし、別の更新判定APIにはハッシュ比較もある。現在の実装の「原本ハッシュ＋変換プロファイルによる世代」と同一ではない。[ingest.py](https://github.com/rhel-lightspeed/docs2db/blob/3e0315b74d3d9c7c25c016f626b12b2ac7a87553/src/docs2db/ingest.py#L133)、[embeddings.py](https://github.com/rhel-lightspeed/docs2db/blob/3e0315b74d3d9c7c25c016f626b12b2ac7a87553/src/docs2db/embeddings.py)

重要な違いは、ディスク上の中間ファイルを正本とする点。現在の実装はDBに登録した抽出結果を確認ファイルへ出力し、確認ファイルをDBへ逆輸入しない。また、調査コミットの`EMBEDDING_CONFIGS`はGranite 30M Englishを登録している。READMEのOpenAI例はチャンクの文脈生成用LLMであり、OpenAI Embeddingの利用例と混同できない。[設計のSource of Truth](https://github.com/rhel-lightspeed/docs2db/blob/3e0315b74d3d9c7c25c016f626b12b2ac7a87553/docs/DESIGN.md)、[Embedding実装](https://github.com/rhel-lightspeed/docs2db/blob/3e0315b74d3d9c7c25c016f626b12b2ac7a87553/src/docs2db/embeddings.py)

参照価値が高いのは、段階ごとの再実行、処理metadataの持ち方、監査コマンド、検索側との責務分離である。既存のOpenAI用プロファイルや原本対応のテーブルをそのまま移行できるという意味ではない。

### 3.2 CocoIndex

公式PDF例には、ローカルフォルダの再帰走査、DoclingでのMarkdown化、分割、Embedding、PostgreSQLへの保存が実際に実装されている。READMEは入力とコードのハッシュを使うキャッシュと、変更分だけの再計算を説明している。[PDF例のmain.py](https://github.com/cocoindex-io/cocoindex/blob/main/examples/pdf_embedding/main.py)、[README](https://github.com/cocoindex-io/cocoindex/blob/main/README.md)

PDF例のチャンクにはファイル名と開始・終了位置が付く。ただしコード上、その位置はDoclingから生成したMarkdownの文字位置であり、PDF原本のbboxではない。また、この例のモデルは`sentence-transformers/all-MiniLM-L6-v2`。OpenAIへ合わせるにはEmbedding処理の差し替えが必要になる。[PDF例のデータ型・process_file](https://github.com/cocoindex-io/cocoindex/blob/main/examples/pdf_embedding/main.py#L66)

参照価値が高いのは、自前の差分・再計算管理を汎用エンジンで行う設計。現在の原本保管、詳細なブロック対応、draft制約を維持する場合は、そのデータモデルをエンジンの入出力へ接続する設計が必要になる。これは確認したソース構成からの評価である。

### 3.3 Docpipe

Doclingによる解析、OpenAI Embedding、pgvectorを構成で選択でき、Python SDK・CLI・HTTP APIを提供する。今回確認した候補の中では、現在使う技術の組み合わせが特に近い。[README](https://github.com/thesunnysinha/docpipe/blob/bb47200a52d0ebfe58d9bd0bb0f3937d06628d44/README.md)、[IngestionConfig](https://github.com/thesunnysinha/docpipe/blob/bb47200a52d0ebfe58d9bd0bb0f3937d06628d44/src/docpipe/core/schemas/ingestion.py)

`incremental.py`は入力バイト列と正規化metadataからSHA-256のfingerprintを作る。状態照会の失敗時は既定でfail-closedとする。pgvector側にはupsert、source単位の削除、metadataフィルタ等の境界がある。[incremental.py](https://github.com/thesunnysinha/docpipe/blob/bb47200a52d0ebfe58d9bd0bb0f3937d06628d44/src/docpipe/ingestion/incremental.py)、[pgvector adapter](https://github.com/thesunnysinha/docpipe/blob/bb47200a52d0ebfe58d9bd0bb0f3937d06628d44/src/docpipe/vectorstores/pgvector/adapter.py)

Docling adapterはページ別テキストとrawのDoclingDocumentを保持する。一方、確認したDocumentBuilderの標準ページ経路はsource・pageを中心とするmetadataで取り込み文書を作るため、rawに存在するbboxや表セルが、現在と同じ粒度でベクトル側まで追跡できるとは断定しない。[Docling parser](https://github.com/thesunnysinha/docpipe/blob/bb47200a52d0ebfe58d9bd0bb0f3937d06628d44/src/docpipe/parsers/docling_parser.py)、[DocumentBuilder](https://github.com/thesunnysinha/docpipe/blob/bb47200a52d0ebfe58d9bd0bb0f3937d06628d44/src/docpipe/ingestion/document_builder.py)

LocalSourceは許可ルート、サイズ上限、open時のファイル識別再確認を持つ。ただし単一sourceの解決・読み取り境界であり、現在のフォルダ全体の列挙・除外・削除判定を含む収集処理と同一とは扱わない。[LocalSource](https://github.com/thesunnysinha/docpipe/blob/bb47200a52d0ebfe58d9bd0bb0f3937d06628d44/src/docpipe/sources/local.py)

## 4. 組み合わせ・大規模化の参考になる候補

### 4.1 LlamaIndex

`IngestionPipeline`には変換のキャッシュ、文書IDとハッシュによるupsert、消えた文書を削除する戦略がある。`DoclingReader`、`OpenAIEmbedding`、`PGVectorStore`を組み合わせれば、現在と近い処理を構成できる。[IngestionPipeline](https://github.com/run-llama/llama_index/blob/main/llama-index-core/llama_index/core/ingestion/pipeline.py)、[PGVectorStore](https://github.com/run-llama/llama_index/blob/main/llama-index-integrations/vector_stores/llama-index-vector-stores-postgres/llama_index/vector_stores/postgres/base.py)、[パイプラインの公式説明](https://github.com/run-llama/llama_index/blob/main/docs/src/content/docs/framework/module_guides/loading/ingestion_pipeline/index.md)

確認したDoclingReaderはMarkdownとJSONの両形式を扱うが、既定の文書ID生成はUUID4。差分処理を現在のような安定IDで運用するなら`id_func`等を設定する必要がある。キャッシュ・docstoreの永続化も組み合わせる。[DoclingReader](https://github.com/run-llama/llama_index/blob/main/llama-index-integrations/readers/llama-index-readers-docling/llama_index/readers/docling/base.py)

### 4.2 Docling pipelines

Doclingを中心とする取り込み、分割、Embedding、品質処理、差分・削除同期、ジョブ・レポート等を備える。確認した最新版READMEでは、増分状態の保存先にParquetとPostgreSQLを挙げている。[調査コミットのREADME](https://github.com/docling-project/docling-pipelines/blob/ee43d188d52d12e9bb78bcc9d944b31efa294b66/README.md)

ただしベクトル保存の標準アダプタとして確認したのはOpenSearchとMilvusであり、「PostgreSQLに状態を保存できる」ことは「pgvectorへベクトルを保存できる」ことと別である。現在のpgvector構成を継続するなら、出力アダプタの追加・接続を検討する必要がある。[ベクトル保存先の説明](https://github.com/docling-project/docling-pipelines/blob/ee43d188d52d12e9bb78bcc9d944b31efa294b66/README.md)、[アダプタfactory](https://github.com/docling-project/docling-pipelines/blob/ee43d188d52d12e9bb78bcc9d944b31efa294b66/src/docpipe/core/operators/vectordb/adapters/outbound/factories/vector_store_factory.py)

なお、`docling-project/docling-pipelines`と`thesunnysinha/docpipe`は別プロジェクトである。確認したソースは双方とも`docpipe`というPython名前空間を使用している。

### 4.3 Unstructured Ingest

公式の取り込みフローは、Index、Filter、Download、Partition、Chunk、Embed、Upload等に分かれ、現在の収集・抽出・分割・登録と対応付けやすい。Local sourceはフォルダとglob指定を持ち、OpenAI Embeddingの設定とPostgreSQL/pgvectorの出力先もある。[取り込みフロー](https://docs.unstructured.io/open-source/ingestion/overview)、[Local source](https://docs.unstructured.io/open-source/ingestion/source-connectors/local)、[Embedding設定](https://docs.unstructured.io/open-source/ingestion/ingest-configuration/embedding-configuration)、[PostgreSQL出力](https://docs.unstructured.io/open-source/ingestion/destination-connectors/postgresql)

DoclingではなくUnstructuredのpartition処理が中心になるため、抽出器をそのまま置き換える評価と、収集・出力の設計だけ参照する評価を分ける必要がある。公式OverviewはAPI利用を推奨し、OSSのCLI・PythonライブラリがAPIの全機能を追っているわけではないと説明している。リポジトリ全体が更新停止しているという意味ではない。[公式Overview](https://docs.unstructured.io/open-source/ingestion/overview)

## 5. 現在の仕様を維持する場合の確認点

以下は「他プロジェクトに存在しない」と断定したものではなく、今回の標準例・主要ソースだけでは同等性を確認できなかった部分である。

1. 原本コピー中の変更検知、ハッシュ単位での保管、保管済み実体の改変確認。
2. 原本版と抽出世代の分離、パーサー実装・モデルmanifestまで含める再生成条件。
3. Excelのセル・数式・保存済み結果、Wordの段落・表順序等の補完抽出。
4. チャンク→ブロック→原本の文字範囲・bbox・セル位置を追跡するDB上の対応。
5. 成功と部分抽出を区別し、OCR・画像解析待ちを成功扱いにしない運用。
6. draft限定保存、公開前の検証、API呼出を取り込みと分けて制御する運用。
7. 収集失敗と原本削除を区別し、一時的な共有フォルダの障害で誤削除しないこと。

したがって、類似する部品・処理は多いが、現在の業務データモデルを無調整で置き換えられるとまでは確認できない。

## 6. 参照する優先順位

- 全体のCLI・中間生成物・DBロードの設計を比較する：Docs2DB。
- 差分更新・変更伝播・再計算の仕組みを比較する：CocoIndex。
- 同じDocling／OpenAI／pgvector構成でSDK・API化を比較する：Docpipe。
- 抽出・分割・Embeddingの部品を組み合わせる方式を比較する：LlamaIndex。
- 多数の収集元・ジョブ・大規模処理へ広げる設計を比較する：Docling pipelines、Unstructured Ingest。

現時点の結論は、**類似度の高いプロジェクトは存在し、Docs2DB・CocoIndex・Docpipeを最初の比較対象にすると具体的な差が見える**というもの。導入・置換の結論には、同じ原本での原本位置・表セル・再実行・削除・部分失敗の比較が必要になる。

## 7. 調査スナップショット

次の3件はGitHub APIからmainのコミットを解決し、そのコミットの主要ソースを読み取った。コードの実行・依存の導入は行っていない。

| リポジトリ | 調査コミット | APIで確認したライセンス識別子 |
| --- | --- | --- |
| rhel-lightspeed/docs2db | `3e0315b74d3d9c7c25c016f626b12b2ac7a87553` | Apache-2.0 |
| thesunnysinha/docpipe | `bb47200a52d0ebfe58d9bd0bb0f3937d06628d44` | MIT |
| docling-project/docling-pipelines | `ee43d188d52d12e9bb78bcc9d944b31efa294b66` | MIT |

CocoIndex、LlamaIndex、Unstructuredは本文にリンクしたmainのソース・公式ドキュメントを確認した。公開情報とmainは更新され得るため、調査日以降の動作を固定する資料ではない。

取得したソースとGitHub metadataは、Git管理対象外の`artifacts/research/github-ingestion/`へ保存した。
