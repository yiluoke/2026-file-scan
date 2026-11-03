"""Qt固有記述の補完。字句・XML上の事実と、実行時の対応候補を分けて保存する。"""

from __future__ import annotations

import bisect
import json
import re
from pathlib import Path
from xml.etree import ElementTree as ET

from .store import ScanError, digest, read_bytes, read_json, version_path
from .text import arguments, calls, masked

VERSION = "qt-lexical-xml-v1"
LIMITATIONS = [
    "qmake・条件付きコンパイルの有効性、型、オーバーロード、実行時の接続成立は未確認。",
    "QMLは字句ベースの補完。動的binding、JavaScriptの評価、全構文の検証は対象外。",
    "Factory・独自マクロ・動的パスは候補として保持し、完全なプラグイン所属は確定しない。",
]


class Extractor:
    def __init__(self, settings, snapshot):
        self.settings, self.snapshot = settings, snapshot
        self.root = version_path(settings.artifact_root, "snapshots", snapshot["snapshot_id"])
        self.records, self.errors, self.coverage = [], [], []

    def add(self, entry, text, kind, start, end, data, certainty="source_fact"):
        start, end = max(0, start), min(len(text), end)
        if end <= start:
            return
        line_starts = [0, *[m.end() for m in re.finditer("\n", text)]]
        first, last = bisect.bisect_right(line_starts, start), bisect.bisect_right(line_starts, end - 1)
        encoding = entry["encoding"]
        mapping = read_json(self.root / "maps" / (entry["path"] + ".json"))
        # BOMを含む原本の開始位置は保存済み対応表から求める。
        bom = mapping["lines"][0]["source_start"] if mapping["lines"] else 0
        evidence = {"path": entry["path"], "sha256": entry["sha256"], "encoding": encoding,
                    "start_line": first, "end_line": last,
                    "byte_start": bom + len(text[:start].encode(encoding)),
                    "byte_end": bom + len(text[:end].encode(encoding))}
        identifier = digest([entry["path"], kind, start, end, data])
        before = text[:start].splitlines()
        conditions = []
        for line in before:
            if re.match(r"\s*#\s*(if|ifdef|ifndef)\b", line):
                conditions.append(line.strip())
            elif re.match(r"\s*#\s*endif\b", line) and conditions:
                conditions.pop()
            elif re.match(r"\s*#\s*(else|elif)\b", line) and conditions:
                conditions[-1] += " / " + line.strip()
        self.records.append({"id": identifier, "kind": kind, "data": data,
                             "certainty": certainty, "review": "unreviewed",
                             "evidence": evidence, "conditions": conditions,
                             "method": VERSION})

    def cpp(self, entry, text):
        code = masked(text)
        for match in re.finditer(r"\b(Q_OBJECT|Q_GADGET|Q_INVOKABLE)\b", code):
            end = text.find("\n", match.end())
            self.add(entry, text, "meta_object", match.start(),
                     len(text) if end < 0 else end, {"macro": match.group(1)})
        names = (r"Q_PROPERTY|Q_PLUGIN_METADATA|Q_INTERFACES|Q_DECLARE_INTERFACE|Q_IMPORT_PLUGIN|"
                 r"connect|setupUi|connectSlotsByName|setContextProperty|"
                 r"qmlRegister\w+|setSource|QPluginLoader|QLibrary|QSettings|"
                 r"registerFactory|registerPlugin|registerType")
        for name, start, end, content in calls(text, names):
            args = arguments(content)
            data = {"name": name, "arguments": args}
            kind, certainty = "qt_call", "source_fact"
            if name == "Q_PROPERTY":
                kind = "property"
            elif name == "Q_PLUGIN_METADATA":
                kind = "plugin"
                metadata = re.search(r'\bFILE\s*"([^"]+)"', content)
                iid = re.search(r'\bIID\s+("[^"]+"|\w+)', content)
                data.update(iid_expression=iid.group(1) if iid else None)
                if metadata:
                    import posixpath
                    relative = posixpath.normpath((Path(entry["path"]).parent / metadata.group(1)).as_posix())
                    candidates = {i["path"]: i for i in self.snapshot["files"]}
                    target = candidates.get(relative)
                    if target and target["text_status"] == "ready":
                        parsed = json.loads(read_bytes(self.root / "utf8" / relative))
                        data.update(metadata_file=relative,
                                    metadata_keys=sorted(parsed) if isinstance(parsed, dict) else [])
                    else:
                        data["metadata_unresolved"] = True
                owner = list(re.finditer(r"\bclass\s+(\w+)\b[^;{]*\{", code[:start]))
                data["class_candidate"] = owner[-1].group(1) if owner else None
                data["plugin_id"] = digest([entry["path"], data["class_candidate"], name])
            elif name in {"Q_DECLARE_INTERFACE", "Q_INTERFACES", "Q_IMPORT_PLUGIN"}:
                kind = "plugin_interface"
            elif name == "connect":
                kind = "connection"
                data["form"] = ("signal_slot_macros" if "SIGNAL(" in content.replace(" ", "")
                                else "lambda" if "[" in content else "function_pointer")
                data["runtime_status"] = "unresolved"
            elif name in {"setupUi", "connectSlotsByName"}:
                kind, certainty = "ui_binding", "candidate"
            elif name.startswith("qmlRegister") or name == "setContextProperty":
                kind = "qml_cpp_binding"
            elif name in {"QPluginLoader", "QLibrary", "registerFactory", "registerPlugin", "registerType"}:
                kind, certainty = "plugin_loader", "candidate"
            elif name == "QSettings":
                kind = "settings_reference"
            self.add(entry, text, kind, start, end, data, certainty)
        for match in re.finditer(r"\b(QPluginLoader|QLibrary|QSettings)\s+(\w+)\s*\(", code):
            # 型名と変数名を持つコンストラクターも検出する。
            expression = text[match.start(2):]
            found = next(calls(expression, re.escape(match.group(2))), None)
            if found:
                _, _, end, content = found
                self.add(entry, text, "settings_reference" if match.group(1) == "QSettings"
                         else "plugin_loader", match.start(), match.start(2) + end,
                         {"type": match.group(1), "variable": match.group(2),
                          "arguments": arguments(content)}, "candidate")
        setting_variables = set(re.findall(r"\bQSettings\s*\*?\s+(\w+)", code))
        for variable in setting_variables:
            pattern = r"\b" + re.escape(variable) + r"\s*(?:\.|->)\s*(value|setValue|contains|beginGroup)\s*\("
            for match in re.finditer(pattern, code):
                method_start = match.start(1)
                found = next(calls(text[method_start:], re.escape(match.group(1))), None)
                if found:
                    _, _, end, content = found
                    self.add(entry, text, "settings_reference", match.start(), method_start + end,
                             {"variable": variable, "method": match.group(1),
                              "key_and_default_expressions": arguments(content)}, "candidate")
        # テンプレート付きのQt型登録は通常呼出と分け、構文候補として残す。
        for match in re.finditer(r"\b(qmlRegister\w+)\s*<[^;\n]+", code):
            self.add(entry, text, "qml_cpp_binding", match.start(), match.end(),
                     {"expression": text[match.start():match.end()]}, "candidate")
        labels = list(re.finditer(r"(?:\b(?:public|protected|private)\s+)?"
                                 r"\b(signals|Q_SIGNALS|slots|Q_SLOTS)\s*:", code))
        for label in labels:
            next_label = re.search(r"\b(public|protected|private|signals|Q_SIGNALS)\s*:",
                                   code[label.end():])
            end = label.end() + next_label.start() if next_label else len(code)
            # 宣言を跨ぐマクロ展開・ネストした型は解決せず、字句の候補とする。
            for match in re.finditer(r"\b(\w+)\s*\([^;{}]*\)\s*(?:const\s*)?;",
                                     code[label.end():end]):
                start = label.end() + match.start()
                self.add(entry, text, "signal" if label.group(1) in {"signals", "Q_SIGNALS"}
                         else "slot", start, label.end() + match.end(),
                         {"name": match.group(1)}, "candidate")

    def xml(self, entry, text):
        # Qtツールが出す外部参照・内部サブセットのない宣言は許可する。
        parsed_text = re.sub(r"<!DOCTYPE\s+(?:RCC|UI)\s*>", "", text, flags=re.IGNORECASE)
        if re.search(r"<!\s*(DOCTYPE|ENTITY)\b", parsed_text, re.IGNORECASE):
            raise ScanError("xml_declarations_unsupported")
        root = ET.fromstring(parsed_text)
        if entry["kind"] == "widgets":
            if root.tag != "ui":
                raise ScanError("invalid_ui_root")
            self.add(entry, text, "screen", 0, len(text),
                     {"class": root.findtext("class"), "mode": "widgets"})
            for widget in root.iter("widget"):
                properties = {p.get("name"): "".join(p.itertext()).strip()
                              for p in widget.findall("property")
                              if p.get("name") in {"text", "windowTitle", "toolTip", "enabled"}}
                self.add(entry, text, "widget", 0, len(text),
                         {"class": widget.get("class"), "object_name": widget.get("name"),
                          "properties": properties})
            for connection in root.findall("./connections/connection"):
                self.add(entry, text, "ui_connection", 0, len(text),
                         {tag: connection.findtext(tag) for tag in ("sender", "signal", "receiver", "slot")})
            self.add(entry, text, "auto_connection", 0, len(text),
                     {"pattern": "on_<objectName>_<signal>",
                      "requires": "setupUi/connectSlotsByNameとSlot実装の照合"}, "candidate")
        else:
            if root.tag != "RCC":
                raise ScanError("invalid_qrc_root")
            for resource in root.findall("qresource"):
                prefix = resource.get("prefix", "/").strip("/")
                for file in resource.findall("file"):
                    source = (file.text or "").strip()
                    alias = file.get("alias", source)
                    self.add(entry, text, "resource", 0, len(text),
                             {"url": ":/" + "/".join(v for v in (prefix, alias) if v),
                              "file": source, "alias": alias})

    def qml(self, entry, text):
        code = masked(text)
        self.add(entry, text, "screen", 0, len(text),
                 {"name": entry["path"], "mode": "qml", "coverage": "lexical_partial"})
        pattern = (r"(?m)^\s*(import\s+[^\r\n;]+|(?:readonly\s+)?property\s+[^\r\n;]+|"
                   r"signal\s+[^\r\n;]+|function\s+\w+\s*\([^)]*\)|"
                   r"id\s*:\s*\w+|on[A-Z]\w*\s*:|Connections\s*\{)")
        for match in re.finditer(pattern, code):
            self.add(entry, text, "qml_declaration", match.start(), match.end(),
                     {"expression": text[match.start():match.end()].strip()}, "candidate")

    def run(self):
        for entry in self.snapshot["files"]:
            if entry["text_status"] != "ready":
                self.coverage.append({"path": entry["path"], "status": "unparsed"})
                continue
            text = read_bytes(self.root / "utf8" / entry["path"]).decode("utf-8")
            coverage = "reference_only"
            try:
                if entry["kind"] == "cpp":
                    self.cpp(entry, text)
                    coverage = "lexical_partial"
                elif entry["kind"] in {"widgets", "resource"}:
                    self.xml(entry, text)
                    coverage = "xml"
                elif entry["kind"] == "qml":
                    self.qml(entry, text)
                    coverage = "lexical_partial"
                elif entry["kind"] == "qmldir":
                    for match in re.finditer(r"(?m)^\s*(module|plugin|optional\s+plugin|\w+)\s+[^\n]+", text):
                        self.add(entry, text, "qml_module", match.start(), match.end(),
                                 {"declaration": match.group().strip()})
                    coverage = "lexical_partial"
                elif entry["kind"] in {"javascript", "qmltypes"}:
                    coverage = "unparsed"
                self.coverage.append({"path": entry["path"], "status": coverage})
            except (ScanError, ET.ParseError, json.JSONDecodeError) as exc:
                self.errors.append({"path": entry["path"],
                                    "code": str(exc) if isinstance(exc, ScanError) else "qt_parse_failed"})
                self.coverage.append({"path": entry["path"], "status": "failed"})
        return {"version": VERSION, "records": self.records, "errors": self.errors,
                "coverage": self.coverage, "limitations": LIMITATIONS}


def extract(settings, snapshot):
    return Extractor(settings, snapshot).run()
