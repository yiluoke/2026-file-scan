"""固定版CodeWikiを実行する、ファイル入出力だけのアダプター。

このファイルは単独スクリプトとして起動する。親側のfile-scan依存をimportしない。
秘密は環境変数からメモリーへ読み、CLI引数・設定ファイルへ書き出さない。
"""

from __future__ import annotations

import importlib.metadata
import json
import os
import subprocess
import sys
from pathlib import Path


def identity():
    distribution = importlib.metadata.distribution("codewiki")
    direct = json.loads(distribution.read_text("direct_url.json") or "{}")
    commit = direct.get("vcs_info", {}).get("commit_id")
    if not commit and direct.get("dir_info", {}).get("editable"):
        import codewiki
        repo = Path(codewiki.__file__).resolve().parent.parent
        result = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                                capture_output=True, text=True, timeout=10, check=True)
        commit = result.stdout.strip()
        dirty = subprocess.run(["git", "-C", str(repo), "status", "--porcelain",
                                "--untracked-files=no"], capture_output=True,
                               text=True, timeout=10, check=True)
        if dirty.stdout.strip():
            raise RuntimeError("codewiki_checkout_modified")
    return {"version": distribution.version, "commit": commit}


def main(request):
    info = identity()
    if info != {"version": request["expected_version"], "commit": request["expected_commit"]}:
        raise RuntimeError("codewiki_version_mismatch")
    if request["operation"] == "probe":
        return info
    # 選別済み作業コピーを対象とし、拡張子の違いで補助C++ソースを黙って落とさない。
    from codewiki.src.be.dependency_analyzer.utils import patterns
    from codewiki.src.be.dependency_analyzer.analyzers.cpp import TreeSitterCppAnalyzer
    diagnostics = []
    original_parse = TreeSitterCppAnalyzer._parse_with_macro_recovery
    def parse_with_diagnostics(analyzer, parser):
        root = original_parse(analyzer, parser)
        errors = analyzer._count_parse_errors(root)
        if errors:
            path = Path(analyzer.file_path).relative_to(request["repo"]).as_posix()
            diagnostics.append({"path": path, "code": "cpp_syntax_partial",
                                "error_count": errors, "coverage": "partial",
                                "method": "codewiki_macro_recovery"})
        return root
    TreeSitterCppAnalyzer._parse_with_macro_recovery = parse_with_diagnostics
    for extension in request["cpp_extensions"]:
        patterns.CODE_EXTENSIONS[extension] = "cpp"
    includes = ["*" + "".join(f"[{c.lower()}{c.upper()}]" if c.isalpha() else c for c in extension)
                for extension in [*request["cpp_extensions"], ".js"]]
    if request["operation"] == "analyze":
        from codewiki.src.config import Config
        from codewiki.src.be.dependency_analyzer import DependencyGraphBuilder
        config = Config(repo_path=request["repo"], output_dir=request["output"],
                        dependency_graph_dir=str(Path(request["output"]) / "graphs"),
                        docs_dir=request["output"], max_depth=2, llm_base_url="unused",
                        llm_api_key="unused", main_model="unused", cluster_model="unused",
                        use_gitignore=False, artifacts_enabled=False,
                        agent_instructions={"include_patterns": includes})
        builder = DependencyGraphBuilder(config)
        components, leaves = builder.build_dependency_graph()
        nodes = []
        for node in components.values():
            data = node.model_dump(mode="json")
            data.pop("source_code", None)
            data.pop("file_path", None)
            data["depends_on"] = sorted(data.get("depends_on", []))
            nodes.append(data)
        return {"identity": info, "components": sorted(nodes, key=lambda n: n["id"]),
                "leaf_nodes": leaves, "syntax_diagnostics": diagnostics}
    from codewiki.cli.adapters.doc_generator import CLIDocumentationGenerator
    doc = request["documentation"]
    model, key, base = [os.environ.get(doc[name], "")
                        for name in ("model_env", "api_key_env", "base_url_env")]
    if not all((model, key, base)):
        raise RuntimeError("llm_environment_missing")
    config = {"provider": "openai-compatible", "base_url": base, "api_key": key,
              "main_model": model, "cluster_model": model, "fallback_model": model,
              "max_tokens": request["limits"]["max_tokens"],
              "request_limit": request["limits"]["request_limit"],
              "max_depth": request["limits"]["max_depth"], "agent_retries": 1,
              "use_gitignore": False, "artifacts_enabled": False, "layout": "flat",
              "agent_instructions": {"include_patterns": includes, "language": "ja",
                                     "doc_type": "developer", "custom_instructions": (
                  "日本語の実装仕様書を生成してください。識別子とパスは原文のまま保持。"
                  "根拠はソース相対パスと行番号を `main.cpp:12-18` の形式で示してください。"
                  "実装上の記述と推定を区別。"
                  "Qtの実行時接続やプラグイン依存、業務要件・設計意図を断定しない。"
                  "qmake設定は参考情報で、条件評価・コマンド実行はしない。"
                  "Mermaid図は生成せず、文章と表で記述。日時・実行IDを見出しに含めない。")}}
    job = CLIDocumentationGenerator(Path(request["repo"]), Path(request["output"]), config,
                                    verbose=False, generate_html=False).generate()
    status = getattr(job.status, "value", job.status)
    if status != "completed":
        raise RuntimeError("codewiki_generation_incomplete")
    return {"identity": info, "status": "succeeded",
            "usage": job.statistics.usage_totals, "syntax_diagnostics": diagnostics}


if __name__ == "__main__":
    # CodeWikiのdotenv自動読込、外部Mermaid検証、任意テレメトリーを抑止する。
    os.environ["PYTHON_DOTENV_DISABLED"] = "1"
    os.environ["MERMAID_VALIDATE"] = "0"
    os.environ["OTEL_SDK_DISABLED"] = "true"
    request_path, response_path = map(Path, sys.argv[1:])
    try:
        result = main(json.loads(request_path.read_text(encoding="utf-8")))
        response_path.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    except Exception as exc:
        # 例外メッセージにソース本文やAPIキーが含まれる可能性がある。
        known = {"codewiki_version_mismatch", "llm_environment_missing",
                 "codewiki_checkout_modified", "codewiki_generation_incomplete"}
        code = str(exc) if isinstance(exc, RuntimeError) and str(exc) in known else type(exc).__name__
        response_path.write_text(json.dumps({"error": code}), encoding="utf-8")
        sys.exit(1)
