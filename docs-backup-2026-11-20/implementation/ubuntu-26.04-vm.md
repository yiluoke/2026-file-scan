# 本番導入先：Windowsホスト上のUbuntu 26.04 VM

2026-11-02：日本語PDF OCRにRapidOCR＋ONNX Runtime CPUを追加した。モデル取得コマンドはOCRモデルも配置する。設定にpdf_ocr（enabled=true、mode=auto、scale=3.0、timeout_seconds=1800）を含める。モデルと両manifestは実行ユーザーが読める場所へ配置する。既存環境更新と受入確認は[日本語OCR](rapidocr-pdf.md)を参照。

更新日：2026-11-15。導入先はWindowsホスト上で稼働するUbuntu 26.04 LTSの仮想マシン。アプリ・Python・DB・モデルはUbuntu内へ配置する。本書はその環境向けの構築案と実行例であり、Ubuntu実機での受入確認はこれから行う。

必要ライブラリの版と役割、モデル準備、テーブルSQLの詳細は[共通の環境構築手順](environment-setup.md)も参照する。以下はUbuntu VMのコマンドであり、Windows PowerShellでは実行しない。

## 1. 構成とUbuntu 26.04での注意点

| 項目 | 本環境での配置・方針 |
| --- | --- |
| Windows | 仮想マシンのホスト。仮想化ソフトの種類は未指定 |
| Ubuntu | 26.04 LTS。`uname -m`でCPUアーキテクチャを確認する |
| Python | Ubuntu標準の3.14系を使用。確認版3.14.4。標準版から仮想環境を作成する |
| DB | Ubuntu内のPostgreSQL 18＋pgvector 0.8.6。検索APIを使う場合はPGroongaも必須 |
| アプリ | `/opt/file-scan/`へソース一式、同ディレクトリの`.venv`へPython依存を導入する例 |
| アプリ設定・ソース | `/opt/file-scan/config.local.yaml`・`.env`。処理は`/opt/file-scan/src/file_scan/`と`/opt/file-scan/src/search/` |
| 原本・モデル・レポート | `/var/lib/file-scan/`配下。Ubuntu側のローカルディスクを使う |
| トークナイザー | `/var/cache/file-scan/tiktoken/` |
| 取込元 | SMB共有ならUbuntuへマウントし、`/mnt/file-server/department`等を指定する |

Ubuntu 26.04の標準Pythonは3.14系。2026-10-25に公式のrelease/updates/securityを確認し、Python 3.14.4と各Pythonライブラリの上流版を基準にローカル環境を変更した。標準版との差・追加依存・検証結果は[Python版整合の記録](ubuntu-26.04-python.md)を参照。Ubuntu自体をローカルで起動する必要はない。

Ubuntu 26.04の`postgresql-18-pgvector`は、確認時の標準配布情報では0.8.1-2であり、ローカルの0.8.6とは異なる。本書では0.8.6の公式ソースを使う。実際の候補版はVMで確認して記録する。[Ubuntuの配布情報](https://packages.ubuntu.com/source/resolute/pgvector)

DBのデータ領域・実体保管先・キャッシュはUbuntuのローカルファイルシステムへ置く。Windows／NASの共有は取り込み元として使用する。同梱`compose.yaml`は開発用のため、この本番例はUbuntuへのネイティブ導入で記載する。

## 2. OSパッケージと実行ユーザー

管理者権限で実行する。以下のユーザー・ディレクトリ作成は新規の場合だけ行う。

```sh
cat /etc/os-release
uname -m
sudo apt update
sudo apt install -y ca-certificates curl git build-essential pkg-config software-properties-common cifs-utils python3 python3-venv libgl1 libglib2.0-0t64
sudo add-apt-repository -y universe
sudo apt update
apt-cache policy postgresql-18 postgresql-18-pgvector libgroonga-dev
sudo apt install -y postgresql-18 postgresql-client-18 postgresql-server-dev-18
sudo adduser --system --group --home /var/lib/file-scan file-scan
sudo install -d -o root -g root -m 0755 /opt/file-scan
sudo install -d -o file-scan -g file-scan /var/lib/file-scan/storage /var/lib/file-scan/reports /var/lib/file-scan/models
sudo install -d -o file-scan -g file-scan /var/cache/file-scan/tiktoken
sudo systemctl enable --now postgresql
pg_lsclusters
```

配布ソース一式を管理者が`/opt/file-scan/`へ直接配置し、配置先とソースはroot所有で実行ユーザーが読み取れるようにする。既存環境が`file-scan`所有の場合は処理を停止し、設定を保護してからプロジェクトのディレクトリと配布ソースの所有者・書込権限を見直す。移設元の`.venv`やDBデータ領域はコピーしない。実行ユーザーには共有の読込権限と`/var/lib/file-scan/`・`/var/cache/file-scan/`の必要な書込権限を与える。

## 3. Ubuntu標準Python 3.14と固定依存

新規導入時はsudoを使える一般ユーザーが、書き込み可能な一時ディレクトリで配布ソースのビルドを行う。仮想環境は最終パスに作成し、検査後にroot所有へ戻す。稼働中の仮想環境は上書きしない。

```sh
(
  set -eu
  umask 022
  build_dir="$(mktemp -d)"
  trap 'rm -rf -- "$build_dir"' EXIT
  test ! -e /opt/file-scan/.venv
  sudo install -d -o "$(id -u)" -g "$(id -g)" -m 0755 /opt/file-scan/.venv
  cp -R /opt/file-scan/{pyproject.toml,README.md,requirements,scripts,src} "$build_dir/"
  chmod -R u+w "$build_dir"
  cd "$build_dir"
  python3.14 --version
  python3.14 -m venv /opt/file-scan/.venv
  /opt/file-scan/.venv/bin/python -m pip install -r requirements/ubuntu-26.04.lock
  /opt/file-scan/.venv/bin/python -m pip install --no-deps --no-build-isolation '.[dev,search]'
  /opt/file-scan/.venv/bin/python scripts/check_python_environment.py
  /opt/file-scan/.venv/bin/python -m pip check
  /opt/file-scan/.venv/bin/file-scan --version
  /opt/file-scan/.venv/bin/file-search --help
  sudo chown -hR root:root /opt/file-scan/.venv
)
```

確認基準はPython 3.14.4。Ubuntuの保守更新でパッチ版が進んだ場合は、版の対応表とテスト結果も更新する。lockはLinuxのCPU用PyTorchを公式配布元から取得する。Ubuntuの`/usr/bin/python3`の置換は不要。以降の本番アプリ操作は`file-scan`ユーザーと同じ仮想環境を使う。準備が途中で失敗した場合は、稼働中のものと区別し、今回作った不完全な`.venv`だけを確認してから再作成する。

## 4. DB拡張・DB・テーブル

### pgvector 0.8.6

管理者の作業ディレクトリで実行する。既存のvectorパッケージと重ねて上書きせず、この新規環境の導入方法を一つに揃える。

```sh
mkdir -p "$HOME/file-scan-build"
git clone --branch v0.8.6 --depth 1 https://github.com/pgvector/pgvector.git "$HOME/file-scan-build/pgvector-0.8.6"
cd "$HOME/file-scan-build/pgvector-0.8.6"
git rev-parse HEAD
make PG_CONFIG=/usr/lib/postgresql/18/bin/pg_config OPTFLAGS=""
sudo make PG_CONFIG=/usr/lib/postgresql/18/bin/pg_config install
```

CPU固有の最適化フラグを外し、VM移動時のCPU差を減らす例。[pgvector公式ビルド方法](https://github.com/pgvector/pgvector#installation)

### PGroonga（検索APIを使う場合は必須）

Ubuntu 26.04の標準PostgreSQL 18には、公式APTの`postgresql-18-pgroonga`を使用できる。採用版をAPTで用意できない場合は固定ソースからの導入も選べる。いずれか一方を選び、実際の拡張・依存版を記録する。[PGroonga手順3節](pgroonga.md#3-ubuntu-2604-vmへのサーバー側導入)、[公式Ubuntu手順](https://pgroonga.github.io/install/ubuntu.html)

### DB作成とスキーマ導入

取込単体または専用の検証DBを作る場合の手順。本番の検索APIと同時に導入する場合は、[検索API本番手順3節](search-api-production.md#3-ospythondb拡張)で本番DBと拡張を準備し、同5節でアプリ表を初期化する。

```sh
sudo -u postgres psql
```

```sql
CREATE ROLE file_scan LOGIN;
\password file_scan
CREATE DATABASE file_scan OWNER file_scan;
CREATE DATABASE file_scan_test OWNER file_scan;
\connect file_scan
CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public;
CREATE EXTENSION IF NOT EXISTS pgroonga WITH SCHEMA public;
\connect file_scan_test
CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public;
CREATE EXTENSION IF NOT EXISTS pgroonga WITH SCHEMA public;
\quit
```

PGroongaを導入しない取り込み専用環境は、その2行だけ省略する。次はアプリ実行ユーザーから、TCP接続で配布SQLを実行する。

```sh
cd /opt/file-scan
psql -h 127.0.0.1 -U file_scan -d file_scan -v ON_ERROR_STOP=1 -f sql/postgresql/schema-v3.sql
psql -h 127.0.0.1 -U file_scan -d file_scan -c "SELECT version FROM winnap.schema_migrations;"
psql -h 127.0.0.1 -U file_scan -d file_scan -c "SELECT extname, extversion FROM pg_extension WHERE extname IN ('vector', 'pgroonga');"
```

新規SQLは20テーブルを作成し、スキーマ版は3になる。既存DBへの更新には`file-scan init-db`を使う。DB接続は同じUbuntu内の`127.0.0.1:5432`。Windowsホストの`127.0.0.1`とは区別する。

## 5. アプリ設定・モデル

設定ファイルは`/opt/file-scan/`直下に置く。YAML内の相対パスは、設定ファイルの親ディレクトリが基準となる。次は新規作成時の例。管理者がDBパスワードと必要に応じてAPIキーを設定する。既存ファイルは上書きせず所有者とモードを確認する。検索APIも運用する場合は、同じ`config.local.yaml`と取込用`.env`を引き継ぎ、API用`.env.search`を追加する。[検索API本番手順4節](search-api-production.md#4-共通設定と認証情報)

```sh
cd /opt/file-scan
if [ ! -e config.local.yaml ]; then
  sudo install -o root -g file-scan -m 0640 config.example.yaml config.local.yaml
fi
if [ ! -e .env ]; then
  sudo install -o root -g root -m 0600 /dev/null .env
fi
sudoedit /opt/file-scan/config.local.yaml /opt/file-scan/.env
```

`.env`にはDB接続先とキャッシュを設定し、Embeddingを使う場合に`OPENAI_API_KEY`を追加する。`PASSWORD`は実値をURL用にエンコードして指定する。検証DBを使用する自動テストは専用環境で行う。

```dotenv
FILE_SCAN_DATABASE_URL=postgresql+psycopg://file_scan:PASSWORD@127.0.0.1:5432/file_scan
TIKTOKEN_CACHE_DIR=/var/cache/file-scan/tiktoken
HF_HOME=/var/cache/file-scan/huggingface
```

`config.local.yaml`では保管先とモデル配置先を絶対パスにする。

```yaml
storage_dir: /var/lib/file-scan/storage
report_dir: /var/lib/file-scan/reports
docling_artifacts_path: /var/lib/file-scan/models/docling
```

手動操作ではBashセッションで以下の関数を定義する。systemdがroot所有の`.env`を環境変数として読み、コマンドは`file-scan`ユーザーで実行する。[systemd-runの仕様](https://www.freedesktop.org/software/systemd/man/latest/systemd-run.html)、[EnvironmentFileの仕様](https://www.freedesktop.org/software/systemd/man/latest/systemd.exec.html#EnvironmentFile=)

```sh
file_scan_admin() {
  sudo systemd-run --wait --pipe --collect --quiet --expand-environment=no \
    --property=User=file-scan --property=Group=file-scan --property=UMask=0077 \
    --property=WorkingDirectory=/opt/file-scan \
    --property=EnvironmentFile=/opt/file-scan/.env -- "$@"
}
```

```sh
sudo install -d -o file-scan -g file-scan /var/cache/file-scan/huggingface
file_scan_admin /opt/file-scan/.venv/bin/python /opt/file-scan/scripts/download_docling_models.py --config /opt/file-scan/config.local.yaml
file_scan_admin /opt/file-scan/.venv/bin/python /opt/file-scan/scripts/download_docling_models.py --config /opt/file-scan/config.local.yaml --verify-only
file_scan_admin /opt/file-scan/.venv/bin/python -c "from file_scan.tokens import count_tokens; print(count_tokens('Ubuntu VMの確認'))"
```

## 6. Windows／NASの共有フォルダを収集する場合

VMのネットワークから共有サーバー名を解決でき、SMBのTCP 445へ到達できる構成を用意する。Windows側の共有権限・ファイル権限・ファイアウォールも、VMからの実行に合わせる。ホストのドライブ文字をUbuntuのパスへ直接指定しない。

管理者が認証ファイルを用意する。値は導入先のものへ置き換え、共有の読込用アカウントを使用する。

```sh
sudo install -d -m 700 /etc/file-scan
sudo install -m 600 /dev/null /etc/file-scan/smb-credentials
sudoedit /etc/file-scan/smb-credentials
sudo install -d /mnt/file-server
id file-scan
```

認証ファイルの形式：

```ini
username=SMB_USER
password=SMB_PASSWORD
domain=SMB_DOMAIN
```

ドメイン指定が不要ならdomain行を省略する。`/etc/fstab`へ次の形式で追加する。`FILE_SERVER`・`SHARE`は共有元、`UID`・`GID`は`id file-scan`で得た数値に置き換える。SMBの版は共有サーバーに対応するものを使う。

```fstab
//FILE_SERVER/SHARE /mnt/file-server cifs credentials=/etc/file-scan/smb-credentials,vers=3.1.1,ro,uid=UID,gid=GID,file_mode=0440,dir_mode=0550,_netdev,x-systemd.automount,nofail 0 0
```

```sh
sudo systemctl daemon-reload
sudo mount /mnt/file-server
findmnt /mnt/file-server
sudo -u file-scan test -r /mnt/file-server/department
findmnt -rn -t cifs -T /mnt/file-server/department
```

Ubuntuのマウント先をYAMLへ指定する。

```yaml
sources:
  - source_id: department-share
    root: /mnt/file-server/department
    recursive: true
    exclude_directories: [旧版/]
    exclude_directory_names: [BK]  # どの階層でも同名フォルダと配下を除外
    exclude_filenames: ['~$*', '*.tmp']
```

**共有の未マウント状態で、残った空ディレクトリを取り込まない。** 現行バッチは正常に列挙できた範囲から消えたファイルを削除状態にするため、手動実行前にもマウント・共有内の読込・CIFS実体の検査を行う。autofsのマウントポイントが存在するだけで正常と判断しない。下記の起動例にもこれらの検査を入れる。[Ubuntu公式のCIFSマウント手順](https://ubuntu.com/server/docs/how-to/samba/mount-cifs-shares-permanently/)

## 7. Ubuntu上の受入確認

公開8サンプル、モデル・PDF・再実行の試験とpytestは、書き込み可能な検証用チェックアウトと専用DBで行う。サンプル取得スクリプトはチェックアウトにファイルを書き込む。8件の登録と再実行の8件の`unchanged`を確認する。PGroongaなしの場合は共通手順10節の取込専用テストを使う。[公開サンプルとテストの手順](environment-setup.md#7-収集元の準備と設定)

本番VMで共有を設定した後は6節のマウント検査を行い、5節で定義した`file_scan_admin`で設定に基づく走査と状態を確認する。実際の取り込みは次節のサービスで行う。

```sh
file_scan_admin /opt/file-scan/.venv/bin/file-scan scan --config /opt/file-scan/config.local.yaml
file_scan_admin /opt/file-scan/.venv/bin/file-scan status --config /opt/file-scan/config.local.yaml
/opt/file-scan/.venv/bin/python -m pip check
```

記録するもの：Ubuntu版・CPU、Pythonの実パッチ版、Python依存、DB・vector・PGroonga・Groonga版、モデルlock、テストの成功／スキップ、共有への読込、再起動後の接続・マウント状態。Windowsホストの稼働・VM自動起動方針も管理者側で設定する。

## 8. 再起動後の取り込み実行例

PostgreSQLはsystemdで起動し、共有はfstabの設定で利用する。バッチは手動または運用側で決めた実行契機から起動する。以下は共有マウントを確認してingestだけを実行する、oneshotサービスの例。定期実行時刻はここでは設定しない。

`/etc/systemd/system/file-scan-ingest.service`：

```ini
[Unit]
Description=File scan ingestion
Wants=network-online.target
After=network-online.target postgresql.service
Requires=postgresql.service
RequiresMountsFor=/mnt/file-server

[Service]
Type=oneshot
User=file-scan
Group=file-scan
WorkingDirectory=/opt/file-scan
EnvironmentFile=/opt/file-scan/.env
UMask=0077
ExecStartPre=/usr/bin/mountpoint -q /mnt/file-server
ExecStartPre=/usr/bin/test -r /mnt/file-server/department
ExecStartPre=/usr/bin/findmnt -rn -t cifs -T /mnt/file-server/department
ExecStart=/opt/file-scan/.venv/bin/file-scan ingest --config /opt/file-scan/config.local.yaml
TimeoutStartSec=infinity
KillMode=control-group
```

この例は6節の共有と実パスを設定した後で使う。`.env`は`名前=値`の形式で作り、root所有・`0600`にする。systemdが読み込み、`file-scan`で起動する取込処理へ環境変数を渡す。個別の管理操作には5節の`file_scan_admin`を使う。

```sh
sudo systemctl daemon-reload
sudo systemctl start file-scan-ingest.service
sudo systemctl status file-scan-ingest.service
sudo journalctl -u file-scan-ingest.service -n 100 --no-pager
```

部分抽出の終了コード2も確認対象になる。失敗時は実行レポートを確認する。Embeddingの自動実行はこの例に含めず、実キー・送信対象を設定した後に別途実行する。

## 9. 導入前後の扱い

- 現在手元で確認済みなのはMacのPython 3.14.4・PostgreSQL 18.6・vector 0.8.6・PGroonga 4.1.0。Ubuntu 26.04 VMの実行結果へ読み替えない。
- Ubuntu VM上のwheel・モデル・DB拡張は、そのOS・CPUで準備する。Macの仮想環境・DBデータディレクトリ・共有ライブラリはコピーしない。
- 本番の保存先は最初のingest前に決める。バックアップはDBと原本・画像の保管先を、処理を停止した整合する時点で揃える。
- Dockerを採用する運用へ変更する場合は、開発用Composeの流用ではなく、PGroonga・永続化・ユーザー・バックアップを含む本番構成を別途確定する。

文書作成時は公式情報・現行コードと照合し、記載したBashの構文・リンク・配布SQLの整合を確認した。ここに示すsudo操作・パッケージ導入・SMBマウント・systemd起動は、このMac環境では実行していない。
