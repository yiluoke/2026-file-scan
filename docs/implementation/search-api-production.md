# 検索APIの本番環境構築手順

更新日：2026-11-03。対象はWindowsホスト上のUbuntu 26.04 VM。現在の`src/search/`を常駐させ、限定した社内クライアント・Difyから利用するための手順をまとめる。実装と公式資料を照合した構築手順であり、この文書の作成時にUbuntu上へのインストール・本番配備を実行したものではない。

## 1. 適用範囲と構成

**現在のAPIは`local_prototype`で、文書ACL・利用者別SSOを適用しない。** 同じ接続キーを持つ呼出元は、設定ナレッジ内の公開済み資料と原本を取得できる。利用者全員が対象資料を共有してよい範囲へ接続を限定する。利用者別に資料を制限する本番運用には、別途ACL実装が必要となる。TLS・接続元制限は文書ACLの代わりにはならない。

本書では次の単一VM構成を使う。

| 対象 | 配置・接続 |
| --- | --- |
| プロジェクトルート | `/opt/file-scan/`。`pyproject.toml`・設定ファイル・`src/`を直接置く |
| 取込ソース | `/opt/file-scan/src/file_scan/` |
| 検索APIソース | `/opt/file-scan/src/search/`。取込と独立した並列のパッケージ |
| アプリ設定 | `/opt/file-scan/config.local.yaml`を共有。環境変数は同じ直下の`.env`・`.env.search` |
| Python環境 | `/opt/file-scan/.venv/`。現在は取込・検索を同じ配布物から導入する |
| APIプロセス | OSユーザー`file-scan`、systemdの`file-search.service`、1プロセス |
| API待受 | `127.0.0.1:8000`。現CLIに外部bind用の`--host`はない |
| 接続入口 | NginxのHTTPS 443。許可した接続元だけ通す |
| DB | 同じVMのPostgreSQL 18、`127.0.0.1:5432`、DB名`file_scan` |
| テーブル | 物理スキーマ`winnap`。取込の管理版v3、検索の管理版v1 |
| 原本・画像 | `/var/lib/file-scan/storage`。取込が書き、APIは原本を読む |
| トークナイザー | `/var/cache/file-scan/tiktoken`へ事前配置 |
| 質問Embedding | OpenAIへHTTPS接続。公開データが採用したモデル・次元数を使用 |

検索APIに関係する主なソースとアプリ設定の配置は次のとおり。リポジトリの内容を`/opt/file-scan/`へ直接配置する。

```text
/opt/file-scan/
├── pyproject.toml
├── config.example.yaml
├── config.local.yaml       # 取込・検索の共通設定（Git管理外）
├── .env                    # 取込・公開管理用の環境変数（Git管理外）
├── .env.search             # 常駐API用の環境変数（Git管理外）
├── .venv/                  # Ubuntu上で作成するPython環境
├── src/
│   ├── file_scan/          # ファイル取込処理
│   └── search/             # 検索API・公開管理処理
├── requirements/
├── scripts/
└── …                       # sql/・docs/等の配布物
```

`pyproject.toml`は`src/`から両パッケージを導入し、`file-scan`と`file-search`の起動コマンドを`.venv/bin/`へ作成する。作業ディレクトリは`/opt/file-scan/`とし、導入済みのコマンドを使う。

YAML内の相対パスは、カレントディレクトリではなく**設定ファイルの親ディレクトリ**を基準に解決される。例えば`storage_dir: artifacts/storage`は`/opt/file-scan/artifacts/storage`となる。本番例では永続データを`/var/lib/file-scan/`、辞書キャッシュを`/var/cache/file-scan/`へ置き、YAMLには絶対パスを設定する。取込元の`/mnt/file-server/`も含め、これらはソースの配置先とは用途が異なる。

DB名・接続先・データ配置パスは導入先に合わせて変更する。ただし、既存DBの`storage_dir`は初回登録時の絶対パスと照合される。移設時にYAMLだけを書き換えて既存DBへ接続しない。DBと保管先の移行計画を先に確認する。

アプリ設定はプロジェクト直下へ置く。systemdユニットの`/etc/systemd/system/`、Nginx設定・証明書の`/etc/nginx/`、SMBマウント用の認証ファイルは、各OSサービスが参照する配置先を使う。

通常稼働時は、APIからOpenAIへのHTTPS通信が必要となる。パッケージ・モデル・辞書の配布元への接続は構築時に準備する。SMBのTCP 445は取込側の要件であり、APIは保管済み原本をローカルで読む。

## 2. 導入前に用意するもの

1. 配布対象のGitコミットまたは版を固定したソース一式。`src/search/`だけでなく、参照先の`src/file_scan/`、`requirements/`、`scripts/`、モデルlockも含める。
2. Ubuntu VMのIP・DNS名、許可する呼出元IP、組織で信頼されるサーバー証明書と秘密鍵。
3. 対象ナレッジ・実際の業務版名・採用原本・承認者。実資料の質問と本文をEmbedding APIへ送信できる範囲。
4. DB・原本保管先・バックアップ先の容量、停止を許容する時間、復旧目標、担当者。

導入順は「OS・Python → DB拡張 → 共通設定 → テーブル → 取込・Embedding・公開 → API常駐化 → HTTPS → 受入確認」とする。既存の取込環境がある場合は、そのDB・保管先・ナレッジを引き継ぎ、二重に構築しない。

## 3. OS・Python・DB拡張

新規VMは[Ubuntu 26.04 VMの構築手順](ubuntu-26.04-vm.md)の2〜4節で、OSユーザー`file-scan`、配置先、PostgreSQL、拡張を準備する。検索では**pgvectorとPGroongaの両方が必須**で、PGroongaの省略手順は使わない。同梱`compose.yaml`は開発用であり、PGroongaを含む本番構成にはなっていない。

| 項目 | このプロジェクトの確認基準 |
| --- | --- |
| Python | 3.14系、確認版3.14.4。アプリの許可範囲は3.14以上3.15未満 |
| Python依存 | `requirements/ubuntu-26.04.lock`と`pyproject.toml`。FastAPI等の`search` extraも導入 |
| PostgreSQL | 確認版18.6。保守更新を適用する場合は採用版を記録して受入確認 |
| pgvector拡張 | 0.8.6。Python側の`pgvector==0.5.1`とは別 |
| PGroonga拡張 | 4.1.0。Groonga等の依存版も構築記録へ残す |

2026-11-03に再確認した[PGroonga公式Ubuntu手順](https://pgroonga.github.io/install/ubuntu.html)の対応一覧は22.04／24.04で、26.04は掲載されていない。本環境では既存VM手順の[公式ソースビルド](https://pgroonga.github.io/install/source.html)を使い、別Ubuntu版向けのAPTリポジトリを流用しない。Macの`artifacts/pgroonga-runtime/`や`.venv`はUbuntuへコピーしない。

ソース一式を`/opt/file-scan/`へ配置してから、OSユーザー`file-scan`で実行する。以下は新規導入時の操作である。既存環境の更新は11節に従い、稼働中のソース・仮想環境を上書きしない。

```sh
sudo -u file-scan -H bash
cd /opt/file-scan
python3.14 --version
python3.14 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements/ubuntu-26.04.lock
python -m pip install --no-deps --no-build-isolation '.[search]'
python scripts/check_python_environment.py
python -m pip check
file-search --help
exit
```

現在のlockは検証用依存も含む。`check_python_environment.py`はPythonのパッチ版まで基準と照合するため、Ubuntuの保守更新で差が出た場合は、基準と試験結果を更新する。照合エラーを無視して完了と扱わない。

DB管理者が接続し、新規DBの場合だけロール・DBを作る。既存環境ではDB・所有ロールを再作成せず、拡張の有効化と版を確認する。

```sh
sudo -u postgres psql -v ON_ERROR_STOP=1
```

```sql
CREATE ROLE file_scan LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE;
\password file_scan
CREATE DATABASE file_scan OWNER file_scan;
\connect file_scan
CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public VERSION '0.8.6';
CREATE EXTENSION IF NOT EXISTS pgroonga WITH SCHEMA public VERSION '4.1.0';
SELECT e.extname, e.extversion, n.nspname
FROM pg_extension e JOIN pg_namespace n ON n.oid = e.extnamespace
WHERE e.extname IN ('vector', 'pgroonga');
\quit
```

両拡張を`public`に置く新規構成の例。`IF NOT EXISTS`は既存拡張の版を更新しないので、SELECT結果が採用版と一致することを確認する。拡張の所有者・導入作業はDB管理者とし、API用ロールへsuperuser権限を与えない。

PostgreSQLはVM内からだけ接続できる待受とし、TCP認証をSCRAMで構成する。`pg_lsclusters`、`SHOW config_file`、`SHOW hba_file`で実配置を確認する。以下は標準クラスタ`18/main`を前提とし、クラスタ名が違う場合は後述のsystemd依存先も変更する。

## 4. 共通設定と認証情報

### 現在の共有範囲

現実装は取込・検索で同じYAMLを読み、共通項目はDB・ナレッジ・保管先・Embedding設定となる。前に検討した`common.yaml`・`file_scan.yaml`・`search.yaml`への3分割は未実装である。検索専用のYAML項目は現時点で`search.test_mode`。ポートと履歴検索許可はCLI引数、検索件数上限・期限・予算はコードの既定値である。

本書では共通YAMLを`/opt/file-scan/config.local.yaml`、環境変数を用途別の2ファイルへ置く。ナレッジ・保管先等は共通YAMLで管理し、接続するDBロールを環境変数で分ける。現ローダーは接続URL全体を受け取るため、2つのenvファイルのDBホスト・ポート・DB名を同じ値に合わせる。

| ファイル | 利用者・内容 |
| --- | --- |
| `/opt/file-scan/config.local.yaml` | 両機能で共有する設定。取込元も含め、初期構築または既存設定を引き継ぐ |
| `/opt/file-scan/.env` | 管理用。DB所有ロール`file_scan`の接続先、OpenAIキー、辞書配置先 |
| `/opt/file-scan/.env.search` | API用。後述の`file_scan_search`の接続先、検索接続キー、OpenAIキー、辞書配置先 |

既存の`file-scan-ingest.service`を使う場合も、`WorkingDirectory=/opt/file-scan`、`EnvironmentFile=/opt/file-scan/.env`、起動コマンド`/opt/file-scan/.venv/bin/file-scan`、`--config /opt/file-scan/config.local.yaml`へ揃える。元の共有マウント検査は保持し、変更後に`systemctl daemon-reload`する。既存の`config.local.yaml`を共通設定として引き継ぎ、同じ項目を管理する別のYAMLを作らない。

管理者が設定を準備する。次の操作は存在しないファイルだけを作成し、既存の設定・認証情報は保持したまま権限を揃える。既存設定がある場合は、後述の例で全体を置き換えず必要項目だけを更新する。

```sh
sudo bash <<'SETUP'
set -eu
cd /opt/file-scan
if [ ! -e config.local.yaml ]; then
  install -o root -g file-scan -m 0640 config.example.yaml config.local.yaml
fi
for env_file in .env .env.search; do
  if [ ! -e "$env_file" ]; then
    install -o root -g root -m 0600 /dev/null "$env_file"
  fi
done
chown root:file-scan config.local.yaml
chmod 0640 config.local.yaml
chown root:root .env .env.search
chmod 0600 .env .env.search
SETUP
sudoedit /opt/file-scan/config.local.yaml /opt/file-scan/.env /opt/file-scan/.env.search
```

`config.local.yaml`・`.env`・`.env.search`は現行の`.gitignore`で除外される。`.env`のDB接続先は取込・公開管理用のまま維持し、API専用ロールは`.env.search`だけへ設定する。

YAMLはコピーした全体設定を維持し、次の項目を実環境へ合わせる。これは差分の例であり、この断片だけでファイル全体を置き換えない。

```yaml
knowledge_id: business-docs
system_id: target-system
environment: production
storage_dir: /var/lib/file-scan/storage
report_dir: /var/lib/file-scan/reports
docling_artifacts_path: /var/lib/file-scan/models/docling
sources:
  - source_id: department-share
    root: /mnt/file-server/department
    recursive: true
    exclude_directories: [旧版/]
    exclude_directory_names: [BK]  # どの階層でも同名フォルダと配下を除外
    exclude_filenames: ['~$*', '*.tmp']
embedding:
  enabled: true
  model: text-embedding-3-small
  dimensions: 1536
  api_key_env: OPENAI_API_KEY
search:
  test_mode: false
```

既存DBの場合は`knowledge_id`・採用プロファイル・保管先を既存値へ合わせる。設定例に残った`embedding.api_key`の仮キーは削除し、実キーをYAMLへ書かない。モデル・次元数を変更する場合は、既存公開プロファイルとの混在を避ける移行が必要となる。

`.env`の形式。`PASSWORD`を実値へ置換し、URL予約文字はパーセントエンコードする。

```dotenv
FILE_SCAN_DATABASE_URL=postgresql+psycopg://file_scan:PASSWORD@127.0.0.1:5432/file_scan
OPENAI_API_KEY=OPENAI_KEY
TIKTOKEN_CACHE_DIR=/var/cache/file-scan/tiktoken
```

`.env.search`の形式。`SEARCH_DB_PASSWORD`と接続キーは、それぞれ別の値を用意する。OpenAIへの接続には実際の有効なキーが必要。

```dotenv
FILE_SCAN_DATABASE_URL=postgresql+psycopg://file_scan_search:SEARCH_DB_PASSWORD@127.0.0.1:5432/file_scan
FILE_SCAN_SEARCH_API_KEY=SEARCH_CONNECTION_KEY
OPENAI_API_KEY=OPENAI_KEY
TIKTOKEN_CACHE_DIR=/var/cache/file-scan/tiktoken
```

`EnvironmentFile`には`export`・コマンド置換・他変数への参照を書かず、単純な`名前=値`を使う。Pythonはこれらのファイルを自動読込しない。systemdの起動時に渡す。現APIはキーを環境変数から受け取る実装であり、組織で秘密管理基盤を使う場合は、その基盤からプロセスへ渡す仕組みも構成する。[systemdの実行環境仕様](https://www.freedesktop.org/software/systemd/man/latest/systemd.exec.html)

### 原本・モデル・辞書

`file-scan`ユーザーが保管原本を読めることを確認する。APIは取込元を走査せず、原本はCASから取得するが、現在の共通ローダーには有効な`sources`設定が必要である。SMBは[VM手順](ubuntu-26.04-vm.md)の6節に従い、未マウントの空フォルダを取り込まない。

PDF用のモデル・OCR設定は取込側の手順で準備する。検索API自体はPDF変換モデルを実行しない。トークナイザー辞書は、接続可能な準備環境で通常モードの実行ユーザー・配置先へ事前取得する。

```sh
sudo -u file-scan env TIKTOKEN_CACHE_DIR=/var/cache/file-scan/tiktoken \
  /opt/file-scan/.venv/bin/python -c "from file_scan.tokens import count_tokens; print(count_tokens('初期確認'))"
```

OpenAIへの接続経路・組織のCA設定も確認する。辞書不足による起動後の初回ダウンロードに依存しない。

## 5. テーブル初期化と実資料の公開

以下は管理者の一時的なrootシェルで環境変数を読み、実際のアプリ処理を`file-scan`ユーザーで実行する例。root所有の秘密ファイルを一般ユーザー向けに読み取り可能へ変更する必要はない。操作後は`exit`する。

```sh
sudo bash
set -a
source /opt/file-scan/.env
set +a
cd /opt/file-scan
runuser -u file-scan -- .venv/bin/file-scan init-db --config /opt/file-scan/config.local.yaml
runuser -u file-scan -- .venv/bin/file-search init-db --config /opt/file-scan/config.local.yaml
exit
```

期待値は`winnap.schema_migrations`が3、`winnap.search_schema_migrations`が1。取込20表と検索8表を同じ`winnap`に管理する。既存の`public`配置が残る環境では、新規初期化を強行せず[スキーマ移行手順](winnap-schema.md)を実施する。初期化はAPI用のDBロールでは行わない。

DB管理者が`file_scan`へ接続し、次を確認する。全文索引は`indisvalid = true`の1行が必要で、0行なら未作成である。

```sql
SELECT version FROM winnap.schema_migrations;
SELECT version FROM winnap.search_schema_migrations;
SELECT indexrelid::regclass AS index_name, indisvalid
FROM pg_index
WHERE indexrelid = to_regclass('winnap.ix_search_chunks_fulltext');
```

資料を取り込んだだけでは検索公開されない。[公開管理の詳細](search-api.md#版の作成と公開)に従い、次の順で準備する。

```sh
sudo bash
set -a
source /opt/file-scan/.env
set +a
cd /opt/file-scan
runuser -u file-scan -- .venv/bin/file-scan ingest --config /opt/file-scan/config.local.yaml
runuser -u file-scan -- .venv/bin/file-scan embed --config /opt/file-scan/config.local.yaml
runuser -u file-scan -- .venv/bin/file-scan status --config /opt/file-scan/config.local.yaml
runuser -u file-scan -- .venv/bin/file-search create-release --config /opt/file-scan/config.local.yaml --release '<実際の業務版名>'
runuser -u file-scan -- .venv/bin/file-search publish --config /opt/file-scan/config.local.yaml \
  --release '<実際の業務版名>' \
  --document-id 'sha256:<承認する原本のdocument_id>' \
  --generation-id 'sha256:<承認するgeneration_id>' \
  --reviewed-by '<実際の承認者ID>'
runuser -u file-scan -- .venv/bin/file-search status --config /opt/file-scan/config.local.yaml
exit
```

山括弧の項目を実値へ置き換え、採用する各文書を明示公開する。初回作成した版は既定となる。以後、既定版を切り替えるときは`create-release`に`--make-default`を付ける。

- ベクトルは公開時に凍結される。`embed`は`publish`より先に行い、後から生成した場合は同世代を再公開する。
- `partial`は不足を確認してから`--allow-partial`で個別承認する。通常の`/retrieval`はcoverage不足を409として返す。
- `ingest`・`embed`の定期実行と業務版の公開承認を別の運用として管理する。原本更新後は、旧公開コピーが現行検索から外れるため、再公開までcoverageが不足する。
- 別の業務版への切り替えだけで、更新前の原本を現行扱いに戻せるとは想定しない。履歴検索許可は後述の起動設定とは別に判断する。

## 6. API専用DBロール

DB管理者が、5節の初期化後に実行する。取込・公開管理は所有ロール`file_scan`、常駐APIは`file_scan_search`へ分ける。以下は現在の参照表と、監視用INSERTだけを許可する例。[PostgreSQLのGRANT仕様](https://www.postgresql.org/docs/18/sql-grant.html)

```sh
sudo -u postgres psql -d file_scan -v ON_ERROR_STOP=1
```

```sql
CREATE ROLE file_scan_search LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION;
\password file_scan_search
GRANT CONNECT ON DATABASE file_scan TO file_scan_search;
GRANT USAGE ON SCHEMA winnap, public TO file_scan_search;
GRANT SELECT ON
  winnap.schema_migrations, winnap.ingestion_settings,
  winnap.knowledge_bases, winnap.knowledge_documents, winnap.documents,
  winnap.selected_generations, winnap.extraction_generations,
  winnap.embedding_profiles, winnap.artifact_assets,
  winnap.search_schema_migrations, winnap.search_catalogs, winnap.search_releases,
  winnap.search_release_bindings, winnap.search_publications,
  winnap.search_chunks, winnap.search_blocks
TO file_scan_search;
GRANT INSERT ON winnap.search_events TO file_scan_search;
\quit
```

このAPIロールに`CREATE`・業務表の`UPDATE/DELETE`・管理ロールの所属は付けない。拡張関数の既定の実行権限を変更しているDBでは、DB管理者が必要なPGroonga関数等の権限も確認する。新規構成は`public.pgroonga_*`を使い、旧`pgroonga`スキーマの互換関数には依存しない。

`.env.search`のDBパスワードをこのロールの値へ合わせ、`pg_hba.conf`で127.0.0.1からの接続を許可する。今後、参照表を増やす移行を行った場合は、この権限一覧も見直す。

## 7. systemdで常駐化

`/etc/systemd/system/file-search.service`を作成する。`18/main`以外のDBクラスタでは依存先を置き換える。サービスは現在のCLIと同じく1プロセス・loopback待受で起動する。

```ini
[Unit]
Description=File Search API
Wants=network-online.target
After=network-online.target postgresql@18-main.service
Requires=postgresql@18-main.service

[Service]
Type=exec
User=file-scan
Group=file-scan
WorkingDirectory=/opt/file-scan
EnvironmentFile=/opt/file-scan/.env.search
Environment=PYTHONDONTWRITEBYTECODE=1
UMask=0077
ExecStartPre=/opt/file-scan/.venv/bin/python -c "from search.config import load_search_settings; c=load_search_settings('/opt/file-scan/config.local.yaml'); assert not c.test_mode, 'production requires search.test_mode=false'"
ExecStart=/opt/file-scan/.venv/bin/file-search serve --config /opt/file-scan/config.local.yaml --port 8000
Restart=on-failure
RestartSec=3
TimeoutStopSec=30
KillMode=control-group
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadOnlyPaths=/var/lib/file-scan/storage /var/cache/file-scan/tiktoken
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

本番サービスでは`test_mode: true`を起動前に拒否する。DBを検査しないテスト応答のhealthが200となることを、本番稼働と誤認しないためである。単独の接続試験は、[テスト用設定](../../config.search-test.example.yaml)を別ポートで手動起動し、終了後に停止する。

ファイル書き込みはDBの監視表だけとし、APIサービス内ではアプリ・保管先・辞書を読み取り専用にする。取込バッチはこのサービス内で起動しない。[systemdのサービス仕様](https://www.freedesktop.org/software/systemd/man/latest/systemd.service.html)

```sh
sudo systemd-analyze verify /etc/systemd/system/file-search.service
sudo systemctl daemon-reload
sudo systemctl enable --now file-search.service
sudo systemctl status file-search.service
sudo journalctl -u file-search.service -n 100 --no-pager
ss -ltn | grep '127.0.0.1:8000'
curl --fail --silent --show-error http://127.0.0.1:8000/healthz
```

healthの`execution_mode`が`local_prototype`で、vectorとPGroongaの版が返ることを確認する。healthだけでは公開資料・実Embeddingの準備完了を確認できない。`--allow-history`は必要な場合だけ`ExecStart`へ追加し、閲覧可能範囲と古い原本の取扱いを確認する。

## 8. NginxでHTTPS接続を受ける

DNS名`search.internal.example`、許可IP`10.20.30.40`、証明書パスを実際の値へ置き換える。IPは例示値であり、Difyがコンテナにある場合はVMから見える送信元IPを確認する。外部から8000・5432へ直接接続させず、ファイアウォール・Windowsホスト・VMネットワークでも許可元からの443だけを通す。

```sh
sudo apt install -y nginx
sudo install -d -o root -g root -m 0700 /etc/nginx/tls
```

組織の証明書チェーンを`/etc/nginx/tls/search.crt`、秘密鍵を`/etc/nginx/tls/search.key`へ配置し、秘密鍵はrootだけが読める権限にする。呼出側にも対応するCAを信頼させる。[NginxのHTTPS設定](https://nginx.org/en/docs/http/configuring_https_servers.html)

`/etc/nginx/conf.d/file-search.conf`の例。Ubuntu標準構成で`http`内から読み込まれるファイルへ置く。

```nginx
log_format file_search '$remote_addr $time_iso8601 "$request_method $uri" '
                       '$status $request_time $upstream_response_time';

server {
    listen 443 ssl;
    server_name search.internal.example;
    ssl_certificate /etc/nginx/tls/search.crt;
    ssl_certificate_key /etc/nginx/tls/search.key;
    ssl_protocols TLSv1.2 TLSv1.3;

    allow 127.0.0.1;
    allow 10.20.30.40;
    deny all;
    client_max_body_size 32k;
    access_log /var/log/nginx/file-search-access.log file_search;
    error_log /var/log/nginx/file-search-error.log warn;

    proxy_http_version 1.1;
    proxy_set_header Connection "";
    proxy_set_header Host $host;
    proxy_set_header Authorization $http_authorization;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_connect_timeout 2s;
    proxy_send_timeout 15s;
    proxy_read_timeout 15s;
    proxy_next_upstream off;
    proxy_cache off;
    proxy_buffering off;

    location = /search-with-status {
        proxy_pass http://127.0.0.1:8000;
    }
    location = /retrieval {
        proxy_pass http://127.0.0.1:8000;
    }
    location ^~ /sources/ {
        proxy_read_timeout 60s;
        proxy_pass http://127.0.0.1:8000;
    }
    location / {
        return 404;
    }
}
```

APIパスを変更せず転送し、`/docs`・`/openapi.json`・`/healthz`は外部へ公開しない。原本は必ず認証付き`/sources/`から返し、storageをNginxの`alias`等で公開しない。アクセスログには質問本文・Authorization・クエリ文字列を含めず、ログの保存期間も設定する。質問はJSON、キーはBearerヘッダーで送り、URLへ付けない。

APIの検索期限は全体10秒なので、プロキシはそれより長く待つ。プロキシでPOSTを自動再試行したり、古い検索結果をキャッシュして返したりしない。[Nginxのプロキシ仕様](https://nginx.org/en/docs/http/ngx_http_proxy_module.html)

```sh
sudo nginx -t
sudo systemctl enable --now nginx
sudo systemctl reload nginx
```

## 9. 受入確認

本番DBでpytestを実行しない。回帰試験は同じ版の拡張を入れた検証専用DBで行い、Ubuntu実機の結果・Gitコミット・依存版・OS/CPU・拡張版を記録する。

| 確認 | 合格条件 |
| --- | --- |
| 実モード | YAMLが`search.test_mode: false`。healthが`execution_mode: local_prototype` |
| DB・索引 | 取込v3・検索v1、拡張版を確認。公開チャンクのベクトル不足がなく、PGroonga索引が有効 |
| 実検索 | 許可された実質問で200。固定のテスト文言ではなく、承認した資料の根拠・出典を返す |
| coverage | 想定した対象範囲で`complete`。不足時は、理由を把握し状態付きAPIで扱う |
| 互換API | coverage完了時の`/retrieval`が200。不足時の409を呼出側がエラーとして扱う |
| 認証・ネットワーク | 接続キーなし／誤りは401、許可しない送信元はNginxで403。CA検証を無効にしない |
| 原本 | 返却された`source_ref`を、同じ接続キーで取得できる。原本版・SHA-256・文字位置を照合 |
| 更新・削除 | 原本状態を取込が更新した後、古い現行根拠が返らない。再公開とcoverage回復を確認 |
| 起動・停止 | VM再起動後にPostgreSQL・API・Nginxが起動。停止時に処理と接続が終了する |
| 障害・復旧 | DB停止・Embedding失敗・タイムアウトを正常0件と扱わない。バックアップ復元を別環境で確認 |

許可された呼出元でのHTTP確認例。`business-docs`と質問を実値へ置き換え、接続キーは呼出側の秘密管理から環境変数へ読み込む。以下のファイルには根拠本文が入るため、運用で認めた作業先を使い、確認後に片付ける。

```sh
umask 077
cat > /tmp/file-search-request.json <<'JSON'
{"knowledge_id":"business-docs","query":"送信が許可された実資料の確認質問","retrieval_setting":{"top_k":5,"score_threshold":0.0}}
JSON
printf 'Authorization: Bearer %s\n' "$FILE_SCAN_SEARCH_API_KEY" | \
  curl --fail-with-body --silent --show-error \
    https://search.internal.example/search-with-status \
    --header @- --header 'Content-Type: application/json' \
    --data-binary @/tmp/file-search-request.json \
    --output /tmp/file-search-response.json
```

接続試験のテストモードはDB・Embedding・公開準備を検査しないため、この受入確認の代わりにはしない。実質問のEmbeddingでは外部APIへ送信し利用料金が発生する。

## 10. Difyから利用する場合

Dify本体の配備とChatflow作成は別作業であり、検索APIを先行して稼働できる。DifyからVMのDNS名・443へ接続でき、証明書を検証できることを確認する。Difyコンテナの`127.0.0.1`はAPI VMを指さない。

- 主経路はHTTP Requestノードから`https://search.internal.example/search-with-status`へPOSTする。BearerキーはDify側の秘密設定で管理し、JSON契約は[API操作手順](search-api.md)に合わせる。
- 非200では回答生成へ進めず、`coverage_state`・`result_state`・空レコードを分岐処理する。テスト応答を実根拠として扱わない。
- 標準のExternal Knowledge連携を使う場合はベースURLを登録し、Difyが`/retrieval`を呼ぶ。主経路との二重検索を避ける。
- `source_ref`はAPIルート基準の相対パス。原本取得にもBearerが必要で、ブラウザーからの単純なリンククリックだけでは認証情報を渡せない。キー入りURLを作らず、認証付き取得を行う画面・バックエンドを用意する。

## 11. 監視・更新・鍵の変更

| 対象 | 確認するもの |
| --- | --- |
| プロセス | `systemctl status file-search.service`、journal、再起動回数 |
| API | 内部healthのモード・拡張、HTTPエラー率、応答時間、429・503・504 |
| 公開状態 | coverage、更新待ち、失敗、partial、ベクトル不足。healthが200でも別途確認 |
| 監視記録 | `winnap.search_events`。書き込み失敗の`search_event_write_failed`も監視 |
| DB・ディスク | 接続数、容量、PGroongaのログ・索引、公開コピー・原本・監視表の増加 |
| TLS・外部API | 証明書期限、CA、OpenAIキー・課金・通信可否 |

API1プロセスの同時検索は4件、DBプール4接続、待ち1秒、質問Embedding4秒、各SQL2秒、全体10秒。これらは現在コードで固定される。複数プロセスにすると枠・接続数もプロセス数分増えるため、単純にworkerを増やす前にDB・外部API・負荷を評価する。

401は接続キー、404は範囲・公開原本、409は互換APIの不足、422は入力や未整備フィルター、429は同時実行、503/504はDB・Embedding・期限等を確認する。全文検索だけへ自動縮退する構成ではない。

更新時は別の作業場所で新版を事前検証する。本番へ反映するときはAPIと取込・Embedding・公開処理、その起動元を停止し、`/opt/file-scan/`直下の`config.local.yaml`・`.env`・`.env.search`を保持して、配布ソースと依存を更新する。仮想環境は最終配置先の`/opt/file-scan/.venv/`で作成・導入する。別パスで作成した仮想環境を移動して使わない。`WorkingDirectory`・`EnvironmentFile`・`ExecStart`の参照先は本書の配置へ揃え、確認後に再開する。

DB移行が必要なら事前に整合バックアップを取る。アプリだけを旧版へ戻してもDB移行が戻るとは扱わない。旧版が現在のDBと互換でない場合は、DBとstorageを同じバックアップ時点へ復元する。

接続キーの変更は`.env.search`と呼出側を更新し、`sudo systemctl restart file-search.service`を実行する。ホットリロード・複数キーの同時有効化は未実装なので、切替時間を調整する。OpenAIキーや共通YAMLの変更もプロセス再起動が必要。systemdユニットを変更した場合は先に`daemon-reload`する。

## 12. バックアップ・復旧

基本例は、全ての取込・Embedding・公開処理・APIを停止し、同じ時点のDB・storage・設定を揃える停止バックアップとする。外部スケジューラーや手動バッチも停止を確認する。これは単一VM向けの保守例であり、無停止運用・PITR・HAを保証する構成ではない。運用の復旧目標がこの方式で満たせない場合は、[PostgreSQLのバックアップ方式](https://www.postgresql.org/docs/18/backup.html)とPGroongaの構成を含めて別途設計する。

```sh
sudo systemctl stop file-search.service
# 別途、取込・Embedding・公開処理とその起動元も停止した状態にする
BACKUP_DIR="/var/backups/file-scan/$(date -u +%Y%m%dT%H%M%SZ)"
sudo install -d -o postgres -g postgres -m 0700 "$BACKUP_DIR"
sudo -u postgres pg_dump --format=custom --file="$BACKUP_DIR/database.dump" file_scan
sudo tar --acls --xattrs -cpf "$BACKUP_DIR/files.tar" -C /var/lib/file-scan storage reports
sudo tar -cpf "$BACKUP_DIR/config.tar" -C /opt/file-scan config.local.yaml .env .env.search
sudo chmod 0600 "$BACKUP_DIR/database.dump" "$BACKUP_DIR/files.tar" "$BACKUP_DIR/config.tar"
```

`config.tar`はプロジェクト直下のアプリ設定3ファイルを保存する。ソース一式や`.venv`はこの設定アーカイブへ含めない。復元時は同じ`/opt/file-scan/`直下へ戻し、4節の所有者・権限を再確認する。systemdユニット、Nginx設定・証明書、SMBマウント設定は別途保護して保管する。

`database.dump`は検索表・公開コピー・Embeddingを含むDB全体を対象とする。`pg_dump`はロールを保存しないため、所有・実行ロールの再作成手順も保護して保管する。ソースの版・lock・DB拡張とGroongaの版、モデルmanifestも一緒に記録する。暗号化・別障害領域への複製・保存期間は運用基準へ合わせる。[pg_dumpの仕様](https://www.postgresql.org/docs/18/app-pgdump.html)

復旧は別環境の空DBから試す。採用版のサーバー拡張とロールを先に用意し、全DBの論理dumpを`pg_restore --exit-on-error`で復元する。空DBへ`init-db`で先にアプリ表を作らない。復元先ではPGroonga索引の再構築時間も測り、storageをDBに保存された同じ絶対パスへ復元し、所有者・読込権限・原本ハッシュを確認する。その後、版・実検索・coverage・原本取得を確認して再開する。[pg_restoreの仕様](https://www.postgresql.org/docs/18/app-pgrestore.html)

PGroongaのクラッシュ対策・WAL連携は`CREATE EXTENSION`だけでは構成されない。本手順は自動復旧やストリーミング複製を設定していない。異常停止後はAPI・書込処理を止め、DB管理者がPostgreSQLとPGroongaの状態を確認する。索引の再構築が必要なら、正常な原データを確認したうえで次を実行し、受入確認後に再開する。

```sql
REINDEX INDEX winnap.ix_search_chunks_fulltext;
ANALYZE winnap.search_chunks;
```

GroongaのDB自体が壊れてREINDEXもできない場合はファイルを手動削除せず、論理バックアップからの復元等で復旧する。自動クラッシュ復旧・複製を採用する場合は、公式の[クラッシュ対策](https://pgroonga.github.io/reference/crash-safe.html)と[レプリケーション](https://pgroonga.github.io/reference/replication.html)を確認し、採用方式で障害・復元試験を行う。

## 13. この文書で確認した範囲

現行の`pyproject.toml`、取込・検索の設定ローダーとCLI、非同期DB接続、検索・出典・テストモード、`winnap`配置、固定依存、既存のVM手順と照合した。プロジェクトルートを`/opt/file-scan/`として、設定・`src/`・仮想環境・systemd・バックアップのパスを統一した。公式資料は2026-11-03に確認した。文書内のBash構文とローカル参照リンクも確認した。

この作業では本番VM・DB・Nginx・systemdの設定変更、実資料の公開、実OpenAI呼び出しを行っていない。Ubuntu上の`systemd-analyze verify`・`nginx -t`・実検索・権限・バックアップ復元を実施し、担当者が受入結果を記録してから利用を開始する。
