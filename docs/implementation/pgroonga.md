# ローカルPostgreSQLへのPGroonga導入

導入・検証日：2026-10-25。検索APIは未実装。今回の変更はPGroongaの実行環境・DB有効化・回帰検証と、権限制御を保留する初回実装範囲の設計反映である。

## 導入結果

| 項目 | 導入した構成 |
| --- | --- |
| PostgreSQL | 既存Postgres.appの18.6、arm64。サーバー置換・再初期化なし |
| pgvector | 既存の0.8.6を維持 |
| PGroonga | 4.1.0 |
| Groonga | 16.1.4。MessagePack 6.1.0を同梱ソースから使用 |
| xxHash | PGroongaのMeson wrap定義による0.8.3 |
| 有効化したDB | `file_scan`、`file_scan_test` |
| 初期の全文索引設定 | `TokenBigram`＋`NormalizerAuto`。識別子の要素一致は別経路で大文字小文字・記号を保持 |

Homebrew・Dockerは現在のMacにないため、公式リリースソースから既存PostgreSQLのヘッダーを使ってビルドした。Postgres.app内への拡張ファイル追加はmacOSに拒否されたため、**PostgreSQL 18の標準の拡張検索パスを使い、プロジェクト専用領域へ導入**した。[PGroonga公式ソース導入手順](https://pgroonga.github.io/install/source.html)、[PostgreSQLの拡張検索パス](https://www.postgresql.org/docs/18/runtime-config-client.html#RUNTIME-CONFIG-CLIENT-OTHER)

- 実行時に必要なファイル：`artifacts/pgroonga-runtime/`。共有ライブラリ、拡張control／SQL、Groongaの実行ファイル等を保持する。
- 再ビルド用：`artifacts/pgroonga-build/`。取得元・SHA-256は`sources.json`、設定とビルド結果は`*-configure.log`、`*-compile.log`、`*-install.log`に保存する。
- 実行時ライブラリとDB設定はこのチェックアウトの絶対パスを使う。**runtimeはビルド用キャッシュとして削除しない**。チェックアウトを移動する場合は再ビルドとDB設定の変更が必要。
- Postgres.appのアップデート後は新しいPostgreSQLのヘッダーでの再ビルド要否と拡張の読み込みを確認する。
- Docker Composeの既存イメージはpgvectorのみ。このネイティブ版の導入をCompose対応済みとは扱わない。

## 同じ構成を再ビルドする

既存のPython 3.14系の`.venv`とPostgres.app 18を使い、リポジトリのルートで実行する。ビルドツールは専用venvへ入れ、アプリの依存定義を変更しない。

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

## DBごとの有効化

今回、既存のアプリ用・テスト用DBへ次の設定を追加した。`<リポジトリ絶対パス>`は実際のチェックアウトのパスへ置き換える。`$system`と`$libdir`はPostgreSQLが解釈するリテラルなので、シェルの二重引用符で展開させない。

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

設定後は**新しい接続**で各DBへ接続し、[拡張有効化SQL](../../scripts/init-pgroonga.sql)を実行する。サーバー再起動は不要。既存テーブル・文書の公開状態を変更せず、検索用の本番チャンク索引もこの段階では作らない。

```sql
CREATE EXTENSION IF NOT EXISTS pgroonga;
SELECT extname, extversion FROM pg_extension
 WHERE extname IN ('vector', 'pgroonga');
```

新規のネイティブDBも[既存の初期化](../../scripts/init-postgres.sql)の後、この設定・有効化を行う。`init-postgres.sql`と既存Composeはpgvectorのみの初期化を維持している。

## 検証

`.env`を読み込み、テスト専用DBで実行する。

```sh
set -a
source .env
set +a
.venv/bin/python scripts/test_postgres.py tests/test_pgroonga.py tests/test_postgres.py
.venv/bin/python scripts/test_postgres.py
.venv/bin/python -m ruff check src tests scripts
```

[追加テスト](../../tests/test_pgroonga.py)は一時表で日本語検索、全角英数字の正規化、似た別のエラーコードの除外、ヒットなし、score、`EXPLAIN`での索引利用、同じ表のpgvector距離演算を検査する。アプリ用DBの文書・世代・チャンク件数が導入前後で同じことも確認した。全160テスト成功（38.72秒、スキップなし）、ruffも成功。Docling/Pydantic由来の既存非推奨警告5件がある。

この確認は拡張の導入と基本動作の検証である。実文書の検索API、3経路の統合、日本語質問からの検索語作成、精度・性能の検証は今後の実装に含む。
