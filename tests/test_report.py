import contextlib
import csv
import io
import json
import sys
import shutil
import uuid
import unittest
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))
sys.path.insert(0, str(ROOT / "examples"))
import report  # noqa: E402
from make_sample import BASE, TRACE_A, TRACE_B, record, sample_records  # noqa: E402


def changed_record(**updates):
    result = sample_records()[0]
    data = json.loads(result["@message"])
    data.update(updates)
    result["@message"] = json.dumps(data, ensure_ascii=False)
    return result


class TimeAndNormalizationTests(unittest.TestCase):
    def test_explicit_timezone_and_epoch_have_same_instant(self):
        self.assertEqual(report.parse_time("2026-10-26T09:00:00+09:00"), BASE * 1000)
        self.assertEqual(report.parse_time(str(BASE)), BASE * 1000)
        self.assertEqual(report.parse_time("2026-10-26T00:00:00.001Z"), BASE * 1000 + 1)

    def test_invalid_or_ambiguous_time_fails(self):
        for value in ["2026-10-26 09:00:00", "2026-10-26T00:00:00.0001Z", "2026-10-26T00:00:00+09:99", "-1"]:
            with self.subTest(value=value), self.assertRaises((report.ReportError, ValueError)):
                report.parse_time(value)

    def test_xray_and_w3c_ids_are_one_trace(self):
        xray = "1-" + TRACE_A[:8] + "-" + TRACE_A[8:]
        self.assertEqual(report.canonical_trace_id(xray), TRACE_A)
        with self.assertRaises(report.ReportError):
            report.canonical_trace_id("0" * 32)

    def test_nanoseconds_keep_all_digits(self):
        span = report.normalize(sample_records()[0])
        self.assertEqual(span.start, BASE * 1_000_000_000 + 123)
        self.assertTrue(span.values()[6].endswith(".000000123Z"))
        self.assertEqual(span.values()[20], str(BASE * 1_000_000_000 + 123))
        with self.assertRaises(report.ReportError):
            report.normalize(changed_record(startTimeUnixNano=float(span.start)))

    def test_zero_duration_and_missing_end_are_different(self):
        zero = report.normalize(changed_record(endTimeUnixNano=BASE * 1_000_000_000 + 123, durationNano=0))
        missing = report.normalize(changed_record(endTimeUnixNano=0, durationNano=None))
        self.assertEqual(zero.duration_ms, 0)
        self.assertEqual(zero.issues, [])
        self.assertIsNone(missing.duration_ms)
        self.assertIn("終了時刻なし", missing.issues)

    def test_duration_can_be_derived_from_either_field(self):
        self.assertEqual(report.normalize(changed_record(durationNano=None)).duration_ms, 1400)
        self.assertEqual(report.normalize(changed_record(endTimeUnixNano=None)).duration_ms, 1400)
        with self.assertRaises(report.ReportError):
            report.normalize(changed_record(endTimeUnixNano=1))

    def test_http_and_otel_statuses(self):
        for http, expected in [(200, "OK"), (404, "ERROR"), (429, "THROTTLE"), (500, "FAULT")]:
            span = report.normalize(changed_record(attributes={"http.status_code": http}, status={"code": 1}))
            self.assertEqual(span.status, expected)
        self.assertEqual(report.normalize(changed_record(attributes={}, status={"code": 2})).status, "ERROR")
        self.assertEqual(report.normalize(changed_record(attributes={}, status={"code": 0})).status, "UNSET")

    def test_otlp_attributes_and_service_fallback(self):
        span = report.normalize(changed_record(attributes=[{"key": "http.status_code", "value": {"intValue": "429"}}]))
        self.assertEqual(span.service, "web-gateway")
        self.assertEqual(span.http_status, 429)

    def test_csv_formula_text_is_escaped(self):
        for value in ["=1+1", " +SUM(A1:A2)", "@cmd", "-1", "\t=2", "\ntext"]:
            self.assertEqual(report.csv_safe(value), "'" + value)
        self.assertEqual(report.csv_safe("ordinary, \"text\"\n"), "ordinary, \"text\"\n")


class HierarchyTests(unittest.TestCase):
    def test_parent_precedes_children_even_if_input_is_shuffled(self):
        spans = [report.normalize(item) for item in reversed(sample_records())]
        traces = report.build_traces(spans)
        self.assertEqual([span.id for span in traces[0].spans], [f"{i:016x}" for i in range(1, 7)])
        self.assertEqual([span.depth for span in traces[0].spans], [0, 1, 2, 3, 4, 2])
        self.assertIn("親スパン未取得", traces[2].notes)
        self.assertIn("ルートスパン未取得", traces[2].notes)

    def test_cycles_are_visible_and_terminate(self):
        first = report.normalize(changed_record(parentSpanId="0000000000000002"))
        second = report.normalize(changed_record(spanId="0000000000000002", parentSpanId="0000000000000001"))
        traces = report.build_traces([first, second])
        self.assertEqual(len(traces[0].spans), 2)
        self.assertTrue(all("親子関係が循環" in span.issues for span in traces[0].spans))
        self.assertEqual(report.dependencies(traces), [])

    def test_deep_traces_do_not_use_python_recursion(self):
        spans = [report.normalize(record(TRACE_A, i, i - 1, "worker", "work", i, 1)) for i in range(1, 1501)]
        trace = report.build_traces(spans)[0]
        self.assertEqual(trace.spans[-1].depth, 1499)

    def test_bars_have_half_open_endpoints_and_zero_marker(self):
        parent = report.normalize(record(TRACE_A, 1, 0, "app", "root", 0, 40))
        child = report.normalize(record(TRACE_A, 2, 1, "app", "child", 10, 10))
        trace = report.build_traces([parent, child])[0]
        self.assertEqual(report.timeline_buckets(child, trace), set(range(10, 20)))
        point = report.normalize(record(TRACE_A, 3, 1, "app", "point", 40, 0))
        self.assertEqual(report.timeline_buckets(point, trace), {39})

    def test_dependency_counts_durations_and_nearest_rank(self):
        traces = report.build_traces([report.normalize(item) for item in sample_records()])
        edges = {(row[0], row[1]): row for row in report.dependencies(traces)}
        edge = edges["web-gateway", "order-service"]
        self.assertEqual(edge[2:8], [1, 1, 1, 1250, 1250, 1250])
        self.assertNotIn(("payment-service", "sample_inventory"), edges)


class ExportTests(unittest.TestCase):
    def setUp(self):
        (ROOT / ".work").mkdir(exist_ok=True)
        self.directory = ROOT / ".work" / ("test-" + uuid.uuid4().hex)
        self.directory.mkdir()

    def tearDown(self):
        resolved = self.directory.resolve()
        if resolved.parent == (ROOT / ".work").resolve() and resolved.name.startswith("test-"):
            shutil.rmtree(resolved)

    def export(self, records, **overrides):
        events = self.directory / "raw-results.jsonl"
        events.write_text("".join(json.dumps(item, ensure_ascii=False) + "\n" for item in records), encoding="utf-8")
        queries = self.directory / "queries.jsonl"
        queries.write_text('', encoding="utf-8")
        kwargs = dict(events=str(events), queries=str(queries), template=str(ROOT / "templates/trace-report.xlsx"),
                      output_dir=str(self.directory), start_ms=BASE * 1000, end_ms=(BASE + 3600) * 1000,
                      region="ap-northeast-1", log_group="aws/spans", trace_id="", source="offline")
        kwargs.update(overrides)
        args = type("Args", (), kwargs)()
        with contextlib.redirect_stderr(io.StringIO()):
            report.render(args)
        return json.loads((self.directory / "query-manifest.json").read_text(encoding="utf-8"))

    def test_csv_and_workbook_reconcile_and_preserve_numeric_types(self):
        metadata = self.export(sample_records())
        self.assertEqual((metadata["trace_count"], metadata["span_count"]), (3, 9))
        with (self.directory / "spans.csv").open(encoding="utf-8-sig", newline="") as source:
            rows = list(csv.DictReader(source))
        self.assertEqual(len(rows), 9)
        self.assertEqual(rows[0]["start_time_unix_nano"], str(BASE * 1_000_000_000 + 123))
        book = report.Template(self.directory / "report.xlsx")
        self.assertEqual(book.value("Traces", "F11"), "6")
        self.assertEqual(book.value("Spans", "U9"), rows[0]["start_time_unix_nano"])
        date_cell = book.sheets["Spans"].find(f"{report.S}sheetData/{report.S}row/{report.S}c[@r='G9']")
        self.assertIsNone(date_cell.get("t"))
        self.assertAlmostEqual(float(date_cell.findtext(report.S + "v")), 46321.0, places=6)
        self.assertEqual(len(book.sheets["Traces"].find(report.S + "hyperlinks")), 3)
        self.assertIsNone(book.sheets["Timeline"].find(report.S + "autoFilter"))
        for sheet in book.sheets.values():
            self.assertFalse(any(node.tag == report.S + "f" for node in sheet.iter()))
        with zipfile.ZipFile(self.directory / "report.xlsx") as archive:
            self.assertIsNone(archive.testzip())
            for name in archive.namelist():
                if name.endswith(".xml"):
                    ET.fromstring(archive.read(name))

    def test_empty_results_export_headers_and_zero_counts(self):
        metadata = self.export([])
        self.assertEqual(metadata["span_count"], 0)
        self.assertEqual(metadata["trace_count"], 0)
        with zipfile.ZipFile(self.directory / "report.xlsx") as archive:
            sheet = ET.fromstring(archive.read("xl/worksheets/sheet1.xml"))
        cell = lambda address: sheet.find(f"{report.S}sheetData/{report.S}row/{report.S}c[@r='{address}']")
        self.assertEqual(report.cell_text(cell("B6"), []), "0")
        self.assertIn("スパンはありません", report.cell_text(cell("A11"), []))

    def test_duplicate_is_removed_but_conflicting_id_fails(self):
        metadata = self.export([sample_records()[0], sample_records()[0]])
        self.assertEqual(metadata["duplicate_records_removed"], 1)
        with self.assertRaisesRegex(report.ReportError, "Conflicting"):
            self.export([sample_records()[0], changed_record(name="different")])

    def test_time_and_trace_selection_is_half_open(self):
        metadata = self.export(sample_records(), trace_id=TRACE_B, start_ms=(BASE + 2) * 1000, end_ms=(BASE + 3) * 1000)
        self.assertEqual(metadata["span_count"], 2)
        metadata = self.export(sample_records(), start_ms=BASE * 1000, end_ms=BASE * 1000 + 20)
        self.assertEqual(metadata["span_count"], 1)

    def test_long_cells_are_marked_while_csv_keeps_full_payload(self):
        message = "very long " + "あ" * 40000
        metadata = self.export([changed_record(name=message)])
        self.assertGreater(metadata["excel_truncated_cells"], 0)
        book = report.Template(self.directory / "report.xlsx")
        self.assertIn("truncated", book.value("Spans", "E9"))
        with (self.directory / "spans.csv").open(encoding="utf-8-sig", newline="") as source:
            csv.field_size_limit(4 * 1024 * 1024)
            row = next(csv.DictReader(source))
        self.assertEqual(row["name"], message)

    def test_formula_names_remain_literal_in_xlsx(self):
        self.export([changed_record(name="=HYPERLINK(\"example\")")])
        book = report.Template(self.directory / "report.xlsx")
        self.assertEqual(book.value("Spans", "E9"), '=HYPERLINK("example")')
        cell = book.sheets["Spans"].find(f"{report.S}sheetData/{report.S}row/{report.S}c[@r='E9']")
        self.assertEqual(cell.get("t"), "inlineStr")
        with (self.directory / "spans.csv").open(encoding="utf-8-sig", newline="") as source:
            self.assertTrue(next(csv.DictReader(source))["name"].startswith("'="))

    def test_template_contract_and_xml_controls(self):
        template = report.Template(ROOT / "templates/trace-report.xlsx")
        cell = template.cell("A1", "literal _x0041_ and \x01", "0")
        xml = ET.tostring(cell).decode()
        self.assertIn("_x005F_x0041_", xml)
        self.assertIn("_x0001_", xml)
        ET.fromstring(ET.tostring(cell))
        bad = self.directory / "bad.xlsx"
        with zipfile.ZipFile(bad, "w") as archive:
            archive.writestr("wrong.xml", "<wrong />")
        with self.assertRaises(report.ReportError):
            report.Template(bad)


if __name__ == "__main__":
    unittest.main()
