# PGroongaの導入・日本語検索・運用手順

更新日：2026-11-15。初回のMac導入日は2026-10-25。検索APIの全文経路・3経路の順位統合・公開管理を実装している。本書は現行コード・固定したビルドソース・公式資料と照合した手順である。

PGroongaを使う準備は、**サーバーのライブラリ導入 → 対象DBで拡張を有効化 → 検索表・索引の作成 → 採用資料の公開**の順で行う。`pip install`や`CREATE EXTENSION`だけで全段階が完了するわけではない。

| 利用環境・目的 | 参照先 |
| --- | --- |
| 本番のUbuntu 26.04 VMへ新規導入 | 本書3〜7節。[APIの本番構築](search-api-production.md)と併用 |
| 既存のMac開発環境を再現 | 本書8節のPostgres.app用ビルド・検索パス設定 |
| 日本語検索と索引の基本動作を確認 | 本書6節の一時表SQLと9節のテスト |

## 1. 確認基準とMacの初回導入記録

| 項目 | 導入した構成 |
| --- | --- |
| PostgreSQL | 既存Postgres.appの18.6、arm64。サーバー置換・再初期化なし |
| pgvector | 既存の0.8.6を維持 |
| PGroonga | 4.1.0 |
| Groonga | Macの確認版16.1.4。MessagePack 6.1.0を同梱ソースから使用。Ubuntuでは3節の実導入版を記録 |
| xxHash | PGroongaのMeson wrap定義による0.8.3 |
| 有効化したDB | `file_scan`、`file_scan_test` |
| 初期の全文索引設定 | `TokenBigram`＋`NormalizerAuto`。識別子の要素一致は別経路で大文字小文字・記号を保持 |

2026-11-03に確認した[PGroonga公式リリース一覧](https://github.com/pgroonga/pgroonga/releases)でも4.1.0が最新として掲載されている。Groonga 16.1.4とビルドツールの固定値は、このプロジェクトの再現用の確認版であり、常に最新であるという意味ではない。今回の資料更新では、インストール済みライブラリの置換・業務DBの更新は行わない。

初回導入時はHomebrew・Dockerを使わず、公式リリースソースから既存PostgreSQLのヘッダーでビルドした。この環境ではPostgres.app内への追加がmacOSに拒否されたため、**PostgreSQL 18の標準の拡張検索パスを使い、プロジェクト専用領域へ導入**した。このMac固有の方法をUbuntuの標準配置へ適用する必要はない。[PGroonga公式ソース導入](https://pgroonga.github.io/install/source.html)、[PostgreSQL 18の拡張検索パス](https://www.postgresql.org/docs/18/runtime-config-client.html#GUC-EXTENSION-CONTROL-PATH)

- 実行時に必要なファイル：`artifacts/pgroonga-runtime/`。共有ライブラリ、拡張control／SQL、Groongaの実行ファイル等を保持する。
- 再ビルド用：`artifacts/pgroonga-build/`。取得元・SHA-256は`sources.json`、設定とビルド結果は`*-configure.log`、`*-compile.log`、`*-install.log`に保存する。
- 実行時ライブラリとDB設定はこのチェックアウトの絶対パスを使う。**runtimeはビルド用キャッシュとして削除しない**。チェックアウトを移動する場合は再ビルドとDB設定の変更が必要。
- Postgres.appのアップデート後は新しいPostgreSQLのヘッダーでの再ビルド要否と拡張の読み込みを確認する。
- Docker Composeの既存イメージはpgvectorのみ。このネイティブ版の導入をCompose対応済みとは扱わない。

## 2. 現在の日本語検索の仕様

| 項目 | 現行の動作 |
| --- | --- |
| 全文検索対象 | `winnap.search_chunks.search_text`。承認した公開コピーが対象で、取込下書きを直接検索しない |
| 全文索引 | `winnap.ix_search_chunks_fulltext`。`USING pgroonga`、`tokenizer='TokenBigram'`、`normalizers='NormalizerAuto'` |
| 質問の分割 | `pgroonga_tokenize`へ`tokenizer`と`normalizer`を指定。索引オプションの`normalizers`とは引数名が異なる |
| 語の選択 | 順序を保って重複を除き、2〜128文字の語を最大16個使う。1文字語だけの質問では全文経路の候補が空になる場合がある |
| 検索方法 | 各語をバインド値で渡し、リテラルの`&@`をORで組み合わせる。質問全体を検索式の`&@~`へ渡さない |
| 候補・順位 | 全文経路は最大50件。`pgroonga_score(tableoid, ctid)`で順位を付け、意味・識別子の経路とRRFで統合 |
| 正規化と識別子 | 全文経路では全角英数字等を正規化する。識別子経路は別の配列一致で、大文字小文字・記号を保持 |
| 対象外 | MeCab辞書、質問展開、reranker、PGroongaの言語モデル機能は導入していない |

`TokenBigram`は日本語等をbigramで扱い、英数字は文字種に応じて連続した単位として扱うため、すべての文字列を一律に2文字ずつへ分割するわけではない。`NormalizerAuto`はUTF-8でNFKCに基づく正規化を行う。[TokenBigram](https://groonga.org/docs/reference/tokenizers/token_bigram.html)、[NormalizerAuto](https://groonga.org/docs/reference/normalizers/normalizer_auto.html)

単語検索と検索式は別の演算子である。[`&@`](https://pgroonga.github.io/reference/operators/match-v2.html)、[`&@~`](https://pgroonga.github.io/reference/operators/query-v2.html)、[`pgroonga_tokenize`](https://pgroonga.github.io/reference/functions/pgroonga-tokenize.html)を参照。実装の正本は[repository.py](../../src/search/repository.py)、索引定義は[schema.py](../../src/search/schema.py)。語の選択が業務質問に合うかは、実資料で精度評価を行う。

## 3. Ubuntu 26.04 VMへのサーバー側導入

PostgreSQL 18を導入したUbuntu VM内で、管理者が行う。OS・Python・pgvector 0.8.6は[VM構築手順](ubuntu-26.04-vm.md)に従う。

Ubuntu 26.04（`resolute`）はPGroonga 4.0.8からサポートされている。Ubuntu標準のPostgreSQL 18には公式Groonga APTの`postgresql-18-pgroonga`、PGDGのPostgreSQL 18には`postgresql-18-pgdg-pgroonga`を使う。導入元と採用版を選び、APTとソースビルドのどちらか一方を使う。[公式Ubuntu手順](https://pgroonga.github.io/install/ubuntu.html)、[4.0.8のリリースノート](https://pgroonga.github.io/news/#408-2026-08-03)

### APTで導入する場合

以下はUbuntu標準のPostgreSQL 18と公式Groonga APTを使う例。組織で利用可能なリポジトリであることを確認し、OSのコードネームが`resolute`であることを確認してから登録する。

```sh
sudo apt install -y ca-certificates curl lsb-release
(
  set -eu
  test "$(lsb_release -sc)" = resolute
  apt_setup_dir="$(mktemp -d)"
  trap 'rm -rf -- "$apt_setup_dir"' EXIT
  chmod 0755 "$apt_setup_dir"
  cd "$apt_setup_dir"
  curl -fLO https://packages.groonga.org/ubuntu/groonga-apt-source-latest-resolute.deb
  sudo apt install ./groonga-apt-source-latest-resolute.deb
)
sudo apt update
apt-cache policy postgresql-18 postgresql-18-pgroonga
apt-cache madison postgresql-18-pgroonga
```

検証基準はPGroonga拡張4.1.0。`apt-cache madison`で承認済みのパッケージ版を確認し、次の`<パッケージ版>`にはリポジトリが表示した完全な版を指定する。目的の版を入手できない場合はソースビルドか、別の版での検証を選ぶ。

```sh
sudo apt install 'postgresql-18-pgroonga=<パッケージ版>'
dpkg-query -W 'postgresql-18-pgroonga' 'libgroonga*'
```

次節のDB有効化ではPostgreSQLが公開している拡張版を照合する。APTパッケージ版とSQLの拡張版は別の値である。

### 固定ソースからビルドする場合

PGroonga 4.1.0の[ビルド定義](https://github.com/pgroonga/pgroonga/blob/4.1.0/meson.build)はGroonga 14.1.0以上を要求する。Ubuntuの[libgroonga-dev](https://packages.ubuntu.com/resolute/libgroonga-dev)は確認時16.0.0+dfsg-1。Macの16.1.4とは異なるので、実際の候補版と採用版を記録する。

```sh
sudo apt update
sudo apt install -y software-properties-common
sudo add-apt-repository -y universe
sudo apt update
sudo apt install -y build-essential ca-certificates curl pkg-config \
  postgresql-server-dev-18 libgroonga-dev libxxhash-dev meson ninja-build
apt-cache policy postgresql-18 libgroonga-dev libxxhash-dev meson
PG_CONFIG=/usr/lib/postgresql/18/bin/pg_config
"$PG_CONFIG" --version
"$PG_CONFIG" --pkglibdir
"$PG_CONFIG" --sharedir
pkg-config --modversion groonga libxxhash
pkg-config --atleast-version=14.1.0 groonga
```

Groongaの最低版確認が失敗した場合は先へ進まず、導入元を確認する。`pg_config`は接続先サーバーと同じPostgreSQL 18のものを使う。新しい作業先で次を実行する。既存の同名ビルドディレクトリがあれば、版・オプションを確認して再設定する。

```sh
mkdir -p "$HOME/file-scan-build"
cd "$HOME/file-scan-build"
curl -fL https://packages.groonga.org/source/pgroonga/pgroonga-4.1.0.tar.gz -o pgroonga-4.1.0.tar.gz
printf '%s  %s\n' '0ce8d58e73820bb74e82882afa58d6c0c5c46d43ae8349c6fc05c7097b85ee40' 'pgroonga-4.1.0.tar.gz' | sha256sum --check
tar -xzf pgroonga-4.1.0.tar.gz
meson setup pgroonga-4.1.0-build pgroonga-4.1.0 \
  --buildtype=release --wrap-mode=nofallback \
  -Dpg_config=/usr/lib/postgresql/18/bin/pg_config \
  -Dinstall_to_postgresql=true -Dxxhash=enabled -Dmessage_pack=auto -Dtest=false
meson compile -C pgroonga-4.1.0-build
sudo meson install -C pgroonga-4.1.0-build
sudo ldconfig
test -f /usr/lib/postgresql/18/lib/pgroonga.so
test -f /usr/share/postgresql/18/extension/pgroonga.control
```

MessagePackはこのUbuntu例では自動検出とし、日本語全文検索の必須依存にしない。xxHashはOSの開発パッケージを使い、Meson wrapの暗黙取得を禁止する。実際のPostgreSQL配置が異なる場合は、最後の2パスも`pg_config --pkglibdir`・`--sharedir`に合わせる。Ubuntuの標準配置では、Mac用のcontrol書換え・拡張検索パス変更は行わない。

APTとソースビルドのいずれを選んでも、採用構成でDB有効化・日本語検索・既存DBの更新手順をUbuntu実機で確認する。

## 4. DBごとに拡張を有効化する

サーバー側ファイルを準備した後、DB管理者が**対象DBごと**に行う。Macでは先に8節の検索パス設定を行う。DB作成やpgvectorのサーバー導入は[共通手順](environment-setup.md)を参照する。

```sh
# Ubuntuでのローカル管理接続例
sudo -u postgres psql -d file_scan -v ON_ERROR_STOP=1
```

```sql
SELECT name, version FROM pg_available_extension_versions
WHERE (name, version) IN (('vector', '0.8.6'), ('pgroonga', '4.1.0'));
CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public VERSION '0.8.6';
CREATE EXTENSION IF NOT EXISTS pgroonga WITH SCHEMA public VERSION '4.1.0';
SELECT e.extname, e.extversion, n.nspname AS extension_schema
FROM pg_extension e JOIN pg_namespace n ON n.oid = e.extnamespace
WHERE e.extname IN ('vector', 'pgroonga');
\connect file_scan_test
CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public VERSION '0.8.6';
CREATE EXTENSION IF NOT EXISTS pgroonga WITH SCHEMA public VERSION '4.1.0';
SELECT e.extname, e.extversion, n.nspname AS extension_schema
FROM pg_extension e JOIN pg_namespace n ON n.oid = e.extnamespace
WHERE e.extname IN ('vector', 'pgroonga');
\quit
```

`pg_available_extension_versions`で採用版を確認してから有効化する。`file_scan_test`は検証専用DBを用意した場合だけ接続する。`IF NOT EXISTS`は、既存拡張の版・配置を更新しない。結果を確認し、既存PGroongaを別スキーマへ置いていた場合は、その場で削除・移動せず対応を判断する。[CREATE EXTENSIONの仕様](https://www.postgresql.org/docs/18/sql-createextension.html)

[scripts/init-pgroonga.sql](../../scripts/init-pgroonga.sql)も、対象DBへPGroongaを有効化するだけの補助SQLである。アプリの表や公開対象は作らない。拡張の関数・演算子は新規構成では`public`、取込・検索の表は`winnap`へ配置する。旧`pgroonga.*`互換APIには依存しない。

## 5. 検索テーブル・全文索引と公開資料を準備する

以下は開発チェックアウトのルートで、設定済みの管理用DB接続を使う例。本番では`file_scan_admin`でコマンドを起動する。表・索引の管理はDB所有ロールを使う。[本番の初期化手順](search-api-production.md#5-テーブル初期化と実資料の公開)

```sh
source .venv/bin/activate
set -a
source .env
set +a
file-scan init-db --config config.local.yaml
file-search init-db --config config.local.yaml
```

`file-scan init-db`が取込v3を、`file-search init-db`が検索v1と索引を同じ`winnap`内へ作る。検索側は`ix_search_chunks_fulltext`を作成するため、次のDDLを手動で重複実行しない。旧`public`配置がある場合は[専用スキーマ移行](winnap-schema.md)を先に行う。

DB管理者が対象DBで確認する。

```sql
SELECT version FROM winnap.schema_migrations;
SELECT version FROM winnap.search_schema_migrations;
SELECT i.indexrelid::regclass AS index_name, i.indisvalid, am.amname,
       pg_get_indexdef(i.indexrelid) AS definition
FROM pg_index i
JOIN pg_class index_class ON index_class.oid = i.indexrelid
JOIN pg_am am ON am.oid = index_class.relam
WHERE i.indexrelid = to_regclass('winnap.ix_search_chunks_fulltext');
```

期待値は取込`3`・検索`1`、索引1行で`indisvalid=true`・`amname=pgroonga`。定義には`search_text`、`TokenBigram`、`NormalizerAuto`が含まれる。検索版1があっても、必須索引が欠落または使用不能なら`init-db`は索引名を示して停止する。通常モードの`/healthz`と検索も503を返す。別途、DB管理者が実消失・無効状態・定義の誤りを調査し、正しいDDLで復旧する。

索引作成後、`ingest → embed → create-release → publish`で実際の採用原本・世代を登録する。[APIの公開管理手順](search-api.md)を参照。取込下書きや仮の承認記録を直接SQLで公開しない。通常検索には`search.test_mode: false`、有効な質問Embedding設定・キーが必要である。テストモードの固定応答はPGroongaの動作確認にはならない。

## 6. 一時表で日本語検索を確認する

vector・PGroongaを有効化した**検証専用DB**へ接続して、次のSQLを同じ接続内で実行する。原本・アプリ表は使わず、最後にROLLBACKして一時表・索引を消す。

```sql
BEGIN;
SET LOCAL search_path = pg_temp, public;
CREATE TEMP TABLE pgroonga_smoke (
  id integer, content text, embedding vector(3)
) ON COMMIT DROP;
INSERT INTO pgroonga_smoke VALUES
  (1, '日次締め処理で E1203 が発生する条件', '[1,0,0]'),
  (2, '在庫照会の操作手順 E12030', '[0,1,0]'),
  (3, '監視のエラーコード Ｅ１２０３', '[0,0,1]');
CREATE INDEX pgroonga_smoke_fulltext ON pgroonga_smoke USING pgroonga
  (content) WITH (tokenizer='TokenBigram', normalizers='NormalizerAuto');
SET LOCAL enable_seqscan = off;
SELECT id, pgroonga_score(tableoid, ctid) AS score
FROM pgroonga_smoke WHERE content &@ '日次締め' ORDER BY id;
SELECT id FROM pgroonga_smoke WHERE content &@ 'E1203' ORDER BY id;
SELECT id FROM pgroonga_smoke WHERE content &@ '未登録の用語';
EXPLAIN SELECT id FROM pgroonga_smoke WHERE content &@ '日次締め';
SELECT token->>'value' AS term
FROM unnest(pgroonga_tokenize('日次締め E1203',
  'tokenizer', 'TokenBigram', 'normalizer', 'NormalizerAuto')) AS tokens(token);
SELECT id FROM pgroonga_smoke
ORDER BY embedding <=> '[1,0,0]'::vector LIMIT 1;
ROLLBACK;
```

- 日本語の検索は`id=1`、英数字検索は`1, 3`、未登録語は0行、cosine距離の先頭は`1`を期待する。
- この英数字例では`E12030`を除外できることを確認する。任意の識別子の完全一致を全文検索で保証するという意味ではなく、APIの識別子配列経路は別に検証する。
- `EXPLAIN`に`pgroonga_smoke_fulltext`が現れることを確認する。`enable_seqscan=off`は、このトランザクション内の索引利用確認だけに使い、本番の常設設定へ入れない。
- `pgroonga_score(tableoid, ctid)`は逐次走査では0となる。0を理由に検索結果を非関連と断定せず、実行計画を確認する。PGroongaの生スコアを、APIのRRF互換スコアと混同しない。[スコア仕様](https://pgroonga.github.io/reference/functions/pgroonga-score.html)

今回のMac構成で`日次締め E1203`を分割した結果は`日次 / 次締 / 締め / め / e / 1203`だった。APIは1文字の`め`・`e`を外す。英字と数字の境界で分かれる場合があり、`E1203`が必ず1語になると想定しない。コード全体の一致を使う場合は、別の識別子経路も確認する。

このSQLが通っても、実資料での検索品質・性能を確認したことにはならない。実データ量と絞り込み条件で、強制設定なしの`EXPLAIN`とAPI試験も行う。

## 7. 更新・復旧時の扱い

| 状況 | 対応 |
| --- | --- |
| `pgroonga.control`が見つからない | インストール先と接続先サーバーが同じPG18か、`pg_config`・拡張検索パスを照合 |
| ライブラリをロードできない | Linuxは`pgroonga.so`、このMacは`pgroonga.dylib`を確認。依存ライブラリ・RPATH・実行ユーザーの読取権限も確認 |
| 拡張はあるがAPIが404 | ナレッジ・業務版・公開対象の準備を確認。拡張導入だけでは資料を公開しない |
| 索引が0行／invalid、スコアが常に0 | 索引定義・有効性・実行計画を確認。管理者が修復を判断し、版の行だけを消して再初期化しない |
| UbuntuでMeson依存エラー | Groonga最低版・libxxhash開発パッケージ・`pkg-config`・PG18の開発ヘッダーを確認 |

PGroongaやGroongaを更新するときは、APIと取込・Embedding・公開処理を止め、DBとstorageを揃えてバックアップする。現在版と移行先版の[公式互換表](https://pgroonga.github.io/upgrade/)を確認し、サーバーバイナリの更新後にDBごとの拡張更新要否を判断する。`CREATE EXTENSION IF NOT EXISTS`や`file-search init-db`は、拡張更新の代わりにはならない。通常手順として`DROP EXTENSION ... CASCADE`を実行しない。

索引の分割・正規化設定を変える場合は、質問側の分割も揃え、索引の再作成と再評価を計画する。`REINDEX`だけでは`TokenBigram`等の定義を別設定へ変更できない。

`CREATE EXTENSION`だけでPGroongaのクラッシュ復旧・WAL連携・複製が構成されるわけではない。[Crash safe](https://pgroonga.github.io/reference/crash-safe.html)には実験的機能の注意があり、[WALを使う複製](https://pgroonga.github.io/reference/replication.html)も別設定が必要である。採用方式に応じて障害試験を行う。

バックアップ・停止・原本との整合・復元試験は[本番構築手順の12節](search-api-production.md#12-バックアップ復旧)を参照。論理dumpからの復元には、採用版のサーバー拡張とロールを先に準備し、索引再構築時間も確認する。Macのカスタム拡張パスは、新しい空DBへ先に設定する。dumpを別名の空DBへ復元するだけで、旧DB固有の`ALTER DATABASE`設定が移るとは想定しない。[pg_dump](https://www.postgresql.org/docs/18/app-pgdump.html)、[pg_restore](https://www.postgresql.org/docs/18/app-pgrestore.html)

## 8. Mac：初回と同じ構成を再ビルドする

既存のPython 3.14系の`.venv`とPostgres.app 18を使い、リポジトリのルートで実行する。ビルドツールは専用venvへ入れ、アプリの依存定義を変更しない。

Xcode Command Line Toolsと、Postgres.app 18の開発ヘッダーが必要となる。`xcode-select -p`と`/Applications/Postgres.app/Contents/Versions/18/bin/pg_config --version`を確認してから進む。

```sh
mkdir -p artifacts/pgroonga-build
.venv/bin/python -m venv artifacts/pgroonga-build/tools
artifacts/pgroonga-build/tools/bin/python -m pip install \
  cmake==3.31.10 meson==1.12.1 ninja==1.13.2 pkgconf==3.0.7.post0
export PATH="$PWD/artifacts/pgroonga-build/tools/bin:$PATH"
```

Groonga同梱RapidJSONのビルドがCMake 4の古い互換設定削除に対応していないため、今回の構成はCMake 3系へ固定した。

公式アーカイブのハッシュを確認して展開する。

```sh
.venv/bin/python - <<'PY'
import hashlib
import json
from pathlib import Path
import tarfile
import urllib.request

base = Path('artifacts/pgroonga-build').resolve()
records = []
sources = [
    ('groonga', '16.1.4', 'ba36450702f2a094cf68b399e9adad9cb0e69d796a19a49cd642b55a710adf3e'),
    ('pgroonga', '4.1.0', '0ce8d58e73820bb74e82882afa58d6c0c5c46d43ae8349c6fc05c7097b85ee40'),
]
for name, version, expected in sources:
    url = f'https://packages.groonga.org/source/{name}/{name}-{version}.tar.gz'
    archive = base / f'{name}-{version}.tar.gz'
    data = archive.read_bytes() if archive.exists() else urllib.request.urlopen(url, timeout=60).read()
    if hashlib.sha256(data).hexdigest() != expected:
        raise SystemExit(f'{name}: source hash mismatch')
    archive.write_bytes(data)
    if not (base / f'{name}-{version}').exists():
        with tarfile.open(archive) as bundle:
            bundle.extractall(base, filter='data')
    records.append({'name': name, 'version': version, 'url': url,
                    'sha256': expected, 'bytes': len(data)})
(base / 'sources.json').write_text(json.dumps(records, indent=2) + '\n')
PY
```

Groongaをビルド・インストールする。形態素辞書、mruby、外部LLM等の追加機能は使わず、今回の日本語検索は組み込みのbigramを使う。

```sh
cmake -S artifacts/pgroonga-build/groonga-16.1.4 \
  -B artifacts/pgroonga-build/groonga-build -G Ninja \
  -DCMAKE_BUILD_TYPE=Release \
  -DCMAKE_INSTALL_PREFIX="$PWD/artifacts/pgroonga-runtime" \
  -DCMAKE_INSTALL_RPATH="$PWD/artifacts/pgroonga-runtime/lib" \
  -DGRN_WITH_MESSAGE_PACK=bundled -DGRN_WITH_XXHASH=bundled \
  -DGRN_WITH_MRUBY=no -DGRN_WITH_MECAB=no \
  -DGRN_WITH_LIBEDIT=no -DGRN_WITH_LIBEVENT=no -DGRN_WITH_ZEROMQ=no \
  -DGRN_WITH_APACHE_ARROW=OFF -DGRN_WITH_H3=no \
  -DGRN_WITH_SIMSIMD=no -DGRN_WITH_USEARCH=no -DGRN_WITH_XSIMD=no \
  -DGRN_WITH_SIMDJSON=no -DGRN_WITH_LLAMA_CPP=no \
  -DGRN_WITH_ZSTD=no -DGRN_WITH_LZ4=no -DGRN_WITH_BLOSC=no -DGRN_WITH_CURL=no
cmake --build artifacts/pgroonga-build/groonga-build --parallel 4
cmake --install artifacts/pgroonga-build/groonga-build
cmake --install artifacts/pgroonga-build/groonga-build/_deps/msgpack-c-build \
  --prefix "$PWD/artifacts/pgroonga-runtime"
```

PGroongaをビルド・インストールする。xxHashはリリース付属のMeson wrap定義に従ってハッシュ検証付きで取得される。再設定時は`meson setup`へ`--reconfigure`を付ける。

```sh
export PKG_CONFIG_PATH="$PWD/artifacts/pgroonga-runtime/lib/pkgconfig"
export LDFLAGS="-Wl,-rpath,$PWD/artifacts/pgroonga-runtime/lib"
meson setup artifacts/pgroonga-build/pgroonga-build \
  artifacts/pgroonga-build/pgroonga-4.1.0 \
  --buildtype=release --prefix="$PWD/artifacts/pgroonga-runtime" --libdir=lib \
  -Dpg_config=/Applications/Postgres.app/Contents/Versions/18/bin/pg_config \
  -Dinstall_to_postgresql=false -Dtest=false -Dmessage_pack=enabled -Dxxhash=enabled
meson compile -C artifacts/pgroonga-build/pgroonga-build -j 4
meson install -C artifacts/pgroonga-build/pgroonga-build --no-rebuild
.venv/bin/python - <<'PY'
from pathlib import Path
for path in Path('artifacts/pgroonga-runtime/share/extension').glob('*.control'):
    # $libdir/を残すとPostgres.app内を固定参照するため、標準の検索パスで解決する。
    path.write_text(path.read_text().replace("module_pathname = '$libdir/", "module_pathname = '"))
PY
```

### MacのDBごとの拡張検索パス

初回は、既存のアプリ用・テスト用DBへ次の設定を追加した。再現時の`<リポジトリ絶対パス>`は実際のチェックアウトのパスへ置き換える。`$system`と`$libdir`はPostgreSQLが解釈するリテラルなので、シェルの二重引用符で展開させない。

```sql
ALTER DATABASE file_scan SET extension_control_path
  TO '<リポジトリ絶対パス>/artifacts/pgroonga-runtime/share:$system';
ALTER DATABASE file_scan SET dynamic_library_path
  TO '<リポジトリ絶対パス>/artifacts/pgroonga-runtime/lib:$libdir';
ALTER DATABASE file_scan_test SET extension_control_path
  TO '<リポジトリ絶対パス>/artifacts/pgroonga-runtime/share:$system';
ALTER DATABASE file_scan_test SET dynamic_library_path
  TO '<リポジトリ絶対パス>/artifacts/pgroonga-runtime/lib:$libdir';
```

`extension_control_path`には`share/extension`ではなく`share`を指定する。PostgreSQLが各要素に`extension`を補う。設定変更はDB管理者が行い、**新しい接続**で4節の有効化SQLを実行する。この検索パス変更自体にサーバー再起動は不要だが、ロード済みライブラリの置換やpreload設定変更では再接続・再起動を別途判断する。

標準の`.so`を想定せず、このPostgres.app 18用ビルドの実体は`artifacts/pgroonga-runtime/lib/pgroonga.dylib`を確認する。

```sql
SHOW extension_control_path;
SHOW dynamic_library_path;
CREATE EXTENSION IF NOT EXISTS pgroonga WITH SCHEMA public VERSION '4.1.0';
SELECT extname, extversion FROM pg_extension
 WHERE extname IN ('vector', 'pgroonga');
```

新規のネイティブDBも[既存の初期化](../../scripts/init-postgres.sql)の後、この設定・有効化を行う。`init-postgres.sql`と既存Composeはpgvectorのみの初期化を維持している。

## 9. 検証と記録

固定依存と`dev`・`search` extra、トークナイザー辞書を準備し、`.env`を読み込んでテスト専用DBで実行する。全体試験には公開サンプルとDocling／OCRモデルも必要となるため、[環境構築手順](environment-setup.md)に従う。検索テストがスキップされた結果を、検索経路の検証成功として扱わない。

```sh
set -a
source .env
set +a
.venv/bin/python scripts/test_postgres.py tests/test_pgroonga.py tests/test_postgres.py tests/test_search.py
.venv/bin/python scripts/test_postgres.py
.venv/bin/python -m ruff check src tests scripts
```

[拡張テスト](../../tests/test_pgroonga.py)は一時表で日本語検索、全角英数字の正規化、似た別のエラーコードの除外、ヒットなし、score、`EXPLAIN`での索引利用、同じ表のpgvector距離演算を検査する。[検索テスト](../../tests/test_search.py)は公開対象・3経路・入力・coverage・更新・出典・期限等を実DBで検証し、OpenAIはモックを使う。

初回導入時の「全160テスト成功」は2026-10-25の履歴で、現行のテスト総数ではない。直近の全体試験記録は[環境構築手順の検証結果](environment-setup.md)も参照し、導入先では実行日・OS/CPU・版・対象テスト・成功／スキップ数を改めて記録する。本番DBをテスト接続先にしない。

今回の再精査では、検索API・スキーマ配置・Mesonの固定ソース・公式の対応OS／演算子／更新手順と照合した。Mac再現用アーカイブ2点のSHA-256も再計算し、記載値と一致した。Ubuntuでのパッケージ導入・再ビルド、実資料での検索品質・負荷・実OpenAI接続は今回の確認範囲には含めない。

2026-11-03の実動確認は、既存DBから独立した一時PostgreSQL 18.6で行った。PGroonga 4.1.0・Groonga 16.1.4・pgvector 0.8.6を使用し、6節のSQLと関連21テストが成功した。`search_path=winnap, public`から補助SQLを2回実行し、PGroongaの配置が`public`となることと再実行可能であることも確認した。検証クラスタは終了後に停止・削除した。記録は`artifacts/reports/pgroonga-review-2026-11-03.json`、テスト結果は`artifacts/pgroonga-review-2026-11-03/pytest.log`に保存した。
