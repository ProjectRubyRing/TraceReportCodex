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
        self.switchback_script = self.directory / "switch back's $config [1].sh"
        self.switchback_log = self.directory / "switchback.log"

    def tearDown(self):
        resolved = self.directory.resolve()
        if resolved.parent == (ROOT / ".work").resolve() and resolved.name.startswith("shell-test-"):
            shutil.rmtree(resolved)

    def write_switchback(self, body="export FAKE_AWS_SWITCHED_BACK=1\n"):
        self.switchback_script.write_text('printf "sourced\\n" >> "$FAKE_AWS_SWITCHBACK_LOG"\n' + body, encoding="utf-8")
        self.switchback_script.chmod(0o600)  # source needs read permission, not execute permission.
        return ["--switchback-mode", "auto", "--switchback-script", str(self.switchback_script)]

    def run_report(self, scenario="normal", records=None, extra=None, expected=0, failures=None):
        state = {"scenario": scenario, "records": sample_records() if records is None else records, "failures": failures or []}
        self.state_file.write_text(json.dumps(state), encoding="utf-8")
        environment = os.environ.copy()
        environment["PATH"] = str(self.mock_bin) + os.pathsep + environment["PATH"]
        environment["FAKE_AWS_STATE"] = str(self.state_file)
        environment["FAKE_AWS_SWITCHED_BACK"] = "0"
        environment["FAKE_AWS_SWITCHBACK_LOG"] = str(self.switchback_log)
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
        result, state = self.run_report("denied", expected=254)
        self.assertEqual(len(state["calls"]), 1)
        self.assertIn("WARNING", result.stderr)
        self.assertIn("switchback", result.stderr.lower())

    def test_warn_mode_recommends_source_without_running_it(self):
        self.write_switchback()
        result, state = self.run_report("denied", extra=["--switchback-mode", "warn", "--switchback-script",
                                                        str(self.switchback_script)], expected=254)
        self.assertIn("source ", result.stderr)
        self.assertIn("--switchback-mode auto", result.stderr)
        self.assertEqual(len(state["calls"]), 1)
        self.assertFalse(self.switchback_log.exists())

    def test_authentication_errors_recommend_switchback(self):
        errors = [(f"An error occurred ({code}) when calling the StartQuery operation: Mock error", 254)
                  for code in ("AccessDenied", "UnauthorizedOperation", "UnauthorizedException", "ForbiddenException",
                               "UnrecognizedClientException", "InvalidClientTokenId", "InvalidAccessKeyId",
                               "ExpiredToken", "ExpiredTokenException", "SignatureDoesNotMatch", "InvalidSignatureException")]
        errors += [("Unable to locate credentials. You can configure credentials by running aws configure.", 253),
                   ("Partial credentials found in env, missing: AWS_SECRET_ACCESS_KEY", 255),
                   ("Error when retrieving token from sso: Token has expired and refresh failed", 255),
                   ("The SSO session associated with this profile has expired or is otherwise invalid", 255)]
        for message, status in errors:
            with self.subTest(message=message):
                result, state = self.run_report(expected=status, failures=[{"action": "start-query", "message": message,
                                                                           "status": status}])
                self.assertIn("WARNING", result.stderr)
                self.assertIn("switchback", result.stderr.lower())
                self.assertEqual(len(state["calls"]), 1)

    def test_auto_switchback_retries_with_sourced_environment(self):
        metadata, state = self.run_report(extra=self.write_switchback(), failures=[{"action": "start-query"}])
        self.assertEqual(metadata["span_count"], 9)
        self.assertEqual([call["action"] for call in state["calls"]], ["start-query", "start-query", "get-query-results"])
        self.assertEqual([call["switched_back"] for call in state["calls"]], [False, True, True])
        self.assertEqual(self.switchback_log.read_text(encoding="utf-8"), "sourced\n")

    def test_auto_switchback_during_polling_keeps_the_query(self):
        metadata, state = self.run_report(extra=self.write_switchback(), failures=[{"action": "get-query-results"}])
        self.assertEqual(metadata["span_count"], 9)
        self.assertEqual(len(state["queries"]), 1)
        self.assertEqual([call["action"] for call in state["calls"]], ["start-query", "get-query-results", "get-query-results"])
        self.assertEqual(state["calls"][1]["args"], state["calls"][2]["args"])

    def test_auto_switchback_during_pagination_keeps_the_page(self):
        metadata, state = self.run_report("paginate", extra=self.write_switchback(),
                                          failures=[{"action": "get-query-results", "paginated": True}])
        self.assertEqual(metadata["span_count"], 9)
        self.assertEqual(metadata["queries"][0]["pages"], 2)
        self.assertEqual(state["calls"][-2]["args"], state["calls"][-1]["args"])
        self.assertFalse(state["calls"][-2]["switched_back"])
        self.assertTrue(state["calls"][-1]["switched_back"])

    def test_auto_switchback_runs_at_most_once_when_access_stays_denied(self):
        result, state = self.run_report("denied", extra=self.write_switchback(), expected=254)
        self.assertEqual(len(state["calls"]), 2)
        self.assertEqual(self.switchback_log.read_text(encoding="utf-8"), "sourced\n")
        self.assertIn("after switchback", result.stderr)

    def test_later_auth_failure_does_not_switchback_again(self):
        result, state = self.run_report(extra=self.write_switchback(), expected=254,
                                        failures=[{"action": "start-query"}, {"action": "get-query-results", "when": "after"}])
        self.assertEqual([call["action"] for call in state["calls"]],
                         ["start-query", "start-query", "get-query-results", "stop-query"])
        self.assertEqual(self.switchback_log.read_text(encoding="utf-8"), "sourced\n")
        self.assertTrue(state["calls"][-1]["switched_back"])
        self.assertIn("after switchback", result.stderr)

    def test_failed_switchback_stops_without_retrying_aws(self):
        result, state = self.run_report(extra=self.write_switchback("set +e\nreturn 7\n"), expected=7,
                                        failures=[{"action": "start-query"}])
        self.assertIn("Switchback script failed", result.stderr)
        self.assertEqual(len(state["calls"]), 1)
        self.assertEqual(self.switchback_log.read_text(encoding="utf-8"), "sourced\n")

    def test_source_output_and_directory_changes_do_not_break_report(self):
        options = self.write_switchback('[[ $# -eq 0 ]] || return 9\nprintf "switchback progress\\n"\n'
                                       'cd /\nset +e\nexport FAKE_AWS_SWITCHED_BACK=1\n')
        options[-1] = os.path.relpath(self.switchback_script)
        options += ["--template", os.path.relpath(ROOT / "templates/trace-report.xlsx"), "--profile", "reporting"]
        metadata, state = self.run_report(extra=options, failures=[{"action": "start-query"}])
        self.assertEqual(metadata["span_count"], 9)
        for call in state["calls"]:
            self.assertEqual(call["args"][call["args"].index("--profile") + 1], "reporting")

    def test_auto_mode_does_not_source_when_aws_succeeds(self):
        self.run_report(extra=self.write_switchback())
        self.assertFalse(self.switchback_log.exists())

    def test_switchback_timeout_does_not_retry_aws(self):
        options = self.write_switchback("sleep 2\nexport FAKE_AWS_SWITCHED_BACK=1\n")
        result, state = self.run_report(extra=options + ["--timeout", "1"], expected=1,
                                        failures=[{"action": "start-query"}])
        self.assertIn("--timeout", result.stderr)
        self.assertEqual(len(state["calls"]), 1)

    def test_cleanup_does_not_start_switchback_while_exiting(self):
        result, state = self.run_report("failed", extra=self.write_switchback(), expected=1,
                                        failures=[{"action": "stop-query", "when": "always"}])
        self.assertEqual(state["calls"][-1]["action"], "stop-query")
        self.assertIn("Could not stop query", result.stderr)
        self.assertFalse(self.switchback_log.exists())

    def test_non_authentication_errors_do_not_switchback(self):
        options = self.write_switchback()
        for message, status in [("Could not connect to the endpoint URL: https://logs.invalid", 255),
                                ("An error occurred (ThrottlingException) when calling the StartQuery operation: slow down", 254),
                                ("An error occurred (ResourceNotFoundException) when calling the StartQuery operation: "
                                 "Log group AccessDeniedException does not exist", 254),
                                ("Parameter validation failed: Invalid length for parameter queryString", 252)]:
            with self.subTest(message=message):
                result, state = self.run_report(extra=options, expected=status,
                                                failures=[{"action": "start-query", "message": message, "status": status}])
                self.assertIn(message, result.stderr)
                self.assertEqual(len(state["calls"]), 1)
                self.assertFalse(self.switchback_log.exists())

    def test_switchback_options_are_validated_before_aws(self):
        cases = [(["--switchback-mode", "invalid"], "--switchback-mode"),
                 (["--switchback-mode"], "Missing value"),
                 (["--switchback-script"], "Missing value"),
                 (["--switchback-mode", "auto"], "--switchback-script"),
                 (["--switchback-mode", "auto", "--switchback-script", str(self.switchback_script)], "--switchback-script"),
                 (["--switchback-mode", "auto", "--switchback-script", str(self.directory)], "--switchback-script")]
        for options, message in cases:
            with self.subTest(options=options):
                result, state = self.run_report(extra=options, expected=1)
                self.assertIn(message, result.stderr)
                self.assertEqual(state.get("calls", []), [])

    def test_offline_mode_does_not_require_or_source_switchback_script(self):
        options = ["--from-results", str(ROOT / "examples/insights-results.json"), "--switchback-mode", "auto",
                   "--switchback-script", str(self.switchback_script)]
        metadata, state = self.run_report(extra=options)
        self.assertEqual(metadata["span_count"], 9)
        self.assertEqual(state.get("calls", []), [])
        self.assertFalse(self.switchback_log.exists())

    def test_offline_export_matches_live_export(self):
        metadata, state = self.run_report(extra=["--from-results", str(ROOT / "examples/insights-results.json")])
        self.assertEqual(metadata["span_count"], 9)
        self.assertEqual(state.get("calls", []), [])


if __name__ == "__main__":
    unittest.main()
