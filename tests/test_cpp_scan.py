"""社内コードやLLM認証を使わず、ソース解析から実際の文書取込まで検証する。"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from dataclasses import replace
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
import yaml
from sqlalchemy import select

from cpp_scan.cli import main
from cpp_scan.codewiki import CodeWiki
from cpp_scan.collector import collect
from cpp_scan.config import ConfigError, Settings, load_config
from cpp_scan.documents import validate_markdown
from cpp_scan.handoff import ingest
from cpp_scan.pipeline import analyze, generate, load_analysis, scan, status
from cpp_scan.snapshot import create_snapshot, load_snapshot
from cpp_scan.store import ScanError, project_lock, read_json, write_json
from cpp_scan.text import decode
from file_scan.db import migrate, open_database, tables


@pytest.fixture
def source_project(tmp_path):
    root = tmp_path / "source"
    root.mkdir()
    files = {
        "app.pro": "QT += widgets qml\nQMAKE_EXTRA_TARGETS += custom\n",
        "main.cpp": '#include "plugin.h"\nint main() { return 0; }\n',
        "plugin.h": '''class Example : public QObject {
    Q_OBJECT
    Q_PLUGIN_METADATA(IID "example.interface" FILE "plugin.json")
    Q_INTERFACES(IExample)
    Q_PROPERTY(int count READ count NOTIFY countChanged)
public:
    Q_INVOKABLE void start();
signals:
    void countChanged(int value);
public slots:
    void handle();
};
''',
        "plugin.json": '{"name": "sample", "dependencies": []}',
        "unreferenced.json": '{"secret": "do-not-import"}',
        "connections.cpp": '''void setup() {
    // connect(fake, SIGNAL(fake()), fake, SLOT(fake()));
    const char *s = "connect(alsoFake())";
    auto r = R"raw(connect(rawFake()))raw";
    connect(sender, &Sender::changed, receiver, &Receiver::handle);
    QObject::connect(sender, SIGNAL(changed(int)), receiver, SLOT(handle(int)));
    connect(sender, &Sender::changed, this, [this](int v) { handle(v); });
    engine.rootContext()->setContextProperty("backend", this);
    qmlRegisterType<Example>("Example", 1, 0, "Example");
    QPluginLoader loader("plugins/sample");
    QSettings settings("config/app.ini", QSettings::IniFormat);
}
''',
        "screen.ui": '''<?xml version="1.0" encoding="UTF-8"?>
<ui version="4.0"><class>MainWindow</class><widget class="QMainWindow" name="MainWindow">
<widget class="QPushButton" name="runButton"><property name="text"><string>実行</string></property></widget>
</widget><connections><connection><sender>runButton</sender><signal>clicked()</signal>
<receiver>MainWindow</receiver><slot>start()</slot></connection></connections></ui>
''',
        "screen.qml": '''import QtQuick 2.12
Item {
    id: root
    property int count: 0
    onCountChanged: backend.start()
    Connections { target: backend }
}
''',
        "qmldir": "module Example\nplugin exampleplugin\nScreen 1.0 screen.qml\n",
        "resources.qrc": '<RCC><qresource prefix="/screens"><file alias="main.qml">screen.qml</file></qresource></RCC>',
        "config/app.ini": "[General]\nmode=sample\n",
        "tests/test.cpp": "void ignoredTest() {}\n",
        "third_party/dependency.cpp": "void dependency() {}\n",
        "build/moc_plugin.cpp": "generated\n",
        "ui_screen.h": "generated\n",
    }
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    (root / "legacy.cpp").write_bytes('// 日本語の処理\r\nvoid legacy() {}\r\n'.encode("cp932"))
    settings = Settings("sample-app", root, tmp_path / "artifacts")
    return settings


class FixtureCodeWiki:
    """CodeWiki境界だけを置換し、生成以外の各工程を実際に動かす。"""

    def __init__(self):
        self.analyses = self.generations = 0

    def profile(self):
        return {"fixture": "codewiki-contract-v1"}

    def analyze(self, repo, output):
        self.analyses += 1
        result = []
        for path in sorted(repo.rglob("*")):
            if path.suffix in {".cpp", ".h"}:
                text = path.read_text(encoding="utf-8")
                name = path.relative_to(repo).as_posix()
                result.append({"id": name + "::__file__", "relative_path": name,
                               "name": name, "component_type": "file", "start_line": 1,
                               "end_line": len(text.splitlines()), "depends_on": []})
        return {"components": result}

    def generate(self, repo, output):
        self.generations += 1
        output.mkdir(parents=True, exist_ok=True)
        (output / "overview.md").write_text("# アプリ仕様\n\n[処理仕様](core.md)\n", encoding="utf-8")
        (output / "core.md").write_text("# 起動と処理\n\n入力を受け取り処理する候補。\n", encoding="utf-8")
        write_json(output / "module_tree.json", {"core": {"components": ["main.cpp::main"]}})
        return {"status": "succeeded", "usage": {"input_tokens": 100}}


def _config(tmp_path, **values):
    raw = {"schema_version": "cpp-scan-v1", "project_id": "sample-app",
           "source_root": "source", "artifact_root": "artifacts", **values}
    path = tmp_path / "cpp.yaml"
    path.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")
    return path


def test_configuration_paths_and_complete_design_example(source_project, tmp_path):
    path = _config(tmp_path, documentation={"language": "ja", "provider": "codewiki"},
                   ingestion={"file_scan_config": "docs.yaml"},
                   codewiki={"python": ".venv-codewiki/bin/python"})
    settings = load_config(path)
    assert settings.source_root == source_project.source_root
    assert settings.file_scan_config == tmp_path / "docs.yaml"
    assert settings.codewiki.python == str(tmp_path / ".venv-codewiki/bin/python")
    assert settings.pro_mode == "reference_only"


@pytest.mark.parametrize("values", [
    {"artifact_root": "source/artifacts"}, {"source_root": "artifacts/source"},
    {"project_id": "../outside"}, {"pro_mode": "execute"}, {"include_tests": "false"},
    {"include_files": ["../secret.json"]}, {"unknown": True}, {"max_files": 0},
    {"documentation": {"api_key": "secret"}}, {"encoding_overrides": {"*.cpp": "latin1"}},
])
def test_invalid_configuration_rejected(source_project, tmp_path, values):
    with pytest.raises(ConfigError):
        load_config(_config(tmp_path, **values))


def test_scan_limits_sources_and_detects_both_ui_modes(source_project):
    result = collect(source_project)
    paths = {item.relative_path for item in result.items}
    assert {"plugin.json", "config/app.ini", "screen.ui", "screen.qml", "app.pro"} <= paths
    assert not {"unreferenced.json", "ui_screen.h", "tests/test.cpp", "third_party/dependency.cpp"} & paths
    report = scan(source_project)
    assert report["status"] == "succeeded"
    assert report["detected_ui_modes"] == ["qml", "widgets"]
    assert not (source_project.artifact_root / "snapshots").exists()


def test_missing_source_is_partial_and_not_empty_success(source_project):
    source_project.source_root.rename(source_project.source_root.with_name("offline"))
    result = scan(source_project)
    assert result["status"] == "partial"
    assert result["errors"] == 1


def test_snapshot_preserves_cp932_bom_crlf_and_content_changes(source_project):
    source_project.artifact_root.mkdir()
    original = {p.relative_to(source_project.source_root): p.read_bytes()
                for p in source_project.source_root.rglob("*") if p.is_file()}
    (source_project.source_root / "bom.h").write_bytes(b"\xef\xbb\xbf// bom\r\n")
    first = create_snapshot(source_project)
    second = create_snapshot(source_project)
    assert second["reused"] and first["snapshot_id"] == second["snapshot_id"]
    root = source_project.artifact_root / "snapshots" / first["snapshot_id"]
    assert (root / "original/legacy.cpp").read_bytes() == original[Path("legacy.cpp")]
    assert "日本語" in (root / "utf8/legacy.cpp").read_text(encoding="utf-8")
    mapping = read_json(root / "maps/legacy.cpp.json")
    assert mapping["encoding"] == "cp932" and mapping["line_count"] == 2
    assert mapping["lines"][1]["source_start"] == original[Path("legacy.cpp")].index(b"void")
    assert read_json(root / "maps/bom.h.json")["lines"][0]["source_start"] == 3
    for path, data in original.items():
        assert (source_project.source_root / path).read_bytes() == data
    (source_project.source_root / "app.pro").write_text("# changed reference only\n")
    assert create_snapshot(source_project)["snapshot_id"] != first["snapshot_id"]


def test_decode_failure_keeps_original_and_prevents_full_success(source_project):
    source_project.artifact_root.mkdir()
    (source_project.source_root / "binary.cpp").write_bytes(b"\xff\x00\xff")
    snapshot = create_snapshot(source_project)
    assert snapshot["status"] == "partial"
    entry = next(i for i in snapshot["files"] if i["path"] == "binary.cpp")
    assert entry["text_status"] == "failed"
    assert (source_project.artifact_root / "snapshots" / snapshot["snapshot_id"] /
            "original/binary.cpp").read_bytes() == b"\xff\x00\xff"


def test_symbolic_links_never_read_outside_source(source_project, tmp_path):
    outside = tmp_path / "outside.cpp"
    outside.write_text("private outside\n")
    (source_project.source_root / "link.cpp").symlink_to(outside)
    (source_project.source_root / "linked-dir").symlink_to(tmp_path, target_is_directory=True)
    result = collect(source_project)
    assert "link.cpp" not in {i.relative_path for i in result.items}
    assert len([e for e in result.exclusions if e["reason"] == "symbolic_link"]) == 2


def test_oversize_and_file_count_limits(source_project):
    with pytest.raises(ScanError, match="file_count_limit"):
        scan(replace(source_project, max_files=1))
    source_project.artifact_root.mkdir(exist_ok=True)
    snapshot = create_snapshot(replace(source_project, max_file_bytes=20))
    assert snapshot["status"] == "partial"
    assert any(e["code"] == "file_too_large" for e in snapshot["errors"])


def test_analysis_qt_evidence_and_cache(source_project):
    adapter = FixtureCodeWiki()
    first = analyze(source_project, adapter=adapter)
    assert first["status"] == "succeeded"
    _, result = load_analysis(source_project, first["analysis_id"])
    records = result["qt"]["records"]
    assert len([r for r in records if r["kind"] == "connection"]) == 3
    assert {r["data"]["form"] for r in records if r["kind"] == "connection"} == {
        "lambda", "function_pointer", "signal_slot_macros"}
    assert {r["kind"] for r in records} >= {"plugin", "plugin_interface", "property", "signal", "slot",
                                          "screen", "qml_cpp_binding", "resource", "widget"}
    plugin = next(r for r in records if r["kind"] == "plugin")
    assert plugin["data"]["metadata_file"] == "plugin.json"
    assert plugin["data"]["plugin_id"] != plugin["data"]["iid_expression"]
    assert plugin["evidence"]["start_line"] == 3
    original = (source_project.source_root / "plugin.h").read_bytes()
    evidence = plugin["evidence"]
    assert original[evidence["byte_start"]:evidence["byte_end"]].startswith(b"Q_PLUGIN_METADATA")
    second = analyze(source_project, adapter=adapter)
    assert second["reused"] and adapter.analyses == 1
    (source_project.source_root / "main.cpp").write_text("// comment-only change\nint main() {}\n")
    third = analyze(source_project, adapter=adapter)
    assert third["snapshot_id"] != first["snapshot_id"] and adapter.analyses == 2


def test_generated_documents_validation_cache_and_fresh_forced_generation(source_project):
    adapter = FixtureCodeWiki()
    analysis = analyze(source_project, adapter=adapter)
    first = generate(source_project, analysis["analysis_id"], adapter=adapter)
    root = source_project.artifact_root / "generations" / first["generation_id"] / "documents"
    assert not validate_markdown(root)
    assert (root / "plugins.md").read_text().find("plugin.h:3") >= 0
    assert generate(source_project, analysis["analysis_id"], adapter=adapter)["reused"]
    forced = generate(source_project, analysis["analysis_id"], force=True, adapter=adapter)
    assert forced["generation_id"] != first["generation_id"] and adapter.generations == 2
    assert {i["path"] for i in first["document_files"]} == {i["path"] for i in forced["document_files"]}


def test_corrupt_snapshot_or_generated_markdown_not_reused(source_project):
    adapter = FixtureCodeWiki()
    analysis = analyze(source_project, adapter=adapter)
    generation = generate(source_project, analysis["analysis_id"], adapter=adapter)
    root = source_project.artifact_root / "generations" / generation["generation_id"] / "documents"
    (root / "overview.md").write_text("tampered\n")
    with pytest.raises(ScanError, match="integrity_failed"):
        generate(source_project, analysis["analysis_id"], adapter=adapter)
    snapshot = source_project.artifact_root / "snapshots" / analysis["snapshot_id"]
    (snapshot / "original/main.cpp").write_text("tampered\n")
    with pytest.raises(ScanError, match="integrity_failed"):
        load_snapshot(source_project, analysis["snapshot_id"])


def test_external_failure_does_not_replace_completed_generation(source_project):
    adapter = FixtureCodeWiki()
    analysis = analyze(source_project, adapter=adapter)
    generation = generate(source_project, analysis["analysis_id"], adapter=adapter)
    def fail(*args):
        raise ScanError("codewiki_timeout")
    adapter.generate = fail
    with pytest.raises(ScanError, match="timeout"):
        generate(source_project, analysis["analysis_id"], force=True, adapter=adapter)
    value = status(source_project)
    assert value["selected"]["generation"] == generation["generation_id"]
    assert value["latest"]["generate"]["status"] == "failed"


def _ingestion_config(settings, tmp_path):
    path = tmp_path / "docs.yaml"
    path.write_text(yaml.safe_dump({
        "knowledge_id": "cpp-sample-app", "system_id": "sample", "environment": "test",
        "sources": [{"source_id": "cpp-sample-app", "root": str(settings.artifact_root / "handoff/current")}],
        "include_extensions": [".md"], "storage_dir": str(tmp_path / "document-storage"),
        "report_dir": str(tmp_path / "document-reports"), "pdf_backend": "native",
        "database_url": "sqlite:///" + str(tmp_path / "documents.sqlite"),
    }), encoding="utf-8")
    return replace(settings, file_scan_config=path)


def test_generated_document_handoff_rejects_directory_name_exclusions(source_project, tmp_path):
    from cpp_scan.handoff import _configuration

    settings = _ingestion_config(source_project, tmp_path)
    raw = yaml.safe_load(settings.file_scan_config.read_text(encoding="utf-8"))
    raw["sources"][0]["exclude_directory_names"] = ["BK"]
    settings.file_scan_config.write_text(yaml.safe_dump(raw), encoding="utf-8")
    with pytest.raises(ScanError, match="dedicated_markdown_ingestion_config_required"):
        _configuration(settings)


def test_real_existing_ingestion_is_used_and_stable(source_project, tmp_path):
    settings = _ingestion_config(source_project, tmp_path)
    adapter = FixtureCodeWiki()
    analysis = analyze(settings, adapter=adapter)
    generation = generate(settings, analysis["analysis_id"], adapter=adapter)
    engine = open_database("sqlite:///" + str(tmp_path / "documents.sqlite"))
    migrate(engine)
    try:
        first = ingest(settings, generation["generation_id"], engine=engine)
        second = ingest(settings, generation["generation_id"], engine=engine)
        assert first["status"] == second["status"] == "succeeded"
        assert first["publication"] == "draft" and first["embedding"] == "not_requested"
        assert first["summary"]["registered"] > 0 and second["summary"]["registered"] == 0
        assert first["documents"] == second["documents"]
        with engine.connect() as conn:
            documents = conn.execute(select(tables["documents"])).mappings().all()
            assert len(documents) == len(generation["document_files"])
            assert {r["format"] for r in documents} == {"md"}
            assert not conn.execute(select(tables["chunk_embeddings"])).first()
    finally:
        engine.dispose()


def test_partial_generation_is_blocked_and_does_not_clear_handoff(source_project, tmp_path):
    settings = _ingestion_config(source_project, tmp_path)
    (settings.source_root / "screen.ui").write_text("<ui>broken\n")
    adapter = FixtureCodeWiki()
    analysis = analyze(settings, adapter=adapter)
    assert analysis["status"] == "partial"
    generation = generate(settings, analysis["analysis_id"], adapter=adapter)
    assert generation["status"] == "partial"
    with pytest.raises(ScanError, match="partial_generation"):
        ingest(settings, generation["generation_id"])
    assert not (settings.artifact_root / "handoff/current").exists()


def test_partial_ingestion_retains_old_documents_and_scope_change_is_explicit(source_project, tmp_path):
    settings = _ingestion_config(source_project, tmp_path)
    adapter = FixtureCodeWiki()
    engine = open_database("sqlite:///" + str(tmp_path / "documents.sqlite"))
    migrate(engine)
    try:
        analysis = analyze(settings, adapter=adapter)
        generation = generate(settings, analysis["analysis_id"], adapter=adapter)
        ingest(settings, generation["generation_id"], engine=engine)
        # 入力を読み損ねた版を明示的に渡しても、旧Qt詳細文書は削除しない。
        (settings.source_root / "screen.ui").write_text("<ui>broken\n")
        partial_analysis = analyze(settings, adapter=adapter)
        partial = generate(settings, partial_analysis["analysis_id"], adapter=adapter)
        report = ingest(replace(settings, allow_partial_ingest=True), partial["generation_id"], engine=engine)
        assert report["retained_paths"]
        for path in report["retained_paths"]:
            assert (settings.artifact_root / "handoff/current" / path).is_file()
        assert report["summary"]["deleted"] == 0
        # 範囲の縮小は通常受渡しと区別し、専用オプションなしでは反映しない。
        (settings.source_root / "screen.ui").write_text('<ui><class>Screen</class></ui>')
        narrower = replace(settings, exclude_patterns=("screen.qml",))
        new_analysis = analyze(narrower, adapter=adapter)
        new_generation = generate(narrower, new_analysis["analysis_id"], adapter=adapter)
        with pytest.raises(ScanError, match="scope_change_requires"):
            ingest(narrower, new_generation["generation_id"], engine=engine)
        result = ingest(narrower, new_generation["generation_id"], engine=engine, accept_scope_change=True)
        assert result["status"] == "succeeded"
    finally:
        engine.dispose()


def test_input_changes_during_snapshot_are_partial(source_project, monkeypatch):
    from file_scan.storage import Storage
    original = Storage.snapshot
    calls = 0
    def changing(self, item, *args, **kwargs):
        nonlocal calls
        result = original(self, item, *args, **kwargs)
        calls += 1
        if calls == 1:
            with (source_project.source_root / "main.cpp").open("a") as handle:
                handle.write("// changed after first file copy\n")
        if item.relative_path == "main.cpp" and calls > len(collect(source_project).items):
            item.path.write_text("int main() { return 99; }\n")
            result = original(self, item, *args, **kwargs)
        return result
    monkeypatch.setattr(Storage, "snapshot", changing)
    source_project.artifact_root.mkdir()
    snapshot = create_snapshot(source_project)
    assert snapshot["status"] == "partial"
    assert any(e["code"] == "source_content_changed" for e in snapshot["errors"])


def test_generated_source_links_are_validated_and_rewritten(source_project):
    adapter = FixtureCodeWiki()
    original = adapter.generate
    def generation(repo, output):
        result = original(repo, output)
        (output / "core.md").write_text("# 処理\n\n[原本](main.cpp#L2)\n", encoding="utf-8")
        return result
    adapter.generate = generation
    analysis = analyze(source_project, adapter=adapter)
    result = generate(source_project, analysis["analysis_id"], adapter=adapter)
    assert result["codewiki_source_references"][0]["start_line"] == 2
    def bad(repo, output):
        result = generation(repo, output)
        (output / "core.md").write_text("# 処理\n\n[原本](main.cpp#L999)\n", encoding="utf-8")
        return result
    adapter.generate = bad
    with pytest.raises(ScanError, match="out_of_range"):
        generate(source_project, analysis["analysis_id"], adapter=adapter, force=True)


def test_real_postgres_handoff(source_project, tmp_path, postgres_database):
    settings = _ingestion_config(source_project, tmp_path)
    adapter = FixtureCodeWiki()
    analysis = analyze(settings, adapter=adapter)
    generation = generate(settings, analysis["analysis_id"], adapter=adapter)
    report = ingest(settings, generation["generation_id"], engine=postgres_database)
    assert report["status"] == "succeeded"
    assert report["summary"]["registered"] == len(generation["document_files"])


def test_project_lock_and_cli_errors(source_project, tmp_path, capsys):
    with project_lock(source_project.artifact_root):
        with pytest.raises(ScanError, match="already_running"):
            scan(source_project)
    config = _config(tmp_path)
    assert main(["scan", "--config", str(config)]) == 0
    assert json.loads(capsys.readouterr().out)["files"] > 0
    assert main(["generate", "--config", str(config), "--analysis", "../../outside"]) == 1
    assert "invalid_version_id" in capsys.readouterr().err


def test_runtime_codewiki_requires_an_explicit_endpoint(source_project, monkeypatch):
    monkeypatch.delenv(source_project.documentation.base_url_env, raising=False)
    with pytest.raises(ScanError, match="llm_environment_missing"):
        CodeWiki(source_project).generate(source_project.source_root, source_project.artifact_root / "wiki")


def test_source_encoding_override_preserves_explicit_choice(source_project):
    settings = replace(source_project, encoding_overrides={"*.cpp": "cp932"})
    text, info = decode("日本語".encode("cp932"), "sample.cpp", settings)
    assert text == "日本語" and info["encoding_requested"] == "cp932"


@pytest.mark.skipif(not os.environ.get("CPP_SCAN_CODEWIKI_PYTHON"), reason="固定版CodeWiki実環境を明示した場合のみ")
def test_real_codewiki_structural_adapter(source_project):
    (source_project.source_root / "UPPER.CPP").write_text("void upperCaseExtension() {}\n")
    settings = replace(source_project, codewiki=replace(source_project.codewiki,
                       python=os.environ["CPP_SCAN_CODEWIKI_PYTHON"]))
    result = analyze(settings)
    assert result["status"] == "succeeded"
    _, analysis = load_analysis(settings, result["analysis_id"])
    assert analysis["components"]
    assert any(c["kind"] == "class" for c in analysis["components"])
    assert analysis["syntax_diagnostics"]
    assert any(c["evidence"]["path"] == "UPPER.CPP" for c in analysis["components"])


@pytest.mark.skipif(not os.environ.get("CPP_SCAN_CODEWIKI_PYTHON"), reason="固定版CodeWiki実環境を明示した場合のみ")
def test_real_codewiki_syntax_failure_is_not_full_success(source_project):
    (source_project.source_root / "broken.cpp").write_text("void broken(\n")
    settings = replace(source_project, codewiki=replace(source_project.codewiki,
                       python=os.environ["CPP_SCAN_CODEWIKI_PYTHON"]))
    result = analyze(settings)
    assert result["status"] == "partial"
    _, analysis = load_analysis(settings, result["analysis_id"])
    assert any(d["path"] == "broken.cpp" for d in analysis["syntax_diagnostics"])
    assert any(e["code"] == "codewiki_syntax_unparsed" for e in analysis["errors"])


@pytest.mark.skipif(not os.environ.get("CPP_SCAN_CODEWIKI_PYTHON"), reason="固定版CodeWiki実環境を明示した場合のみ")
def test_real_codewiki_generation_with_local_api_fixture(source_project, monkeypatch):
    # CodeWiki・OpenAI SDK・ツール編集・JSON応答まで実行する。推論先だけローカル固定応答。
    requests = []
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append(payload)
            if payload.get("tools") and not any(m["role"] == "tool" for m in payload["messages"]):
                message = {"role": "assistant", "content": None, "tool_calls": [{
                    "id": "call_write_fixture", "type": "function", "function": {
                        "name": "str_replace_editor", "arguments": json.dumps({
                            "working_dir": "docs", "command": "create", "path": "overview.md",
                            "file_text": "# 全体実装仕様\n\nQtのプラグイン宣言と画面を持つ解析用アプリです。\n",
                        }, ensure_ascii=False)}}]}
                finish = "tool_calls"
            else:
                message = {"role": "assistant", "content": "# 全体実装仕様\n\n仕様書を作成しました。"}
                finish = "stop"
            result = {"id": "chatcmpl-fixture", "object": "chat.completion", "created": 1,
                      "model": "cpp-source-test", "choices": [{"index": 0, "message": message,
                      "finish_reason": finish}], "usage": {"prompt_tokens": 10,
                      "completion_tokens": 10, "total_tokens": 20}}
            data = json.dumps(result).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    settings = replace(source_project, codewiki=replace(source_project.codewiki,
                       python=os.environ["CPP_SCAN_CODEWIKI_PYTHON"], timeout_seconds=30))
    monkeypatch.setenv(settings.documentation.model_env, "cpp-source-test")
    monkeypatch.setenv(settings.documentation.api_key_env, "local-fixture-key")
    monkeypatch.setenv(settings.documentation.base_url_env, f"http://127.0.0.1:{server.server_port}/v1")
    try:
        analysis = analyze(settings)
        result = generate(settings, analysis["analysis_id"])
        assert result["status"] == "succeeded"
        assert requests and all(r["model"] == "cpp-source-test" for r in requests)
        root = settings.artifact_root / "generations" / result["generation_id"]
        assert (root / "documents/wiki/overview.md").is_file()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_cli_module_works_without_database_or_codewiki(source_project, tmp_path):
    config = _config(tmp_path, codewiki={"python": "missing-python"})
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1] / "src"))
    result = subprocess.run([sys.executable, "-m", "cpp_scan", "scan", "--config", str(config)],
                            cwd=tmp_path, env=env, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["status"] == "succeeded"
