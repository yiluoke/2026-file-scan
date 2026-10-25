"""Ubuntu 26.04向けの検証用全依存（dev + search）と実環境の版を照合する。"""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import platform
import sys
from pathlib import Path

from packaging.requirements import Requirement

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", type=Path, help="照合結果の保存先")
    args = parser.parse_args()
    baseline = json.loads((ROOT / "requirements/ubuntu-26.04-baseline.json").read_text(
        encoding="utf-8"))
    failures = []
    if platform.python_version() != baseline["python"]["upstream"]:
        failures.append(f"Python: {platform.python_version()} != {baseline['python']['upstream']}")
    packages = []
    for line in (ROOT / "requirements/ubuntu-26.04.lock").read_text(encoding="utf-8").splitlines():
        if not line or line.startswith(("#", "--")):
            continue
        requirement = Requirement(line)
        if requirement.marker and not requirement.marker.evaluate():
            continue
        try:
            actual = importlib.metadata.version(requirement.name)
        except importlib.metadata.PackageNotFoundError:
            actual = None
        matched = actual is not None and requirement.specifier.contains(actual)
        packages.append({"name": requirement.name, "expected": str(requirement.specifier),
                         "actual": actual, "matched": matched})
        if not matched:
            failures.append(f"{requirement.name}: {actual} != {requirement.specifier}")
    report = {"python": platform.python_version(), "executable": sys.executable,
              "system": platform.system(), "machine": platform.machine(),
              "packages": packages, "failures": failures}
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                             encoding="utf-8")
    print(f"Python {report['python']} / {report['system']} {report['machine']}")
    for failure in failures:
        print(f"不一致: {failure}", file=sys.stderr)
    print(f"固定依存: {len(packages)}件 / 不一致: {len(failures)}件")
    return int(bool(failures))


if __name__ == "__main__":
    raise SystemExit(main())
