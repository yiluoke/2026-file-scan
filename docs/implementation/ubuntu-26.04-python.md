# Ubuntu 26.04に合わせたPython・依存ライブラリ

確認日：2026-10-25。ローカルの検証環境をPython 3.14.4へ変更し、Ubuntu 26.04の標準配布に対応するPythonライブラリの上流版へ合わせた。Ubuntu OSの起動は今回の作業に含めない。

## 版の決め方

Ubuntu公式アーカイブの`resolute`、`resolute-updates`、`resolute-security`の`main`・`universe`を照合した。初期リリースだけでなく、確認時点の通常の更新・セキュリティ更新を含む。

- `python3`メタパッケージの版は`3.14.3-0ubuntu2`だが、実体の`python3.14`は`3.14.4-1ubuntu0.2`。実行版は**3.14.4**とする。
- PythonライブラリはUbuntuパッケージの上流版を使う。例：`python3-sqlalchemy 2.0.45+ds1-1build1`に対してPyPIの`SQLAlchemy==2.0.45`。
- Ubuntuに対応するPythonパッケージがないDocling等は、追加依存として別途版を固定する。
- UbuntuのDebianパッチ、ビルド設定、Linuxバイナリ自体をmacOSへ導入するものではない。PyPIの依存条件に合わせるため、下記5件には互換例外がある。

公式配布元：[Ubuntuアーカイブ](https://archive.ubuntu.com/ubuntu/dists/)、[Python](https://packages.ubuntu.com/resolute/python3.14)、[SQLAlchemy](https://packages.ubuntu.com/resolute/python3-sqlalchemy)、[OpenAI SDK](https://packages.ubuntu.com/resolute/python3-openai)。取得した6つのパッケージ索引のURL・SHA-256、全依存の対応表は[版の基準JSON](../../requirements/ubuntu-26.04-baseline.json)へ保存した。

## 主な採用版

| 項目 | 採用版 | Ubuntuの対応パッケージ |
| --- | --- | --- |
| Python | 3.14.4 | python3.14 |
| SQLAlchemy | 2.0.45 | python3-sqlalchemy |
| psycopg / psycopg-binary | 3.3.2 | python3-psycopg（binaryはPyPIの同版配布物） |
| PyYAML | 6.0.3 | python3-yaml |
| openpyxl | 3.1.5 | python3-openpyxl |
| python-docx | 1.2.0 | python3-docx |
| pypdf | 6.9.2 | python3-pypdf |
| charset-normalizer | 3.4.4 | python3-charset-normalizer |
| Pillow | 12.1.1 | python3-pil |
| OpenAI SDK | 2.21.0 | python3-openai |
| tiktoken | 0.12.0 | python3-tiktoken |
| NumPy / pandas | 2.3.5 / 2.3.3 | python3-numpy / python3-pandas |
| PyTorch | 2.9.1 | python3-torch。Linuxでは公式CPU wheelの`2.9.1+cpu` |
| OpenCV headless | 4.10.0.84 | python3-opencvの4.10.0に対応するPyPI wheel版 |
| FastAPI / Uvicorn / greenlet | 0.118.0 / 0.38.0 / 3.3.2 | python3-fastapi / python3-uvicorn / python3-greenlet |
| pytest / ReportLab / httpx | 9.0.2 / 4.4.10 / 0.28.1 | python3-pytest / python3-reportlab / python3-httpx |
| pip / setuptools | 25.1.1 / 78.1.1 | python3-pip / python3-setuptools |

追加依存には`docling-slim==2.140.0`、`docling-core==2.104.0`、`docling-ibm-models==4.0.4`、`docling-parse==7.23.0`、`transformers==4.57.6`、`torchvision==0.24.1`、Python側の`pgvector==0.5.1`、`ruff==0.17.1`等がある。Ubuntuの標準版が存在するとして扱わず、対応表では`supplemental`として記録する。

## 互換例外

| ライブラリ | Ubuntu上流版 | 採用版 | 理由 |
| --- | --- | --- | --- |
| h11 | 0.14.0 | 0.16.0 | PyPI版httpcore 1.0.9が`h11>=0.16`を要求する |
| huggingface-hub | 1.2.2 | 0.36.2 | 実PDFで表構造を保持できるTransformers 4.57.6が`huggingface-hub<1`を要求する |
| pydantic-settings | 2.13.0 | 2.14.0 | Docling Core 2.104.0が`>=2.14.0`を要求する |
| requests | 2.32.5 | 2.34.2 | Docling Core 2.104.0が`>=2.34.2`を要求する |
| Starlette | 0.50.0 | 0.48.0 | PyPI版FastAPI 0.118.0の条件が`>=0.40,<0.49`のため |

これらを含めて「全パッケージがUbuntu標準版と完全一致」とは扱わない。依存条件を満たす固定セットとして`pip check`と実処理で確認している。全ての版の完全一致を必須にする場合は、Docling等の機能・採用版か、Ubuntu配布物の利用方法を別途変更する必要がある。

Doclingの公式slim配布で、使用するPDF・ローカルモデル用extraだけを導入する。Office抽出とチャンク分割は本プロジェクトの実装を使うため、Docling側のOffice・chunking・OCR用extraは導入しない。実モデルの固定コミットとハッシュは従来のlockを使用する。PDFの抽出プロファイルにはTransformers・TorchVision・OpenCV・NumPy・SciPy等の版も含め、変更前の抽出キャッシュを再利用しない。

Transformers 5.0.0を使った試験では同じ公開PDFの表領域が検出されなかった。本文だけの確認では分からないため、6列の表セルを検査する実モデルテストを通る4.57.6へ固定した。テストの期待値を弱める変更は行っていない。

## 再構築と照合

Python 3.14.4を用意し、プロジェクトのルートで実行する。Ubuntuでは標準の`python3.14`と`python3-venv`を使用する。既存の仮想環境を別OSへコピーしない。

```sh
python3.14 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements/ubuntu-26.04.lock
python -m pip install --no-deps --no-build-isolation -e '.[dev,search]'
python scripts/check_python_environment.py
python -m pip check
```

先に全依存と構築ツールを導入するため、アプリ導入のpip操作で依存の再解決や構築ツールの更新を行う必要はない。lockは検証用の`dev`・`search`も含む。LinuxのPyTorchはCUDAを含まない公式CPU wheelを使用する。現在のmacOSでは通常の2.9.1 wheelを使用し、DoclingはCPUで動作する。

| ファイル | 役割 |
| --- | --- |
| [.python-version](../../.python-version) | Python管理ツール向けの3.14.4指定 |
| [pyproject.toml](../../pyproject.toml) | 対応Python範囲、直接依存、構築用setuptoolsの固定 |
| [ubuntu-26.04-baseline.json](../../requirements/ubuntu-26.04-baseline.json) | Ubuntuとの全対応表・例外・公式索引の出典 |
| [ubuntu-26.04-constraints.txt](../../requirements/ubuntu-26.04-constraints.txt) | Ubuntu標準版と互換例外の解決制約 |
| [ubuntu-26.04-tools.in](../../requirements/ubuntu-26.04-tools.in) | pip・setuptoolsの導入版 |
| [ubuntu-26.04.lock](../../requirements/ubuntu-26.04.lock) | 間接依存を含む固定一覧。OSごとのmarkerを含む |
| [check_python_environment.py](../../scripts/check_python_environment.py) | 実行Pythonと、そのOSで必要な全固定依存の版を照合する |

lockを更新する場合の構築ツールはuv 0.13.5を使用した。

```sh
uv pip compile pyproject.toml requirements/ubuntu-26.04-tools.in \
  --extra dev --extra search \
  --constraint requirements/ubuntu-26.04-constraints.txt \
  --universal --python-version 3.14 --torch-backend cpu \
  --no-annotate --no-header --output-file requirements/ubuntu-26.04.lock
```

生成後、pip用に`--index-url https://pypi.org/simple`と`--extra-index-url https://download.pytorch.org/whl/cpu`をlockの先頭へ記録する。Ubuntuの提供版・対応表も更新し、実モデルを含めて再検証する。

## 検証結果

macOS arm64上に、Python 3.14.4の独立した仮想環境を新規作成した。既存環境のsystem-site-packagesは継承していない。

- 固定依存97件：版の不一致0件。`pip check`：成功。
- 全179テスト：成功、スキップ0件。
- Doclingの実モデル：公開PDF2件の本文・bbox、表PDFの6列のセル構造、画像保持、OCR待ち状態を確認。
- 公開TXT・DOCX・XLSX・PDFの各2件：実PostgreSQLへの登録と、再実行時の8件再利用を確認。
- Embedding APIのモック試験：認証失敗・再試行・分割・キャッシュを確認。実OpenAIキーによる外部API呼び出しは行っていない。
- 検索API、PGroonga、SQLスキーマの一致、Ruff、固定setuptoolsでの配布wheel作成とモデルlock同梱：成功。

DBは既存のPostgreSQL 18.6＋pgvector 0.8.6＋PGroonga 4.1.0を使用。並行作業との取込ロック競合を避けるため、今回の実行では専用DB`file_scan_test_ubuntu2604`を使用した。通常のテストDBは変更していない。

実行ログはローカルの`artifacts/ubuntu2604/pytest.log`、版照合は`artifacts/ubuntu2604/environment-check.json`へ保存した。Ubuntu OSでの実行結果やUbuntu独自パッチのバイナリ検証へ読み替えない。
