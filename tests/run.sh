#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
bash -n bin/trace-report.sh tests/run.sh
if command -v shellcheck >/dev/null; then
  shellcheck bin/trace-report.sh tests/run.sh
else
  printf 'ShellCheck is not installed; static shell analysis skipped.\n' >&2
fi
python3 -m unittest discover -s tests -v
