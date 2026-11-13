"""日本語仕様書の整形と根拠・内部リンク・出力ハッシュの検証。"""

from __future__ import annotations

import posixpath
import re
from collections import defaultdict
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlsplit

from .collector import CONFIG_EXTENSIONS, kind_for
from .markdown import PARSER, destination, heading_anchors, links, rewrite_links, unclosed_fence
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


# 生成文書の原本参照は、相対パスと行番号（例 main.cpp:12-18）で扱う。
_SOURCE_LOCATION = re.compile(
    r"(?<![\w./%:=-])(?P<path>[\w./%+@-]+):"
    r"L?(?P<first>[0-9]{1,18})(?:-L?(?P<last>[0-9]{1,18}))?(?![0-9])"
)
_INLINE_LOCATION = re.compile(r"(?P<path>[^:\n]+):L?(?P<first>[0-9]{1,18})(?:-L?(?P<last>[0-9]{1,18}))?")


def _source_path(path):
    return kind_for(path) is not None or PurePosixPath(path).suffix.lower() in CONFIG_EXTENSIONS


def _location(entries, path, first, last):
    source = entries.get(path)
    if source is None:
        raise ScanError("codewiki_source_reference_missing")
    if not 1 <= first <= last <= source.get("line_count", 0):
        raise ScanError("codewiki_source_reference_out_of_range")
    return {"path": path, "sha256": source["sha256"], "start_line": first, "end_line": last}


def _text_locations(tokens, entries):
    for block in tokens:
        if block.type != "inline":
            continue
        # リンク先は別に検証する。インラインコードで示したファイル:行も根拠として扱う。
        for token in block.children or []:
            if token.type not in {"text", "code_inline"}:
                continue
            if token.type == "code_inline":
                whole = _INLINE_LOCATION.fullmatch(token.content)
                matches = [whole] if whole else []
            else:
                matches = _SOURCE_LOCATION.finditer(token.content)
            for match in matches:
                path = unquote(match["path"]).removeprefix("./")
                if path not in entries and not _source_path(path):
                    continue
                first = int(match["first"])
                yield path, first, int(match["last"] or first)


def _link_location(entries, target, origin):
    """原本リンクの候補は、文書の隣とスナップショット直下から探す。"""
    path = unquote(target.path)
    candidates = (posixpath.normpath((PurePosixPath(origin).parent / path).as_posix()),
                  path.removeprefix("./"), path.removeprefix("../"))
    original = next((name for name in candidates if name in entries), None)
    if original is None:
        if _source_path(path) and kind_for(path) != "reference":
            raise ScanError("codewiki_source_reference_missing")
        return None
    fragment = unquote(target.fragment)
    lines = re.fullmatch(r"L([0-9]{1,18})(?:-L?([0-9]{1,18}))?", fragment)
    if fragment and not lines:
        raise ScanError("codewiki_source_reference_invalid_fragment")
    first = int(lines[1]) if lines else 1
    last = int(lines[2] or first) if lines else entries[original].get("line_count", 0)
    return _location(entries, original, first, last)


def validate_markdown(root: Path, snapshot: dict | None = None) -> list[dict]:
    root = root.resolve()
    errors = []
    files = file_manifest(root)
    names = {item["path"] for item in files}
    if not {"overview.md", "plugins.md", "screens.md", "processes.md",
            "api.md", "unknowns.md"}.issubset(names):
        errors.append({"code": "required_documents_missing"})
    parsed_documents = {}
    entries = {entry["path"]: entry for entry in snapshot["files"]} if snapshot is not None else None
    for name in sorted(names):
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
        tokens = PARSER.parse(text)
        parsed_documents[name] = tokens, heading_anchors(tokens)
        lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
        if any(token.type == "fence" and unclosed_fence(lines, token) for token in tokens):
            errors.append({"path": name, "code": "unclosed_code_fence"})
        if entries is not None:
            for path, first, last in _text_locations(tokens, entries):
                try:
                    _location(entries, path, first, last)
                except ScanError as exc:
                    errors.append({"path": name, "code": str(exc), "target": f"{path}:{first}-{last}"})
    for name, (tokens, _) in parsed_documents.items():
        for token in links(tokens):
            url = destination(token)
            target = urlsplit(url)
            if target.scheme in {"http", "https", "mailto"} or (not target.scheme and target.netloc):
                continue
            path = (root / PurePosixPath(name).parent / unquote(target.path)).resolve() if target.path else root / name
            if target.scheme or not path.is_relative_to(root):
                errors.append({"path": name, "code": "invalid_internal_link", "target": url})
                continue
            linked = parsed_documents.get(path.relative_to(root).as_posix())
            if linked is None or (target.fragment and unquote(target.fragment) not in linked[1]):
                errors.append({"path": name, "code": "invalid_internal_link", "target": url})
    return errors


def format_documents(settings, snapshot, analysis, codewiki_root: Path, output: Path):
    qt, components = analysis["qt"], analysis["components"]
    header = (f"> 生成文書・未レビュー。project: `{settings.project_id}`。\n"
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
        def replace_link(token):
            parsed = urlsplit(destination(token))
            if parsed.scheme or parsed.netloc or not parsed.path:
                return None
            location = (path.parent / unquote(parsed.path)).resolve()
            if location.is_relative_to(codewiki_root.resolve()):
                original = location.relative_to(codewiki_root).as_posix()
                if original in mapping:
                    href = posixpath.relpath(mapping[original], PurePosixPath(mapping[relative]).parent)
                    if parsed.fragment:
                        href += "#" + parsed.fragment
                    label = token.meta["label"].replace("\n", " ")
                    return f"{label}({href})"
            source = _link_location(source_entries, parsed, relative)
            if source:
                return f"`{source['path']}:{source['start_line']}-{source['end_line']}`"
            return None

        text = rewrite_links(text, replace_link)
        for original, first, last in dict.fromkeys(_text_locations(PARSER.parse(text), source_entries)):
            source_references.append({"document": mapping[relative],
                                      **_location(source_entries, original, first, last),
                                      "semantic_status": "unreviewed"})
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


def validate_generation(folder: Path, manifest: dict, snapshot: dict) -> None:
    if manifest["status"] not in {"succeeded", "partial"}:
        raise ScanError("generation_not_complete")
    verify_files(folder / "documents", manifest["document_files"])
    errors = validate_markdown(folder / "documents", snapshot)
    if errors:
        raise ScanError("generation_document_validation_failed")
