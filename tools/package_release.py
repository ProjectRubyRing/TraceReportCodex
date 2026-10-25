#!/usr/bin/env python3
"""Build a RHEL deployment archive using an explicit, reviewed file allowlist."""
import hashlib
import tarfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
FILES = [
    "README.md", ".editorconfig", ".gitattributes", ".gitignore",
    "bin/trace-report.sh", "lib/report.py", "templates/trace-report.xlsx",
    "iam/read-spans-policy.json", "examples/insights-results.json", "examples/make_sample.py",
    "tests/run.sh", "tests/test_report.py", "tests/test_shell.py", "tests/fake_aws.py",
    "tools/build_template.mjs", "tools/verify_workbook.mjs", "tools/package_release.py",
]


def main():
    output = ROOT / "outputs" / "trace-report-rhel9.tar.gz"
    output.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(output, "w:gz", format=tarfile.PAX_FORMAT) as archive:
        for relative in FILES:
            source = ROOT / relative
            info = archive.gettarinfo(str(source), arcname="trace-report/" + relative)
            info.mode = 0o755 if relative.endswith(".sh") else 0o644
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            with source.open("rb") as content:
                archive.addfile(info, content)
    digest = hashlib.sha256(output.read_bytes()).hexdigest()
    output.with_name(output.name + ".sha256").write_text(digest + "  " + output.name + "\n", encoding="ascii")
    print(output)
    print("SHA256: " + digest)


if __name__ == "__main__":
    main()
