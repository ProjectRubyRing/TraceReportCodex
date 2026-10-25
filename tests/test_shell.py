import json
import os
import shutil
import stat
import subprocess
import sys
import unittest
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "examples"))
from make_sample import BASE, TRACE_A, record, sample_records  # noqa: E402


@unittest.skipUnless(os.name == "posix" and shutil.which("bash") and shutil.which("jq"), "Shell integration requires Linux Bash and jq")
class ShellIntegrationTests(unittest.TestCase):
    def setUp(self):
        (ROOT / ".work").mkdir(exist_ok=True)
        self.directory = ROOT / ".work" / ("shell-test-" + uuid.uuid4().hex)
        self.directory.mkdir()
        self.mock_bin = self.directory / "mock bin"
        self.mock_bin.mkdir()
        mock = self.mock_bin / "aws"
        shutil.copyfile(ROOT / "tests/fake_aws.py", mock)
        mock.chmod(0o755)
        self.state_file = self.directory / "state.json"
        self.output = self.directory / "output with spaces"

    def tearDown(self):
        resolved = self.directory.resolve()
        if resolved.parent == (ROOT / ".work").resolve() and resolved.name.startswith("shell-test-"):
            shutil.rmtree(resolved)

    def run_report(self, scenario="normal", records=None, extra=None, expected=0):
        state = {"scenario": scenario, "records": sample_records() if records is None else records}
        self.state_file.write_text(json.dumps(state), encoding="utf-8")
        environment = os.environ.copy()
        environment["PATH"] = str(self.mock_bin) + os.pathsep + environment["PATH"]
        environment["FAKE_AWS_STATE"] = str(self.state_file)
        command = ["bash", str(ROOT / "bin/trace-report.sh"), "--start", str(BASE), "--end", str(BASE + 4),
                   "--region", "ap-northeast-1", "--output-dir", str(self.output), "--poll-interval", "1"]
        result = subprocess.run(command + (extra or []), env=environment, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
        state = json.loads(self.state_file.read_text(encoding="utf-8"))
        if expected == 0:
            return json.loads((self.output / "query-manifest.json").read_text(encoding="utf-8")), state
        self.assertFalse(self.output.exists())
        self.assertFalse(list(self.directory.glob(".trace-report.*")))
        return result, state

    def test_normal_results_and_private_output(self):
        metadata, _ = self.run_report()
        self.assertEqual(metadata["span_count"], 9)
        self.assertEqual(stat.S_IMODE(self.output.stat().st_mode) & 0o077, 0)
        self.assertEqual(stat.S_IMODE((self.output / "spans.csv").stat().st_mode) & 0o077, 0)

    def test_polling_ignores_partial_results(self):
        metadata, state = self.run_report("poll")
        self.assertEqual(metadata["span_count"], 9)
        self.assertEqual(len([call for call in state["calls"] if call["action"] == "get-query-results"]), 2)

    def test_all_pages_are_read(self):
        metadata, state = self.run_report("paginate")
        self.assertEqual(metadata["span_count"], 9)
        self.assertEqual(metadata["queries"][0]["pages"], 2)
        self.assertTrue(any("--cli-input-json" in call["args"] for call in state["calls"]))

    def test_split_boundaries_have_no_missing_or_duplicated_spans(self):
        records = [record(TRACE_A, i + 1, 0, "test", "span", i * 500, 50) for i in range(8)]
        metadata, _ = self.run_report(records=records, extra=["--query-limit", "4"])
        self.assertEqual(metadata["span_count"], 8)
        self.assertEqual(metadata["duplicate_records_removed"], 0)
        self.assertEqual(len(metadata["queries"]), 7)
        self.assertEqual(sum(query["rows"] for query in metadata["queries"] if not query["split"]), 8)

    def test_unsplittable_millisecond_fails_without_partial_report(self):
        records = [record(TRACE_A, i + 1, 0, "test", "span", 0, 50) for i in range(2)]
        result, _ = self.run_report(records=records, extra=["--query-limit", "2"], expected=1)
        self.assertIn("one millisecond", result.stderr)

    def test_max_query_guard(self):
        result, _ = self.run_report(extra=["--query-limit", "4", "--max-queries", "1"], expected=1)
        self.assertIn("Maximum query count", result.stderr)

    def test_empty_result_is_a_successful_empty_report(self):
        metadata, _ = self.run_report(records=[])
        self.assertEqual(metadata["span_count"], 0)

    def test_failed_query_is_stopped_and_not_exported(self):
        result, state = self.run_report("failed", expected=1)
        self.assertIn("Failed", result.stderr)
        self.assertEqual(state["calls"][-1]["action"], "stop-query")

    def test_timeout_stops_query(self):
        result, state = self.run_report("timeout", extra=["--timeout", "1"], expected=1)
        self.assertIn("--timeout", result.stderr)
        self.assertEqual(state["calls"][-1]["action"], "stop-query")

    def test_repeated_page_token_is_an_error(self):
        result, _ = self.run_report("repeat-token", expected=1)
        self.assertIn("Repeated pagination token", result.stderr)

    def test_access_denied_is_not_retried_forever(self):
        _, state = self.run_report("denied", expected=254)
        self.assertEqual(len(state["calls"]), 1)

    def test_offline_export_matches_live_export(self):
        metadata, state = self.run_report(extra=["--from-results", str(ROOT / "examples/insights-results.json")])
        self.assertEqual(metadata["span_count"], 9)
        self.assertEqual(state.get("calls", []), [])


if __name__ == "__main__":
    unittest.main()
