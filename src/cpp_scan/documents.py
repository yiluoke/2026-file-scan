"""日本語仕様書の整形と根拠・内部リンク・出力ハッシュの検証。"""

from __future__ import annotations

import os
import re
from collections import defaultdict
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlsplit

from .store import (ScanError, digest, file_manifest, read_bytes, verify_files, write_bytes)

VERSION = "japanese-spec-v1"


def _cell(value) -> str:
    # lambdaの[this](...)等をMarkdownリンクと誤読・検査しないようエスケープする。
    return (str(value if value is not None else "").replace("&", "&amp;")
            .replace("<", "&lt;").replace(">", "&gt;").replace("|", "&#124;")
            .replace("[", "&#91;").replace("]", "&#93;").replace("\n", " "))


def _reference(record) -> str:
    evidence = record["evidence"]
    return f"`{evidence['path']}:{evidence['start_line']}-{evidence['end_line']}`"


def _table(records) -> str:
    rows = ["| 種別 | 抽出した記述・候補 | 根拠 | 確実性 |", "| --- | --- | --- | --- |"]
    for record in records:
        rows.append(f"| {_cell(record['kind'])} | {_cell(record['data'])} | "
                    f"{_reference(record)} | {record['certainty']}／未レビュー |")
    return "\n".join(rows) + "\n"


def validate_evidence(snapshot, qt, components) -> list[dict]:
    entries = {entry["path"]: entry for entry in snapshot["files"]}
    errors = []
    for record in [*qt["records"], *components]:
        e = record.get("evidence")
        if not e:
            errors.append({"id": record["id"], "code": "missing_evidence"})
            continue
        source = entries.get(e["path"])
        if (not source or e["sha256"] != source["sha256"]
                or not 1 <= e["start_line"] <= e["end_line"] <= source.get("line_count", 0)
                or not 0 <= e["byte_start"] < e["byte_end"] <= source["size"]):
            errors.append({"id": record["id"], "code": "invalid_source_reference"})
    return errors


def validate_markdown(root: Path) -> list[dict]:
    errors = []
    files = file_manifest(root)
    names = {item["path"] for item in files}
    if not names or not {"overview.md", "plugins.md", "screens.md", "processes.md",
                         "api.md", "unknowns.md"}.issubset(names):
        errors.append({"code": "required_documents_missing"})
    for item in files:
        name = item["path"]
        if not name.endswith(".md"):
            errors.append({"path": name, "code": "non_markdown_document"})
            continue
        try:
            text = read_bytes(root / name).decode("utf-8")
        except UnicodeError:
            errors.append({"path": name, "code": "invalid_utf8_document"})
            continue
        if not text.strip() or "生成文書" not in text or "未レビュー" not in text:
            errors.append({"path": name, "code": "missing_document_provenance"})
        # フェンス内のコードはリンク検査から除く。未閉鎖フェンスは文書不備。
        fence = None
        prose = []
        for line in text.splitlines():
            marker = re.match(r"^\s{0,3}(`{3,}|~{3,})", line)
            if marker:
                if fence is None:
                    fence = marker.group(1)
                elif marker.group(1)[0] == fence[0] and len(marker.group(1)) >= len(fence):
                    fence = None
                continue
            if fence is None:
                prose.append(line)
        if fence:
            errors.append({"path": name, "code": "unclosed_code_fence"})
        for match in re.finditer(r"!?\[[^\]]*\]\(([^)]+)\)", "\n".join(prose)):
            link = match.group(1).split(' "', 1)[0].strip("<>")
            parsed = urlsplit(link)
            if parsed.scheme in {"http", "https", "mailto"}:
                continue
            if not parsed.path:
                continue
            target = (root / PurePosixPath(name).parent / unquote(parsed.path)).resolve()
            if parsed.scheme or not target.is_relative_to(root.resolve()) or not target.is_file():
                errors.append({"path": name, "code": "invalid_internal_link", "target": link})
    return errors


def format_documents(settings, snapshot, analysis, codewiki_root: Path, output: Path):
    qt, components = analysis["qt"], analysis["components"]
    header = (f"> 生成文書・未レビュー。project: `{settings.project_id}`、"
              f"snapshot: `{snapshot['snapshot_id']}`。解析状態: `{analysis['status']}`。\n"
              "> ソースの記述と動作候補を整理した実装仕様です。実行時の成立と業務要件は未確認です。\n\n")

    def page(name, title, body):
        write_bytes(output / name, (f"# {title}\n\n" + header + body).encode("utf-8"))

    records = qt["records"]
    plugins = [r for r in records if r["kind"] in {"plugin", "plugin_interface", "plugin_loader"}]
    screens = [r for r in records if r["kind"] in {"screen", "widget", "ui_connection", "auto_connection",
                                                 "qml_declaration", "qml_cpp_binding", "qml_module"}]
    processes = [r for r in records if r["kind"] in {"connection", "ui_binding", "settings_reference", "qt_call"}]
    page("plugins.md", "プラグイン仕様", "IIDはインターフェース識別子です。所属・依存・有効化・初期化順は"
         "記述とローダーを人手で照合してください。\n\n" + _table(plugins))
    page("screens.md", "画面仕様", "WidgetsのXMLとQMLの字句情報を示します。操作から実装への対応、"
         "自動接続、画面遷移は候補です。\n\n" + _table(screens))
    page("processes.md", "処理と起動契機", "接続式の存在と、実行時に接続が成立することを区別します。"
         "業務処理の入力・手順・分岐・出力・エラーの説明はCodeWiki資料と併せて確認してください。\n\n"
         + _table(processes))
    api = ["| 種別 | 識別子 | 根拠 |", "| --- | --- | --- |"]
    for component in components:
        api.append(f"| {_cell(component['kind'])} | {_cell(component['name'])} | {_reference(component)} |")
    page("api.md", "API・構造一覧", "CodeWikiの構文抽出結果です。型や呼出先の意味解析は行っていません。\n\n"
         + "\n".join(api) + "\n\n" + _table([r for r in records if r["kind"] in
                                                       {"meta_object", "property", "signal", "slot"}]))
    warnings = ["- " + line for line in qt["limitations"]]
    warnings += [f"- `{_cell(e.get('path', '.'))}`: `{e['code']}`" for e in
                 [*snapshot["errors"], *qt["errors"], *analysis["errors"]]]
    warnings += [f"- `{_cell(c['path'])}`: 補完の対応範囲 `{c['status']}`。"
                 for c in qt["coverage"] if c["status"] in {"unparsed", "lexical_partial"}]
    warnings += [f"- `{_cell(d['path'])}`: C++構文の一部を解析できません。"
                 f"マクロ回復後のエラー候補数: {d['error_count']}。部分抽出として確認してください。"
                 for d in analysis.get("syntax_diagnostics", [])]
    warnings += ["- CodeWikiの説明とQt抽出結果の意味的な矛盾は自動確定できません。根拠を照合してレビューしてください。",
                 "- CodeWikiの本文は推定を含みます。根拠行との自動対応がない記述は未確認です。",
                 "- Mermaidの外部検証は無効です。図が含まれる場合は構文未検証として扱います。"]
    page("unknowns.md", "未確認事項と解析範囲", "\n".join(warnings) + "\n")
    # CodeWikiのページを、所有ファイル集合に基づくIDへ対応付ける。
    # 構成が変わって所有集合が変わった場合は改名としてmanifestへ残す。
    owners = {}
    tree_path = codewiki_root / "module_tree.json"
    if tree_path.is_file():
        from .store import read_json
        def visit(tree):
            for name, node in tree.items():
                files = sorted({str(c).split("::")[0] for c in node.get("components", [])})
                owners[name] = files
                visit(node.get("children", {}))
        visit(read_json(tree_path))
    mapping, seen, source_references = {}, set(), []
    source_entries = {e["path"]: e for e in snapshot["files"]}
    source_pages = sorted(codewiki_root.rglob("*.md"))
    if not source_pages or not (codewiki_root / "overview.md").is_file():
        raise ScanError("codewiki_overview_missing")
    for path in source_pages:
        relative = path.relative_to(codewiki_root).as_posix()
        identity = ["files", owners[path.stem]] if owners.get(path.stem) else ["path", relative]
        name = "wiki/" + ("overview" if relative == "overview.md" else digest(identity)[:24]) + ".md"
        if name in seen:
            name = "wiki/" + digest([identity, relative])[:24] + ".md"
        seen.add(name)
        mapping[relative] = name
    for path in source_pages:
        relative = path.relative_to(codewiki_root).as_posix()
        text = read_bytes(path).decode("utf-8")
        def link(match):
            target = match.group(2)
            parsed = urlsplit(target)
            if parsed.scheme or not parsed.path:
                return match.group()
            location = (path.parent / unquote(parsed.path)).resolve()
            if location.is_relative_to(codewiki_root.resolve()):
                original = location.relative_to(codewiki_root).as_posix()
                if original in mapping:
                    new = os.path.relpath(mapping[original], PurePosixPath(mapping[relative]).parent)
                    return match.group(1) + new.replace(os.sep, "/") + ("#" + parsed.fragment if parsed.fragment else "") + ")"
            # CodeWikiのソースリンクは配置先が別なので、原本参照へ変換する。
            # 明示的な相対パスと行を照合できない場合は検証失敗として残す。
            source_name = unquote(parsed.path).replace("\\", "/")
            alternatives = [source_name, source_name.removeprefix("../"), source_name.removeprefix("./")]
            original = next((p for p in alternatives if p in source_entries), None)
            if original:
                source = source_entries[original]
                lines = re.fullmatch(r"L(\d+)(?:-L?(\d+))?", parsed.fragment)
                if parsed.fragment and not lines:
                    raise ScanError("codewiki_source_reference_invalid_fragment")
                first = int(lines.group(1)) if lines else 1
                last = int(lines.group(2) or first) if lines else source.get("line_count", 0)
                if not 1 <= first <= last <= source.get("line_count", 0):
                    raise ScanError("codewiki_source_reference_out_of_range")
                source_references.append({"document": mapping[relative], "path": original,
                                          "sha256": source["sha256"], "start_line": first,
                                          "end_line": last, "semantic_status": "unreviewed"})
                return f"`{original}:{first}-{last}`"
            return match.group()
        text = re.sub(r"(!?\[[^\]]*\]\()([^)]+)\)", link, text)
        write_bytes(output / mapping[relative], (header + "> 本ページはCodeWikiが生成した説明（推定を含む）です。"
                    "根拠行との説明の一致は未確認です。\n\n" + text).encode("utf-8"))
    # Qt要素はファイル単位の安定IDで、詳細資料を分ける。
    grouped = defaultdict(list)
    for record in [*plugins, *[r for r in screens if r["kind"] != "widget"]]:
        grouped[record["evidence"]["path"]].append(record)
    for path, group in sorted(grouped.items()):
        page("qt/" + digest(path)[:24] + ".md", f"Qt補完仕様：{path}", _table(group))
    links = ["- [プラグイン仕様](plugins.md)", "- [画面仕様](screens.md)",
             "- [処理仕様](processes.md)", "- [API・構造一覧](api.md)", "- [未確認事項](unknowns.md)"]
    links += [f"- [CodeWiki：{_cell(old)}]({new})" for old, new in sorted(mapping.items())]
    source_rows = ["| ファイル | 区分 | 文字コード |", "| --- | --- | --- |"]
    source_rows += [f"| `{_cell(e['path'])}` | {e['kind']} | {e.get('encoding', '未解読')} |"
                    for e in snapshot["files"]]
    page("overview.md", "全体構成と仕様書一覧", "## 対象\n\nQt 5／qmakeのソースです。"
         ".proの詳細評価とビルドは実行していません。通常フォルダの二度の照合で固定したコピーを使い、"
         "ファイル横断で同時点の版であることは保証しません。\n\n"
         + "\n".join(links) + "\n\n## 入力一覧\n\n" + "\n".join(source_rows) + "\n")
    return {"codewiki_page_mapping": mapping, "codewiki_source_references": source_references,
            "document_files": file_manifest(output)}


def validate_generation(folder: Path, manifest: dict) -> None:
    if manifest["status"] not in {"succeeded", "partial"}:
        raise ScanError("generation_not_complete")
    verify_files(folder / "documents", manifest["document_files"])
    errors = validate_markdown(folder / "documents")
    if errors:
        raise ScanError("generation_document_validation_failed")
