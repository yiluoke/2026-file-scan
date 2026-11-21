# ファイル取込を文書形式に限定

更新日：2026-11-01。プログラム解析を別処理として作成する方針に合わせ、既存のファイル取込からプログラム解析と汎用データ・設定・ログの取込を削除した。

## 対象と対象外

| 区分 | 形式・扱い |
| --- | --- |
| 取込対象 | Word（`.docx`）、Excel（`.xlsx` / `.xlsm`）、PDF（`.pdf`）、TXT（`.txt`）、Markdown（`.md`、明示設定で`.markdown`） |
| 対象外 | プログラム原本、SQL、JSON、YAML、YML、CSV、LOG。既定設定で収集せず、パーサーに直接渡しても拒否する |
| 引き続き使用 | アプリのYAML設定、確認用のJSON/JSONL/Markdown出力、DB初期化・問合せのSQL、アプリ自身のPython実装 |

文書本文に記載されたコード片・識別子は原文として保持する。プログラムの構造・関数・クラス・呼出関係を解析する処理は行わない。

## 実装変更

- `src/file_scan/parsers.py`：Python ASTパーサーと専用の文字コード宣言処理を削除。SQL・JSON・YAML・YML・CSV・LOGのテキスト抽出分岐も削除した。
- `src/file_scan/config.py`：既定の対象拡張子を文書形式に限定。`include_extensions`は対応文書形式の部分集合だけを受け付ける。YAMLからの読込と直接生成したSettingsの両方で検査する。
- `src/file_scan/chunking.py`：Pythonの定義名を識別子へ引き継ぐ処理を削除。文書のチャンク化・原本対応・トークン上限は維持する。
- 同梱設定2件とローカル設定から`.py`を削除。旧プログラム原本サンプルも削除した。
- 対応形式表・取込設計・検索側の責務記述・図解資料を更新。DBスキーマの破壊的変更は行わない。

## 既存環境への適用

既存の設定ファイルに`.py`・`.sql`・`.json`・`.yaml`・`.yml`・`.csv`・`.log`等が残っている場合は、`include_extensions`から削除する。残したまま実行すると設定エラーになる。

```yaml
include_extensions: ['.txt', '.md', '.xlsx', '.xlsm', '.docx', '.pdf']
```

設定を直してから、既存のDB・保管先を使って再取込する。

```sh
file-scan ingest --config config.local.yaml
file-scan status --config config.local.yaml
```

正常に列挙できた収集元で対象外原本を検出した場合は、既存の文書所属を`included=false`・`collection_status=excluded`へ更新する。過去の原本・抽出世代・ベクトルは履歴として保持し、物理削除しない。単にプログラムを更新しただけでは既存DBの所属は切り替わらないので、設定変更後の`ingest`が必要である。

パーサーとチャンク化の実装ハッシュが変わるため、対応する文書も新しい変換世代になる。ベクトル生成が必要な環境では、その後`embed`を実行する。同じモデル・次元・入力版・`search_text`の既存ベクトルは再利用できる。検索側に公開済みの場合は、通常の原本・世代の照合により更新待ちとなるため、内容を確認してから必要な文書を再公開する。

今回の作業では実際の業務データ再取込・DB変更・外部API呼出・公開は実行していない。

## 検証

- 対象外の形式を設定から有効化できないこと、直接パーサーへ渡しても内容を読まずに拒否することを検証した。
- 文書6拡張子の収集と、対象外7拡張子の除外理由を検証した。`.markdown`の明示設定も確認した。
- Word・Excel・PDF・Markdownが同時に登録され、同じフォルダのプログラムは除外されることを確認した。
- 全体テスト：**160件成功、46件スキップ**。スキップは実PostgreSQL接続を必要とするテスト。Docling実モデルを使うPDF抽出も成功した。既存依存ライブラリの非推奨警告5件がある。
- `ruff check src tests`と`git diff --check`は成功した。

テストは配置済み辞書を一時的なtiktokenキャッシュへ読み込ませて実行した。実OpenAI APIと実PostgreSQLの接続・保存の再検証は今回実施していない。

```sh
TIKTOKEN_CACHE_DIR=/private/tmp/file-scan-guide-token-cache .venv/bin/python -m pytest -q --tb=short
.venv/bin/ruff check src tests
git diff --check
```

上記キャッシュパスは今回の検証環境用。他環境では配置済みのtiktokenキャッシュへ置き換える。

図解資料は[説明資料の一覧](../explanation/README.md)を参照。
