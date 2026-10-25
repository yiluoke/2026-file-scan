# 検索APIに近いGitHubプロジェクトの比較

調査日：2026-10-25（日本時間）。設計への反映先：[検索基盤・検索API設計](../design/02-search-platform.md)。

検索処理の構成はDocs2DB-API、SQLによる順位統合と条件検証はMicrosoftのPostgreSQLサンプル、認証済み主体から検索フィルターを組み立てる責務分割はOnyxが参考になる。**各プロジェクトから設計上の考え方を取り入れ、既存の取り込み・原本・業務版・ACLモデルを維持する**方針とした。

READMEだけでなく、以下のコミットの主要ソースと一部テストを確認した。候補アプリの起動、日本語文書による検索実験、速度・精度の比較は行っていない。類似度は定性的な判断であり、導入を推奨する性能評価ではない。

## 1. 調査時点の実装と設計の差

以下の表は実装前の2026-10-25時点。続く実装では、PGroonga・FastAPI・3経路検索・公開管理・coverageを追加した。実装後の状態と制約は[検索APIの操作・実装](../implementation/search-api.md)を参照。

| 項目 | 2026-file-scanの現在の状態 | 検索APIを公開する前に必要なこと |
| --- | --- | --- |
| 取り込み | PostgreSQLスキーマv3。原本、世代、ブロック、チャンク、原本文字範囲を保持 | 取り込みと検索を別サービス境界にする |
| Embedding | OpenAI生成、pgvector保存、入力ハッシュとプロファイル単位の再利用 | 質問Embeddingを同じ空間で生成し、入力・次元・値を検査 |
| 世代選択 | `selected_generations`は取り込みの下書き選択 | 業務版別の採用・公開・準備状態を導入。下書きをそのまま検索しない |
| 検索 | API・PGroonga検索・順位統合は未実装。FastAPIは依存に未追加。チャンク識別子は主にsymbol由来 | 共通検索サービス、入力検証、3経路、RRF、出典・予算 |
| 公開・ACL | draftのみ、ナレッジ無効、ACL未同期に制約 | 公開管理・ACL・採用版のDB移行と運用。移行前は検索公開しない |
| 不足情報 | 予定原本・失敗をAPIへ返す経路は未実装 | ヒット集合と予定対象の集計を分離し、同じ読み取りで返す |

確認先：[db.py](../../src/file_scan/db.py)、[state.py](../../src/file_scan/state.py)、[embeddings.py](../../src/file_scan/embeddings.py)、[pyproject.toml](../../pyproject.toml)。現行の`embedding_profiles`はprovider・model・dimensions・input_versionが中心で、04の将来設計にあるモデル版・距離等との橋渡しも必要である。

## 2. 確認対象と固定コミット

| 対象 | 確認コミット | 本設計への主な用途 |
| --- | --- | --- |
| [rhel-lightspeed/docs2db-api](https://github.com/rhel-lightspeed/docs2db-api) | `c1ce57ef299fa8c9b244ee5499fd3941bb7785f7` | 取り込みとの分離、検索処理の段階化、RRF、文脈予算 |
| [Azure-Samples/rag-postgres-openai-python](https://github.com/Azure-Samples/rag-postgres-openai-python) | `2c23c488bb8698159739b0a01c22396b788f7de6` | SQLの候補統合、条件の許可リストとバインド、検証例 |
| [onyx-dot-app/onyx](https://github.com/onyx-dot-app/onyx) | `616a4b1a9c75cc07e9645069c672f3e8fb029ca8` | 主体からのACL生成と検索フィルターへの受け渡し |
| [langgenius/dify](https://github.com/langgenius/dify) | `12ab116fffd4706c20eb618d706cc71e094b519e` | 連携先の実際の外部検索呼び出し。置換候補ではない |

取得したコミットは調査時の既定ブランチの先頭であり、本システムが採用する製品版を意味しない。実際のDify配備版は別途固定して結合検証する。

## 3. 取り入れる点と取り入れない点

### 3.1 Docs2DB-API：最も近い検索側の構成

`UniversalRAGEngine`は質問処理、Embedding、hybrid取得、任意のreranker、返却件数・文脈量の処理を分ける。DB検索はベクトルと全文の候補を順位で統合する。取り込みをDocs2DB側へ分けている点も現在の構成と近い。[engine.py](https://github.com/rhel-lightspeed/docs2db-api/blob/c1ce57ef299fa8c9b244ee5499fd3941bb7785f7/src/docs2db_api/rag/engine.py#L563)、[database.py](https://github.com/rhel-lightspeed/docs2db-api/blob/c1ce57ef299fa8c9b244ee5499fd3941bb7785f7/src/docs2db_api/database.py#L390)

参考にするのは、HTTPから独立した検索サービス、内部候補と返却上限の分離、各段階の計測である。確認した全文SQLはREADMEのBM25表記に対し、実装では`ts_rank`と英語の`websearch_to_tsquery`を使う。これを日本語向けBM25実装とみなさず、本文検索はPGroongaで検証する。

RRF値とcosineの閾値は別尺度であり、同コードも処理を区別している。本設計ではさらにDify向けスコアの版を独立させる。文字数を4で割るトークン概算、質問・設定の詳細ログは採用せず、既存の実測トークン計数と本文を残さない監視を使う。[後処理](https://github.com/rhel-lightspeed/docs2db-api/blob/c1ce57ef299fa8c9b244ee5499fd3941bb7785f7/src/docs2db_api/rag/engine.py#L961)

既存スキーマへ直接導入する依存ライブラリにはしない。原本の版、承認済み公開、予定資料の不足、原本位置の検証を本プロジェクト側で一貫して管理する必要がある。

### 3.2 Microsoftサンプル：小さなSQL実装と条件検証

`PostgresSearcher`はベクトルと全文の候補をCTEへ分け、`FULL OUTER JOIN`してRRFを計算する。両経路に同じフィルターを入れ、カラム・演算子の許可リストと値のバインドを分けている。[postgres_searcher.py](https://github.com/Azure-Samples/rag-postgres-openai-python/blob/2c23c488bb8698159739b0a01c22396b788f7de6/src/backend/fastapi_app/postgres_searcher.py#L13)

本設計でも許可済みフィルターを一度検証し、全経路に共通適用する。値にSQLらしい文字列が含まれてもSQL本文へ連結しない試験は参考になる。[tests](https://github.com/Azure-Samples/rag-postgres-openai-python/blob/2c23c488bb8698159739b0a01c22396b788f7de6/tests/test_postgres_searcher.py)

対象は商品行の検索で、日本語文書の世代・原本ACLの実装例ではない。英語FTS、固定候補20件、結果行を1件ずつ取り直す処理はそのまま採用せず、候補上限を設定化し、認可条件付きの一括取得にする。識別子経路と予定原本のcoverageを別途加える。

### 3.3 Onyx：ACLと利用者フィルターの責務分割

認証済み`User`からACLを作る`build_access_filters_for_user`と、利用者の絞り込み条件を検索用の`IndexFilters`へまとめる処理が分かれている。検索実行側はそのフィルターを受け取る。[access_filters.py](https://github.com/onyx-dot-app/onyx/blob/616a4b1a9c75cc07e9645069c672f3e8fb029ca8/backend/onyx/context/search/preprocessing/access_filters.py)、[pipeline.py](https://github.com/onyx-dot-app/onyx/blob/616a4b1a9c75cc07e9645069c672f3e8fb029ca8/backend/onyx/context/search/pipeline.py#L260)、[search_runner.py](https://github.com/onyx-dot-app/onyx/blob/616a4b1a9c75cc07e9645069c672f3e8fb029ca8/backend/onyx/context/search/retrieval/search_runner.py#L51)

参考にするのは、ユーザー入力とサーバーが作った認可条件を別の型・責任で扱う点である。現在の検索計画でも`scope`が認可と版を確定し、各経路・文脈取得が同じ制約を使う。本プロジェクトのDBスキーマ、Dify共有キー、業務リリースにそのまま適用できるとは判断していない。

Onyx全体の導入やEnterprise機能の移植は行わない。確認したルートLICENSEでは通常部分と`ee`配下で条件が分かれる。今回は公開された通常部分の設計を参照しただけで、ソースの転載・依存追加はない。[LICENSE](https://github.com/onyx-dot-app/onyx/blob/616a4b1a9c75cc07e9645069c672f3e8fb029ca8/LICENSE)

### 3.4 Dify：文書だけでなく呼び出しコードを確認

確認コミットの`prepare_external_knowledge_retrieval`はBearerキーと`knowledge_id`、質問、検索設定を渡し、条件なしの場合は`metadata_condition: null`を作る。`execute_external_knowledge_retrieval`は200で`records`配列を読み、非200を検索エラーにする。[service.py](https://github.com/langgenius/dify/blob/12ab116fffd4706c20eb618d706cc71e094b519e/api/services/knowledge/external/service.py#L365)

このため、条件のnull受け入れ、200の空結果と障害の区別を明文化した。不足情報を独自フィールドに入れても標準連携では状態を使う保証がないため、既存方針どおりHTTP Requestノードで状態付きAPIを呼ぶ。共有Bearerキーが質問者のSSOを証明するとは扱わない。

## 4. 拡張の公式仕様から加えた条件

| 仕様 | 確認結果 | 設計上の判断 |
| --- | --- | --- |
| [pgvector Filtering](https://github.com/pgvector/pgvector#filtering)、[Iterative Index Scans](https://github.com/pgvector/pgvector#iterative-index-scans) | 近似索引では絞り込みにより返却候補が減り得る。対応版で反復走査を提供 | 正確検索を基準に、ACL・版の選択性別にRecallを比較。件数確保のために認可範囲を広げない |
| [PGroonga `&@`](https://pgroonga.github.io/reference/operators/match-v2.html)、[`&@~`](https://pgroonga.github.io/reference/operators/query-v2.html) | 単語検索と検索式を受ける演算子は異なる | 通常質問を検索式として解釈せず、語を抽出してバインド。語数・長さを制限 |
| [PGroonga score](https://pgroonga.github.io/reference/functions/pgroonga-score.html) | 逐次走査ではscoreが0になり得る | 実行計画を検証し、0を非関連と同一視しない。生スコアと互換スコアを分ける |
| [Dify External Knowledge API](https://docs.dify.ai/en/cloud/use-dify/knowledge/external-knowledge-api) | 標準契約はrecordsを返す。条件・比較演算子を定義し、metadataはオブジェクト | サービスの対応条件は許可リストで限定。不足は主経路の状態として返す |

## 5. 反映した設計判断

| 変更 | 反映先 | 理由 |
| --- | --- | --- |
| 各50件の候補、3経路RRF、安定した重複排除と順位 | 02 §4.2・4.5 | 内部候補とtop_kを分け、異なる評価尺度を直接加算しない |
| 固定上限による互換スコア、設定・スコア版の記録 | 02 §4.5・6.5 | 生RRFや質問ごとの最大値をDify閾値へ直結させない |
| 質問Embedding中にDB接続を保持しない | 02 §4.6 | 外部I/Oの遅延で長い読み取りトランザクションを作らない |
| expected_documentsとeligible_chunks、同一スナップショット | 02 §4.6 | 失敗してチャンクを作れなかった予定原本も不足に含める |
| 入力上限、条件の許可リスト、現行・履歴の解決規則 | 02 §6.2・6.3 | 不明な条件を無視せず、版・公開・ACLをサーバーで確定 |
| 文脈込みの件数・実測トークン・JSON上限、原文の追跡 | 02 §4.5・6.4 | 関連文書展開による予算超過と引用位置の不整合を防ぐ |
| エラーコード、期限、再試行、黙った縮退なし | 02 §6.7 | 空結果・収集不足・検索の実行障害を区別 |
| 公開移行から共有PoC、Dify結合、追加最適化へ段階導入 | 02 §9 | v3の下書き・キャッシュを公開根拠として使わない |

取り込みの比較は[別の調査記録](2026-10-25-github-ingestion-comparison.md)を参照。今回取得した公開ソースとGitHub metadataはGit管理対象外の`artifacts/research/github-search/`へ保存した。参照対象のコードは実行していない。アプリの依存追加・API実装・DB移行は今回の設計見直しには含めない。
