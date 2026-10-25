"""公開サンプルをプロジェクト内の仮想ファイルサーバーへ配置する。

ネットワークアクセスはこの明示的な準備コマンドだけで行う。
取り込み本体は設定したディレクトリを通常のファイルサーバーと同じ経路で収集する。
"""

import argparse
import hashlib
import json
import tempfile
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ROOT = ROOT / "data/file-server/department"


def download(root: Path) -> list[dict]:
    root = root.resolve()
    samples = json.loads((ROOT / "examples/public-samples.json").read_text(encoding="utf-8"))
    results = []
    for sample in samples:
        path = root / sample["path"]
        if not path.resolve().is_relative_to(root):
            raise ValueError("サンプル保存先が収集ルート外です")
        request = urllib.request.Request(sample["url"], headers={"User-Agent": "file-scan-sample-test/1.0"})
        with urllib.request.urlopen(request, timeout=60) as response:
            content = response.read(20 * 1024 * 1024 + 1)
        if len(content) > 20 * 1024 * 1024:
            raise ValueError("サンプルがサイズ上限を超えています")
        if content.lstrip().lower().startswith((b"<!doctype html", b"<html")):
            raise ValueError("サンプルの代わりにHTMLを受信しました")
        expected_hash = sample.get("sha256")
        checksum = hashlib.sha256(content).hexdigest()
        if expected_hash and checksum != expected_hash:
            raise ValueError("公開サンプルのハッシュが取得記録と一致しません")
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as handle:
            handle.write(content)
            temporary = Path(handle.name)
        try:
            if path.suffix in {".docx", ".xlsx"}:
                with ZipFile(temporary) as archive:
                    part = "word/document.xml" if path.suffix == ".docx" else "xl/workbook.xml"
                    if part not in archive.namelist() or archive.testzip() is not None:
                        raise ValueError("Officeファイルの構造が不正です")
            elif path.suffix == ".pdf" and not content.startswith(b"%PDF-"):
                raise ValueError("PDFヘッダーが不正です")
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)
        results.append({**sample, "sha256": checksum, "bytes": len(content)})
        print(f"{sample['path']}: {len(content)} bytes", flush=True)
    # 実際の共有フォルダにある旧版・Officeロックファイルも除外設定で再現する。
    (root / "旧版").mkdir(exist_ok=True)
    (root / "旧版/除外確認.txt").write_text("旧版フォルダは収集しません。\n", encoding="utf-8")
    (root / "word/~$lock.docx").write_text("Office一時ファイルの除外確認", encoding="utf-8")
    report = ROOT / "artifacts/reports/public-samples-download.json"
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(json.dumps({"downloaded_at": datetime.now(timezone.utc).isoformat(),
                                  "root": str(root), "samples": results},
                                 ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return results


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    args = parser.parse_args()
    download(args.root)
