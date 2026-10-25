# 検索APIの操作・実装

実装日：2026-10-25、検証更新：2026-10-26。文書ACL・SSOを保留した`local_prototype`。Dify本体の配備・Chatflowは別途行う。

## 実装した範囲

- `POST /search-with-status`：根拠、出典、対象版、取り込み・公開・索引の不足を返す。
- `POST /retrieval`：Dify External Knowledge API互換。不足を表せないため、coverage未完了は409。
- `GET /sources/{publication_id}`：接続キーと公開対象を照合し、保管原本のSHA-256を検証して返す。
- `GET /healthz`：検索スキーマ・DB接続・vector／PGroongaを確認。公開済み資料や実Embeddingの準備完了を意味しない。
- `/docs`、`/openapi.json`：リクエスト、レスポンス、Bearer接続認証を記載したAPI定義。
- `file-search`：検索専用DB初期化、業務版作成、世代の明示公開、状態確認、API起動。

意味検索はpgvectorのcosine正確検索、全文検索はPGroonga、識別子は大文字小文字・記号を保持した配列一致。各50候補をRRFで統合する。全文経路はGroongaの`TokenBigram`＋`NormalizerAuto`で質問を分割し、先頭から重複を除いた最大16語（各128文字以内・2文字以上）をリテラルの`&@`でOR検索する。形態素解析・質問展開・rerankerは使用していない。検索語選択と実データでの精度は今後評価する。

## 取り込み処理への影響を抑える構成

取り込みの`src/file_scan/db.py`、`pipeline.py`、`cli.py`の公開・処理方式を変更せず、検索処理を`src/search/`へ分離した。`src/file_scan/`は取り込み、`src/search/`は検索の並列パッケージとし、`file-search`の起動先は`search.cli:main`とする。検索側から既存の取り込みDB定義・設定・原本参照などを利用するが、取り込み側は検索パッケージに依存しない。検索は取り込み用metadataへテーブルを登録しない。取り込みスキーマはv3、検索専用スキーマはv1として別々に管理する。

**取り込みデータはdraftのまま保持し、管理者が承認した内容だけを検索専用表へ凍結する。** 既存のdraft制約やナレッジ無効制約を外して検索へ流す方式は採用しない。取り込みCLI・Embeddingの実行から公開は行わない。

公開コピーには原文、原本文字範囲、ブロック・見出し、画像出現参照、確認済み関係、入力ハッシュが一致する完成ベクトルを保持する。元の世代が後で部分再抽出されても公開コピーの本文は変わらない。元世代の更新時刻も記録し、現行検索では原本版・選択世代・変換設定・更新時刻の不一致を更新待ちとして外す。

公開コピーの作成・採用ポインター切替は1トランザクションで行い、取り込みバッチとの既存ロックで競合を防ぐ。検索中はそのロックを取らず、REPEATABLE READの読み取りと返却直前の最新状態確認を行う。

意味検索用ベクトルも凍結するため、**公開後に`embed`した場合は、同じ世代を改めて`publish`して反映する**。同じ世代・本文・ベクトル・更新時刻の再公開は同じpublication IDを再利用する。公開コピー分のDB容量は追加で必要となる。

## 準備

現在の`pyproject.toml`に対応するPython環境を用意し、プロジェクトのルートで実行する。Python環境全体の構築は[環境構築手順](environment-setup.md)を参照。

```sh
source .venv/bin/activate
python -m pip install -r requirements/ubuntu-26.04.lock
python -m pip install --no-deps --no-build-isolation -e '.[dev,search]'
set -a
source .env
set +a
file-scan init-db --config config.local.yaml
file-search init-db --config config.local.yaml
```

`search`追加依存はFastAPI・Starlette・Uvicorn・HTTPX・greenletを固定版で導入する。取り込みだけの利用では不要。`file-search init-db`にはPostgreSQLと[PGroongaの導入](pgroonga.md)が必要で、拡張のインストールや取り込みv3の初期化をAPI起動時に暗黙実行しない。

既存の開発DBへは検索表だけを追加済み。資料の公開・業務版作成は行っていない。

## 版の作成と公開

原本の取り込みと、許可された環境でのEmbeddingを先に行う。

```sh
file-scan ingest --config config.local.yaml
file-scan embed --config config.local.yaml
file-scan status --config config.local.yaml
file-search create-release --config config.local.yaml --release poc-release-1
```

リリース名は管理者が決めた業務版名に置き換える。初回作成した版は既定となり、以後の版は`--make-default`を指定した場合に切り替える。作成時には現在のincluded原本を予定対象として登録する。名前を付けただけで業務版との対応が保証されるわけではなく、管理者が原本・世代を確認する。

確認用の`artifacts/reports/generations/<世代>/manifest.json`等で`document_id`と`generation_id`を確認し、原文・出典・不足を確認した世代だけを指定する。これらの確認ファイルを編集してもDBへ逆輸入しない。

```sh
file-search publish --config config.local.yaml \
  --release poc-release-1 \
  --document-id 'sha256:<原本の論理ID>' \
  --generation-id 'sha256:<確認した変換世代ID>' \
  --reviewed-by '承認者'
file-search status --config config.local.yaml
```

部分抽出の公開には`--allow-partial`を明示する。公開後もcoverageは`incomplete`となり、状態付きAPIだけが不足を伝えつつ根拠を返す。`--feature-id <機能ID>`を必要に応じて複数付けられる。機能ラベルを一度も付けていないナレッジへの機能フィルターは422。付与済みでも、ラベルのない資料はそのフィルターに一致しない。

公開前に、現在の原本・選択世代との一致、部分抽出の承認、原本実体のハッシュ、チャンクと元ブロックの文字範囲、実入力ハッシュとベクトルを検査する。source以外の派生成果物の公開、別文書への参照展開、GitコミットURLの生成は初版対象外とする。

検索プロファイルは作成時の保存済みEmbedding設定を使う。異なるモデルを既存ナレッジへ混ぜる変更は拒否する。モデル変更の移行は別途行う。

## APIの起動と確認

接続用キーは取り込み用のOpenAI APIキーと別に設定する。例の値は自分の接続用キーへ置き換え、Gitや通常ログへ保存しない。

```sh
export FILE_SCAN_SEARCH_API_KEY='<検索APIの接続用キー>'
file-search serve --config config.local.yaml --port 8000
```

初版CLIは`127.0.0.1`へだけbindする。別ターミナルから確認する。

```sh
curl --fail http://127.0.0.1:8000/healthz
curl --fail http://127.0.0.1:8000/openapi.json
curl --fail-with-body http://127.0.0.1:8000/search-with-status \
  -H "Authorization: Bearer $FILE_SCAN_SEARCH_API_KEY" \
  -H 'Content-Type: application/json' \
  --data '{"knowledge_id":"demo","query":"日次締め E1203","retrieval_setting":{"top_k":5,"score_threshold":0.0}}'
```

`knowledge_id`は設定の値に合わせる。接続キー未設定では起動しない。文書ACLは適用せず、状態には`execution_mode=local_prototype`、`access_control=not_enforced`、`policy_revision=null`を返す。

- 未公開・更新待ち・失敗した予定原本もcoverageへ含める。追加フィルターでその予定対象を消さない。
- 原本の更新・削除・利用不能は、取り込みが検出してDBへ反映した状態を使う。API要求ごとに共有フォルダ全体を再走査する処理は行わない。
- 現行検索では古い公開コピーへ戻らない。履歴は起動時に`--allow-history`を付け、`target_release is <release IDまたは名前>`を明示した場合だけ使える。削除・利用不能・対象外の原本は履歴にも返さない。
- 質問Embeddingには、採用プロファイルのモデル・次元数と、設定済みOpenAI接続情報を使用する。`embedding.enabled=false`・仮キーで、公開候補の意味検索を要求すると503。実APIへ暗黙送信したり、全文検索だけへ縮退したりしない。公開候補がゼロならEmbeddingは呼ばない。
- 全体10秒、Embedding4秒、各SQL2秒、DB接続待ち1秒、同時検索4件（1プロセス）。同時検索枠を1秒以内に取れなければ429と`Retry-After: 1`。外部APIのSDK再試行は0。
- 切断・期限超過時は検索とSQL／外部要求をキャンセルし、接続を解放する。
- 返却は最大20レコード、各2000トークン・合計6000トークン、JSON全体256 KiB。文脈も件数へ含める。引用を切り出した場合は原文字範囲を保持し、正確な位置へ狭められないPDFのbbox・Excelのセル範囲はブロック粒度と明示する。
- 表を予算内に追跡可能な形で返せない場合は、そのレコードを落として次候補を試す。本文を生成要約へ置換しない。

## Difyへの接続

Difyの配備は別作業で、検索API単独の実装・検証は完了している。

1. HTTP Requestノードから`POST /search-with-status`へ上記のJSONとBearerキーを送る。Difyから到達できる接続経路を構成する。現CLIのloopbackをコンテナ・別VMから直接使えるとは想定しない。
2. 非200は回答生成へ進めず、エラーを扱う。200で`result_state=budget_exhausted`なら返却予算不足として扱う。
3. `coverage_state=incomplete / unknown`なら不足を利用者へ伝え、`records`の範囲で回答する。空なら確認できる根拠なしとして扱う。
4. 根拠本文と出典をLLMへ渡す。`score`はRRF由来で、正答確率ではない。画像参照IDだけで画像をLLMが読んだと扱わない。

Dify標準の外部ナレッジ連携ではベースURLを登録し、Difyが`/retrieval`を呼ぶ。`metadata_condition: null`を受け付ける。主経路と標準連携から同一質問を二重検索しない。原本の`source_ref`取得にも接続キーが必要で、長期公開URL・キー入りURLは生成しない。

## 検証と制約

実サーバーはPython 3.14.4で起動し、health・OpenAPI・接続キーなしの401・未設定ナレッジの404を確認して停止した。実資料の公開や実Embedding送信は行っていない。

Python 3.14.4・PostgreSQL 18.6・pgvector 0.8.6・PGroonga 4.1.0で全179テストが成功した（既存の非推奨警告5件）。並行作業とのDBロック競合を避けるため、検証専用の新規DBで実行し、検証後にそのDBだけを削除した。途中の実行ではPDF表認識の2テストが失敗し、単独実行・検索テストを除く160テスト・最終の全179テストは成功した。表認識の変動原因は未確定で、今回、取込・PDF変換のコードに修正は加えていない。

最終結果は`artifacts/reports/search-regression-python314.log`、実サーバーの確認結果は`artifacts/reports/search-api-smoke.json`へ保存した。

検索を`src/search/`へ移した後も全179テストと実サーバー起動確認が成功した。配布wheelに`file_scan/`と`search/`が並列で含まれ、旧配置が残らず、`file-search`の起動先が`search.cli:main`であることも確認した。旧配置でのビルド履歴がある環境では、`build/lib/file_scan/search/`の生成済みキャッシュを除去して再ビルドする。

実DB検証はテスト専用DB内の一時schemaに閉じ込め、模擬ベクトルを開発DBへ入れない。実OpenAI APIへの接続・課金、Dify本体でのChatflow実行、日本語検索品質・性能は今回の実装試験には含めない。

```sh
python scripts/test_postgres.py
python -m ruff check src tests scripts
```

テストは公開前の除外、公開後も取り込みがdraftを保つこと、再取り込み・再Embedding、更新後の旧版除外と明示履歴、削除、未索引のcoverage、部分承認、出典・文脈・予算、入力・接続認証・範囲、Embedding失敗・SDK設定、SQL期限超過・プール復旧、切断、原本破損を確認する。
