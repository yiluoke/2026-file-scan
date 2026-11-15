"""専用CLI。LLM生成・文書取込・Embedding・検索公開を明示的に分ける。"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__
from .config import ConfigError, load_config
from .pipeline import analyze, generate, scan, status
from .store import ScanError


ERROR_HINTS = {
    "codewiki_not_installed": "cpp-scanを実行するPython環境に固定版CodeWikiを導入してください。",
    "ModuleNotFoundError": "統合lockの依存関係をCodeWikiの実行環境に導入してください。",
    "codewiki_tokenizer_download_failed": "トークナイザー辞書を取得できません。準備済みの辞書がある"
                                        "フォルダをTIKTOKEN_CACHE_DIRに指定してください。",
    "codewiki_tokenizer_cache_invalid": "トークナイザー辞書を初期化できません。"
                                       "TIKTOKEN_CACHE_DIRのパス・権限・辞書を確認してください。",
    "codewiki_llm_tls_failed": "LLMへのHTTPS接続でTLSエラーが発生しました。"
                               "社内CAバンドルとSSL_CERT_FILEを確認してください。",
    "codewiki_llm_connection_failed": "LLMに接続できません。ベースURLとネットワーク経路を確認してください。",
    "codewiki_llm_proxy_failed": "プロキシ経由でLLMに接続できません。プロキシのURL・認証・接続先の許可を確認してください。",
    "codewiki_llm_dns_failed": "LLM接続先またはプロキシの名前解決に失敗しました。ホスト名とDNS設定を確認してください。",
    "codewiki_llm_timeout": "LLMへのリクエストがタイムアウトしました。",
    "codewiki_llm_http_400": "APIがリクエストを受け付けませんでした。モデルとAPI方式を確認してください。",
    "codewiki_llm_http_401": "API認証に失敗しました。仕様書生成用のAPIキーを確認してください。",
    "codewiki_llm_http_403": "APIへのアクセスが拒否されました。プロジェクトの権限を確認してください。",
    "codewiki_llm_http_404": "APIがモデルまたはエンドポイントを見つけられません。モデルIDとベースURLを確認してください。",
    "codewiki_llm_http_429": "APIの利用上限に達しました。レート制限と利用枠を確認してください。",
    "codewiki_llm_quota_exhausted": "APIの利用枠が不足しています。プロジェクトの請求設定と利用枠を確認してください。",
}


def _parser():
    parser = argparse.ArgumentParser(description="C++／Qt 5のソース解析・日本語仕様書生成・下書き取込")
    parser.add_argument("--version", action="version", version=__version__)
    commands = parser.add_subparsers(dest="command", required=True)
    descriptions = {"scan": "対象・除外・Qt画面方式の棚卸し（LLM・DBなし）",
                    "analyze": "原本固定・CodeWiki構造解析・Qt補完（LLM・DBなし）",
                    "generate": "指定した解析版から日本語仕様書を生成（LLMへ送信）",
                    "ingest": "完成した仕様書を既存file-scanで下書き登録（Embeddingなし）",
                    "status": "工程の結果・失敗・選択世代・取込履歴（DB接続なし）"}
    for name, description in descriptions.items():
        command = commands.add_parser(name, help=description)
        command.add_argument("--config", required=True, type=Path)
        if name == "analyze":
            command.add_argument("--snapshot", help="保存済みsnapshot IDを再解析")
        if name == "generate":
            command.add_argument("--analysis", required=True, help="解析ID")
        if name in {"analyze", "generate"}:
            command.add_argument("--force", action="store_true", help="完成済みの同一条件の成果物を再利用しない")
        if name == "ingest":
            command.add_argument("--generation", required=True, help="生成ID")
            command.add_argument("--retry-partial", action="store_true")
            command.add_argument("--accept-scope-change", action="store_true",
                                 help="入力元・対象範囲の変更や既存文書の論理削除を確認して反映")
    return parser


def main(argv=None):
    args = _parser().parse_args(argv)
    try:
        settings = load_config(args.config)
        if args.command == "scan":
            result = scan(settings)
        elif args.command == "analyze":
            result = analyze(settings, snapshot_id=args.snapshot, force=args.force)
            result = {k: result[k] for k in ("analysis_id", "snapshot_id", "status", "reused")}
        elif args.command == "generate":
            result = generate(settings, args.analysis, force=args.force)
            result = {k: result[k] for k in ("generation_id", "analysis_id", "snapshot_id", "status", "reused")}
        elif args.command == "ingest":
            from .handoff import ingest
            result = ingest(settings, args.generation, retry_partial=args.retry_partial,
                            accept_scope_change=args.accept_scope_change)
            result = {k: result[k] for k in ("generation_id", "status", "summary", "knowledge_id")}
        else:
            result = status(settings)
        print(json.dumps(result, ensure_ascii=False))
        return 2 if result.get("status") == "partial" else 0
    except (ConfigError, ScanError) as exc:
        print(f"エラー: {exc}", file=sys.stderr)
        if hint := ERROR_HINTS.get(str(exc)):
            print(hint, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("処理を中断しました。完成した旧世代は保持されています。", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"実行に失敗しました ({type(exc).__name__})。設定・依存・状態レポートを確認してください。",
              file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
