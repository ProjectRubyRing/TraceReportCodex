#!/usr/bin/env python3
"""Normalize CloudWatch span records and fill the bundled OOXML template.

Runtime dependencies are deliberately restricted to the Python 3.9+ standard library.
All remote query orchestration is owned by bin/trace-report.sh.
"""

import argparse
import copy
import csv
import datetime as dt
import json
import math
import posixpath
import re
import sys
import zipfile
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from xml.etree import ElementTree as ET


UTC = dt.timezone.utc
EPOCH = dt.datetime(1970, 1, 1, tzinfo=UTC)
NS = {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
S = "{" + NS["s"] + "}"
R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
ET.register_namespace("", NS["s"])
ET.register_namespace("r", R[1:-1])

TRACE_HEADERS = [
    "Trace ID", "ルートサービス", "ルート操作", "開始 (UTC)", "経過時間 (ms)",
    "スパン数", "Fault", "Error", "Throttle", "親未取得", "取得分の注意事項",
]
TIMELINE_HEADERS = ["スパン / 親子階層", "サービス", "状態", "HTTP", "開始差 (ms)", "時間 (ms)"]
DEPENDENCY_HEADERS = [
    "呼び出し元", "呼び出し先", "呼び出し数", "トレース数", "異常数",
    "平均 (ms)", "p95 (ms)", "最大 (ms)", "備考",
]
SPAN_HEADERS = [
    "Trace ID", "Span ID", "Parent span ID", "Service", "Name", "Kind", "Start (UTC)",
    "End (UTC)", "Duration (ms)", "Status", "HTTP method", "HTTP URL", "HTTP status",
    "Remote service", "Remote operation", "Status message", "Resource attributes (JSON)",
    "Attributes (JSON)", "Events (JSON)", "Links (JSON)", "Start (ns)", "End (ns)",
    "Log timestamp (UTC)", "Issues",
]
CSV_HEADERS = [
    "trace_id", "span_id", "parent_span_id", "service", "name", "kind", "start_time_utc",
    "end_time_utc", "duration_ms", "status", "http_method", "http_url", "http_status_code",
    "remote_service", "remote_operation", "status_message", "resource_attributes_json",
    "attributes_json", "events_json", "links_json", "start_time_unix_nano", "end_time_unix_nano",
    "log_timestamp_utc", "issues", "log_record_pointer", "raw_message_json",
]
ERROR_STATES = {"ERROR", "FAULT", "THROTTLE"}


class ReportError(Exception):
    """A user-visible input or template contract error, without payload contents."""


def json_text(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def canonical_trace_id(value):
    value = str(value).lower()
    if re.fullmatch(r"1-[0-9a-f]{8}-[0-9a-f]{24}", value):
        value = value[2:10] + value[11:]
    if not re.fullmatch(r"[0-9a-f]{32}", value) or int(value, 16) == 0:
        raise ReportError("Trace ID must be a nonzero 32-hex or X-Ray trace ID")
    return value


def span_id(value, parent=False):
    value = str(value or "").lower()
    if parent and value in ("", "0" * 16):
        return ""
    if not re.fullmatch(r"[0-9a-f]{16}", value) or int(value, 16) == 0:
        raise ReportError("Invalid span ID or parent span ID")
    return value


def parse_time(value):
    """CLI times have an explicit timezone and no hidden host-local interpretation."""
    if re.fullmatch(r"[0-9]+", value):
        milliseconds = int(value) * 1000
        EPOCH + dt.timedelta(milliseconds=milliseconds)  # Check supported date range.
        return milliseconds
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,3})?(?:Z|[+-]\d{2}:\d{2})", value):
        raise ReportError("Time must be RFC3339 with timezone and at most 3 fractional digits, or Unix seconds")
    # datetime accepts offset minutes >= 60 by normalization; RFC3339 does not.
    if value[-1] != "Z" and (int(value[-5:-3]) > 23 or int(value[-2:]) > 59):
        raise ReportError("Invalid RFC3339 UTC offset")
    parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)
    delta = parsed - EPOCH
    result = (delta.days * 86400 + delta.seconds) * 1000 + delta.microseconds // 1000
    if result < 0:
        raise ReportError("Time must not be earlier than 1970-01-01 UTC")
    return result


def log_time(value):
    if not isinstance(value, str):
        raise ReportError("Query results must include @timestamp")
    parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    # Logs Insights renders @timestamp in UTC without an offset in API responses.
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    delta = parsed.astimezone(UTC) - EPOCH
    return (delta.days * 86400 + delta.seconds) * 1000 + delta.microseconds // 1000


def iso_ns(nanoseconds):
    if nanoseconds is None:
        return ""
    seconds, remainder = divmod(nanoseconds, 1_000_000_000)
    return (EPOCH + dt.timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%S") + f".{remainder:09d}Z"


def excel_date(nanoseconds):
    if nanoseconds is None:
        return None
    # Modern dates use Excel's 1900 system including its historical leap-day bug.
    return 25569 + nanoseconds / 86_400_000_000_000


def nano(value, label):
    # An IEEE double cannot preserve nanosecond epoch timestamps. Never round one.
    if isinstance(value, bool) or not isinstance(value, (str, int)) or not re.fullmatch(r"[0-9]+", str(value)):
        raise ReportError(f"{label} must be an integer or decimal integer string")
    return int(value)


def otel_value(value):
    if not isinstance(value, dict):
        return value
    for key in ("stringValue", "intValue", "doubleValue", "boolValue", "bytesValue"):
        if key in value:
            return value[key]
    if "arrayValue" in value:
        return [otel_value(item) for item in value["arrayValue"].get("values", [])]
    if "kvlistValue" in value:
        return attributes(value["kvlistValue"].get("values", []))
    return value


def attributes(value):
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    if isinstance(value, list) and all(isinstance(item, dict) and "key" in item for item in value):
        return {str(item["key"]): otel_value(item.get("value")) for item in value}
    raise ReportError("Span attributes must be an object or OTLP key/value array")


def attr(mapping, *keys):
    for key in keys:
        if key in mapping and mapping[key] not in (None, ""):
            return mapping[key]
        current = mapping
        for component in key.split("."):
            if not isinstance(current, dict) or component not in current:
                current = None
                break
            current = current[component]
        if current not in (None, ""):
            return current
    return ""


def text(value):
    if value is None:
        return ""
    return json_text(value) if isinstance(value, (dict, list)) else str(value)


def csv_safe(value):
    if not isinstance(value, str):
        return value
    # CSV quoting alone does not prevent Excel from evaluating a field as a formula.
    stripped = value.lstrip()
    if value.startswith(("\t", "\r", "\n")) or stripped.startswith(("=", "+", "-", "@")):
        return "'" + value
    return value


@dataclass
class Span:
    trace: str
    id: str
    parent: str
    service: str
    name: str
    kind: str
    start: int
    end: object
    duration: object
    status: str
    http_method: str
    http_url: str
    http_status: object
    remote_service: str
    remote_operation: str
    status_message: str
    resource: dict
    attrs: dict
    events: list
    links: list
    log_ms: int
    pointer: str
    message: str
    issues: list = field(default_factory=list)
    depth: int = 0

    @property
    def duration_ms(self):
        return None if self.duration is None else self.duration / 1_000_000

    def values(self, excel=False):
        start = excel_date(self.start) if excel else iso_ns(self.start)
        end = excel_date(self.end) if excel else iso_ns(self.end)
        stamp = excel_date(self.log_ms * 1_000_000) if excel else iso_ns(self.log_ms * 1_000_000)
        return [
            self.trace, self.id, self.parent, self.service, self.name, self.kind, start, end,
            self.duration_ms, self.status, self.http_method, self.http_url, self.http_status,
            self.remote_service, self.remote_operation, self.status_message, json_text(self.resource),
            json_text(self.attrs), json_text(self.events), json_text(self.links), str(self.start),
            "" if self.end is None else str(self.end), stamp, "; ".join(self.issues),
        ]


def normalize(record):
    try:
        message = record["@message"]
        data = json.loads(message)
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        raise ReportError("@message must contain one JSON span object") from exc
    if not isinstance(data, dict):
        raise ReportError("@message must contain one JSON span object")
    trace = canonical_trace_id(data.get("traceId", ""))
    identifier = span_id(data.get("spanId"))
    parent = span_id(data.get("parentSpanId"), parent=True)
    start = nano(data.get("startTimeUnixNano"), "startTimeUnixNano")
    duration_value = data.get("durationNano")
    duration = None if duration_value is None else nano(duration_value, "durationNano")
    end_value = data.get("endTimeUnixNano")
    end = None if end_value in (None, 0, "0") else nano(end_value, "endTimeUnixNano")
    if end is not None and end < start:
        raise ReportError("Span end is earlier than span start")
    if end is None and duration is not None:
        end = start + duration
    if duration is None and end is not None:
        duration = end - start
    iso_ns(start)
    iso_ns(end)  # Reject timestamps outside the report's supported date range.
    attrs = attributes(data.get("attributes"))
    resource_object = data.get("resource") or {}
    if not isinstance(resource_object, dict):
        raise ReportError("Span resource must be an object")
    resource = attributes(resource_object.get("attributes"))
    service = text(attr(attrs, "aws.local.service") or attr(resource, "aws.local.service", "service.name") or attr(attrs, "service.name"))
    code = attr(attrs, "http.response.status_code", "http.status_code")
    if code != "":
        if isinstance(code, bool) or not re.fullmatch(r"[1-5][0-9]{2}", str(code)):
            raise ReportError("Invalid HTTP response status code")
        code = int(code)
    else:
        code = None
    status_object = data.get("status") or {}
    if not isinstance(status_object, dict):
        raise ReportError("Span status must be an object")
    status_code = str(status_object.get("code", "UNSET")).upper()
    state = {"0": "UNSET", "1": "OK", "2": "ERROR", "STATUS_CODE_UNSET": "UNSET",
             "STATUS_CODE_OK": "OK", "STATUS_CODE_ERROR": "ERROR"}.get(status_code, status_code)
    if code == 429:
        state = "THROTTLE"
    elif code is not None and code >= 500:
        state = "FAULT"
    elif code is not None and code >= 400:
        state = "ERROR"
    events, links = data.get("events") or [], data.get("links") or []
    if not isinstance(events, list) or not isinstance(links, list):
        raise ReportError("Span events and links must be arrays")
    kind = str(data.get("kind", "UNSPECIFIED")).upper()
    kind = {"0": "UNSPECIFIED", "1": "INTERNAL", "2": "SERVER", "3": "CLIENT",
            "4": "PRODUCER", "5": "CONSUMER"}.get(kind, kind)
    span = Span(
        trace, identifier, parent, service or "(unknown)", text(data.get("name")) or "(unnamed)",
        kind, start, end, duration, state,
        text(attr(attrs, "http.request.method", "http.method")), text(attr(attrs, "url.full", "http.url")), code,
        text(attr(attrs, "aws.remote.service", "peer.service", "server.address")),
        text(attr(attrs, "aws.remote.operation")), text(status_object.get("message")), resource, attrs,
        events, links, log_time(record.get("@timestamp")), text(record.get("@ptr")), message,
    )
    if not service:
        span.issues.append("サービス名なし")
    if end is None:
        span.issues.append("終了時刻なし")
    if duration is not None and end is not None and abs(end - start - duration) > 1_000_000:
        span.issues.append("終了時刻とdurationNanoの差が1ms超")
    if state not in {"OK", "UNSET"} | ERROR_STATES:
        span.issues.append("未対応のstatus.code")
    return span


@dataclass
class Trace:
    id: str
    spans: list
    roots: list
    start: int
    end: int
    notes: list

    @property
    def duration_ms(self):
        return (self.end - self.start) / 1_000_000


def build_traces(spans):
    grouped = defaultdict(list)
    for span in spans:
        grouped[span.trace].append(span)
    traces = []
    for identifier, members in grouped.items():
        members.sort(key=lambda item: (item.start, item.id))
        by_id = {item.id: item for item in members}
        parents = {}
        roots = [item for item in members if not item.parent]
        for item in members:
            parents[item.id] = item.parent if item.parent in by_id else ""
            if item.parent and item.parent not in by_id:
                item.issues.append("親スパン未取得")
        # Walk parent chains iteratively: long traces must not hit Python's recursion limit.
        finished = set()
        for item in members:
            chain, positions = [], {}
            current = item.id
            while current and current not in finished:
                if current in positions:
                    cycle = chain[positions[current]:]
                    for node in cycle:
                        by_id[node].issues.append("親子関係が循環")
                    parents[current] = ""
                    break
                positions[current] = len(chain)
                chain.append(current)
                current = parents[current]
            finished.update(chain)
        children = defaultdict(list)
        for item in members:
            children[parents[item.id]].append(item)
        ordered, stack = [], [(item, 0) for item in reversed(children[""])]
        while stack:
            item, depth = stack.pop()
            item.depth = depth
            ordered.append(item)
            stack.extend((child, depth + 1) for child in reversed(children[item.id]))
        notes = []
        if not roots:
            notes.append("ルートスパン未取得")
        elif len(roots) > 1:
            notes.append("複数ルート")
        for item in ordered:
            for issue in item.issues:
                if issue not in notes:
                    notes.append(issue)
        traces.append(Trace(identifier, ordered, roots, min(item.start for item in members),
                            max(item.end if item.end is not None else item.start for item in members), notes))
    return sorted(traces, key=lambda item: (item.start, item.id))


def dependencies(traces):
    edges = defaultdict(list)
    for trace in traces:
        by_id = {item.id: item for item in trace.spans}
        for item in trace.spans:
            parent = by_id.get(item.parent)
            if parent and parent.service != item.service and "親子関係が循環" not in item.issues:
                edges[(parent.service, item.service)].append(item)
    rows = []
    for (source, target), items in sorted(edges.items()):
        durations = sorted(item.duration_ms for item in items if item.duration_ms is not None)
        rows.append([
            source, target, len(items), len({item.trace for item in items}),
            sum(item.status in ERROR_STATES for item in items),
            sum(durations) / len(durations) if durations else None,
            durations[math.ceil(len(durations) * 0.95) - 1] if durations else None,
            max(durations) if durations else None,
            "終了時刻なしを時間集計から除外" if len(durations) != len(items) else "",
        ])
    return rows


def column_name(number):
    result = ""
    while number:
        number, digit = divmod(number - 1, 26)
        result = chr(65 + digit) + result
    return result


def column_number(address):
    result = 0
    for char in re.match(r"[A-Z]+", address).group():
        result = result * 26 + ord(char) - 64
    return result


def cell_text(cell, shared):
    if cell is None:
        return ""
    if cell.get("t") == "inlineStr":
        return "".join(cell.itertext())
    value = cell.findtext(S + "v", "")
    return shared[int(value)] if cell.get("t") == "s" else value


class Template:
    """Preserve template package parts and replace only the four owned data regions."""

    def __init__(self, path):
        with zipfile.ZipFile(path) as archive:
            infos = archive.infolist()
            if sum(info.file_size for info in infos) > 50 * 1024 * 1024:
                raise ReportError("Template uncompressed size exceeds 50 MiB")
            if len({info.filename for info in infos}) != len(infos):
                raise ReportError("Duplicate ZIP entry in template")
            self.parts = {info.filename: archive.read(info) for info in infos}
        if "xl/workbook.xml" not in self.parts:
            raise ReportError("Template is not an XLSX workbook")
        workbook = ET.fromstring(self.parts["xl/workbook.xml"])
        properties = workbook.find(S + "workbookPr")
        if properties is not None and properties.get("date1904") in ("1", "true"):
            raise ReportError("Template must use the Excel 1900 date system")
        rels = ET.fromstring(self.parts["xl/_rels/workbook.xml.rels"])
        targets = {rel.get("Id"): rel.get("Target") for rel in rels}
        self.paths, self.sheets = {}, {}
        for sheet in workbook.find(S + "sheets"):
            target = targets[sheet.get(R + "id")]
            path = target.lstrip("/") if target.startswith("/") else posixpath.normpath("xl/" + target)
            self.paths[sheet.get("name")] = path
            self.sheets[sheet.get("name")] = ET.fromstring(self.parts[path])
        self.shared = []
        if "xl/sharedStrings.xml" in self.parts:
            self.shared = ["".join(node.itertext()) for node in ET.fromstring(self.parts["xl/sharedStrings.xml"])]
        for name, row, headers in [("Traces", 10, TRACE_HEADERS), ("Timeline", 8, TIMELINE_HEADERS),
                                   ("Dependencies", 8, DEPENDENCY_HEADERS), ("Spans", 8, SPAN_HEADERS)]:
            if name not in self.sheets:
                raise ReportError(f"Template sheet is missing: {name}")
            actual = [self.value(name, f"{column_name(index)}{row}") for index in range(1, len(headers) + 1)]
            if actual != headers:
                raise ReportError(f"Template headers do not match the contract: {name} row {row}")
        self.prototypes = {}
        for name, row in [("Traces", 11), ("Timeline", 9), ("Timeline", 10), ("Dependencies", 9), ("Spans", 9)]:
            node = self.sheets[name].find(f"{S}sheetData/{S}row[@r='{row}']")
            if node is None:
                raise ReportError(f"Template prototype row missing: {name} row {row}")
            self.prototypes[name, row] = copy.deepcopy(node)
        self.bar_styles = {state: self.style("Timeline", address) for state, address in
                           [("OK", "B5"), ("ERROR", "C5"), ("FAULT", "D5"), ("THROTTLE", "E5")]}
        self.truncated_cells = 0

    def value(self, name, address):
        return cell_text(self.sheets[name].find(f"{S}sheetData/{S}row/{S}c[@r='{address}']"), self.shared)

    def style(self, name, address):
        cell = self.sheets[name].find(f"{S}sheetData/{S}row/{S}c[@r='{address}']")
        if cell is None or cell.get("s") is None:
            raise ReportError(f"Template style cell missing: {name}!{address}")
        return cell.get("s")

    def cell(self, address, value, style=None):
        node = ET.Element(S + "c", {"r": address})
        if style is not None:
            node.set("s", style)
        if value is None:
            return node
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            if not math.isfinite(value):
                raise ReportError("Non-finite numeric value in workbook")
            ET.SubElement(node, S + "v").text = str(value)
        else:
            value = text(value)
            encoded = value.encode("utf-16-le")
            if len(encoded) > 65534:
                suffix = " [truncated; see spans.csv / raw-results.jsonl]"
                value = encoded[:60000].decode("utf-16-le", errors="ignore") + suffix
                self.truncated_cells += 1
            # Escape literal OOXML escape sequences before encoding XML control characters.
            value = re.sub(r"_x[0-9a-fA-F]{4}_", lambda match: "_x005F_" + match.group()[1:], value)
            value = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\ufffe\uffff]", lambda match: f"_x{ord(match.group()):04X}_", value)
            node.set("t", "inlineStr")
            string = ET.SubElement(ET.SubElement(node, S + "is"), S + "t")
            string.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
            string.text = value
        return node

    def put(self, name, address, value):
        data = self.sheets[name].find(S + "sheetData")
        number = re.search(r"[0-9]+$", address).group()
        row = data.find(f"{S}row[@r='{number}']")
        if row is None:
            raise ReportError(f"Template metadata row missing: {name} row {number}")
        old = row.find(f"{S}c[@r='{address}']")
        style = old.get("s") if old is not None else None
        if old is not None:
            row.remove(old)
        row.append(self.cell(address, value, style))
        row[:] = sorted(row, key=lambda item: column_number(item.get("r")))

    def reset_data(self, name, start_row):
        root = self.sheets[name]
        data = root.find(S + "sheetData")
        for row in list(data):
            if int(row.get("r")) >= start_row:
                data.remove(row)
        for tag in ("autoFilter", "hyperlinks"):
            node = root.find(S + tag)
            if node is not None:
                root.remove(node)
        merges = root.find(S + "mergeCells")
        if merges is not None:
            for merge in list(merges):
                if max(int(number) for number in re.findall(r"\d+", merge.get("ref"))) >= start_row:
                    merges.remove(merge)
            merges.set("count", str(len(merges)))

    def add_row(self, name, number, values, prototype, depth=0, overrides=None):
        if number > 1_048_576:
            raise ReportError(f"Excel row limit exceeded in {name}; use a shorter query interval")
        source = self.prototypes[name, prototype]
        properties = {key: value for key, value in source.attrib.items() if key not in {"r", "spans", "hidden", "outlineLevel"}}
        properties["r"] = str(number)
        if depth:
            properties["outlineLevel"] = str(min(depth, 7))
        row = ET.Element(S + "row", properties)
        styles = {column_number(cell.get("r")): cell.get("s") for cell in source}
        for index, value in enumerate(values, 1):
            style = (overrides or {}).get(index, styles.get(index))
            row.append(self.cell(f"{column_name(index)}{number}", value, style))
        self.sheets[name].find(S + "sheetData").append(row)

    def finish_sheet(self, name, last_row, columns, filter_row=None):
        root = self.sheets[name]
        dimension = root.find(S + "dimension")
        if dimension is None:
            dimension = ET.Element(S + "dimension")
            root.insert(1 if root.find(S + "sheetPr") is not None else 0, dimension)
        dimension.set("ref", f"A1:{column_name(columns)}{last_row}")
        # Insert schema-ordered optional children immediately after sheetData.
        if filter_row is not None:
            node = ET.Element(S + "autoFilter", {"ref": f"A{filter_row}:{column_name(columns)}{max(filter_row, last_row)}"})
            root.insert(list(root).index(root.find(S + "sheetData")) + 1, node)
        view = root.find(f"{S}sheetViews/{S}sheetView")
        if view is not None:
            pane = view.find(S + "pane")
            if pane is not None:
                view.remove(pane)
            y_split = 8 if name != "Traces" else 10
            pane = ET.Element(S + "pane", {"xSplit": "2" if name == "Timeline" else "1",
                                           "ySplit": str(y_split), "topLeftCell": f"{'C' if name == 'Timeline' else 'B'}{y_split + 1}",
                                           "activePane": "bottomRight", "state": "frozen"})
            view.insert(0, pane)
            for selection in view.findall(S + "selection"):
                view.remove(selection)
            ET.SubElement(view, S + "selection", {"pane": "bottomRight", "activeCell": pane.get("topLeftCell"), "sqref": pane.get("topLeftCell")})

    def save(self, path):
        for name in ("Traces", "Timeline", "Dependencies", "Spans"):
            self.parts[self.paths[name]] = ET.tostring(self.sheets[name], encoding="utf-8", xml_declaration=True)
        with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for name, content in self.parts.items():
                archive.writestr(name, content)


def timeline_buckets(span, trace, count=40):
    elapsed = trace.end - trace.start
    offset = span.start - trace.start
    if elapsed <= 0:
        return {0}
    end = (span.end if span.end is not None else span.start) - trace.start
    first = min(count - 1, max(0, offset * count // elapsed))
    # The right endpoint is exclusive. A zero-duration span still has one marker.
    last = min(count - 1, max(first, (end * count - 1) // elapsed))
    return set(range(first, last + 1))


def spans_from_csv(path):
    """Reload the exported CSV as the template's input, retaining literal source text.

    The JSON column is authoritative so CSV formula-protection prefixes cannot
    accidentally change operation names or other labels in the XLSX report.
    """
    csv.field_size_limit(16 * 1024 * 1024)
    spans = []
    with Path(path).open(encoding="utf-8-sig", newline="") as source:
        reader = csv.DictReader(source)
        if reader.fieldnames != CSV_HEADERS:
            raise ReportError("CSV headers do not match the export contract")
        for number, row in enumerate(reader, 2):
            try:
                spans.append(normalize({"@message": row["raw_message_json"], "@timestamp": row["log_timestamp_utc"], "@ptr": ""}))
            except (ReportError, ValueError, TypeError) as exc:
                raise ReportError(f"Invalid exported span CSV at row {number}") from exc
    return spans


def write_workbook(template, traces, spans, metadata, output):
    period = f"{iso_ns(metadata['start_ms'] * 1_000_000)} ～ {iso_ns(metadata['end_ms'] * 1_000_000)}"
    for name in ("Traces", "Timeline", "Dependencies", "Spans"):
        template.put(name, "B4" if name == "Traces" else "A4", period)
    template.put("Traces", "B5", metadata["region"])
    template.put("Traces", "D5", metadata["log_group"])
    for address, value in [("B6", len(traces)), ("D6", len(spans)),
                           ("F6", sum(span.status in ERROR_STATES for span in spans)),
                           ("H6", sum("親スパン未取得" in span.issues for span in spans)),
                           ("J6", len(metadata["queries"]))]:
        template.put("Traces", address, value)
    for name, start in [("Traces", 11), ("Timeline", 9), ("Dependencies", 9), ("Spans", 9)]:
        template.reset_data(name, start)
    timeline_row = 9
    links = []
    for number, trace in enumerate(traces, 11):
        roots = trace.roots or [trace.spans[0]]
        template.add_row("Traces", number, [
            trace.id, ", ".join(dict.fromkeys(span.service for span in roots)),
            ", ".join(dict.fromkeys(span.name for span in roots)) if trace.roots else "(ルート未取得)",
            excel_date(trace.start), trace.duration_ms, len(trace.spans),
            sum(span.status == "FAULT" for span in trace.spans),
            sum(span.status == "ERROR" for span in trace.spans),
            sum(span.status == "THROTTLE" for span in trace.spans),
            sum("親スパン未取得" in span.issues for span in trace.spans), "; ".join(trace.notes),
        ], 11)
        links.append((f"A{number}", f"'Timeline'!A{timeline_row}"))
        # Group rows expose each trace's own scale, with exact offsets in column E.
        values = [trace.id, "取得分の経過時間", "", None, 0, trace.duration_ms] + [None] * 40
        for quarter, col in enumerate((7, 17, 27, 37)):
            values[col - 1] = f"{trace.duration_ms * quarter / 4:,.3f} ms"
        template.add_row("Timeline", timeline_row, values, 9)
        merges = template.sheets["Timeline"].find(S + "mergeCells")
        for first, last in ((7, 16), (17, 26), (27, 36), (37, 46)):
            ET.SubElement(merges, S + "mergeCell", {"ref": f"{column_name(first)}{timeline_row}:{column_name(last)}{timeline_row}"})
        merges.set("count", str(len(merges)))
        timeline_row += 1
        for span in trace.spans:
            bar_style = template.bar_styles.get(span.status, template.bar_styles["OK"])
            overrides = {7 + index: bar_style for index in timeline_buckets(span, trace)}
            overrides[3] = bar_style
            template.add_row("Timeline", timeline_row, [
                "  " * min(span.depth, 20) + span.name, span.service, span.status, span.http_status,
                (span.start - trace.start) / 1_000_000, span.duration_ms,
                *([None] * 40), trace.id, span.id, span.parent, span.kind, span.depth, "; ".join(span.issues),
            ], 10, depth=span.depth, overrides=overrides)
            timeline_row += 1
    if not traces:
        template.add_row("Traces", 11, ["対象時間範囲にスパンはありません"], 11)
        template.add_row("Timeline", 9, ["対象時間範囲にスパンはありません"], 9)
        timeline_row = 10
    template.finish_sheet("Traces", max(11, len(traces) + 10), 11, filter_row=10)
    if links:
        root = template.sheets["Traces"]
        hyperlinks = ET.Element(S + "hyperlinks")
        for ref, location in links:
            ET.SubElement(hyperlinks, S + "hyperlink", {"ref": ref, "location": location})
        # hyperlinks must follow mergeCells/conditionalFormatting, before page settings.
        before = {"printOptions", "pageMargins", "pageSetup", "headerFooter", "drawing", "extLst"}
        index = next((i for i, node in enumerate(root) if node.tag.rsplit("}", 1)[-1] in before), len(root))
        root.insert(index, hyperlinks)
    template.finish_sheet("Timeline", timeline_row - 1, 52)
    rows = dependencies(traces)
    for number, values in enumerate(rows, 9):
        template.add_row("Dependencies", number, values, 9)
    if not rows:
        template.add_row("Dependencies", 9, ["取得分にサービス間の親子スパンはありません"], 9)
    template.finish_sheet("Dependencies", max(9, len(rows) + 8), 9, filter_row=8)
    for number, span in enumerate(spans, 9):
        template.add_row("Spans", number, span.values(excel=True), 9)
    if not spans:
        template.add_row("Spans", 9, ["対象時間範囲にスパンはありません"], 9)
    template.finish_sheet("Spans", max(9, len(spans) + 8), len(SPAN_HEADERS), filter_row=8)
    template.save(output)


def render(args):
    template = Template(args.template)
    if args.start_ms < 0 or args.end_ms <= args.start_ms:
        raise ReportError("Invalid time interval")
    selected_trace = canonical_trace_id(args.trace_id) if args.trace_id else ""
    records, spans, seen, duplicates, excluded = [], [], {}, 0, 0
    with Path(args.events).open(encoding="utf-8-sig") as source:
        for line_number, line in enumerate(source, 1):
            try:
                record = json.loads(line)
                span = normalize(record)
            except (ReportError, ValueError, TypeError, OverflowError) as exc:
                raise ReportError(f"Invalid span at raw-results.jsonl line {line_number}: {type(exc).__name__}: {exc if isinstance(exc, ReportError) else 'invalid field type or value'}") from exc
            if not args.start_ms <= span.log_ms < args.end_ms or (selected_trace and span.trace != selected_trace):
                excluded += 1
                continue
            key = (span.trace, span.id)
            if key in seen:
                if json.loads(seen[key]) != json.loads(span.message):
                    raise ReportError(f"Conflicting records for the same trace/span ID at line {line_number}; no report exported")
                duplicates += 1
                continue
            seen[key] = span.message
            records.append(record)
            spans.append(span)
    traces = build_traces(spans)
    ordered = [span for trace in traces for span in trace.spans]
    with Path(args.queries).open(encoding="utf-8") as source:
        queries = [json.loads(line) for line in source if line.strip()]
    metadata = {
        "report_version": 1, "source": args.source, "region": args.region, "log_group": args.log_group,
        "start_ms": args.start_ms, "end_ms": args.end_ms, "trace_id_filter": selected_trace,
        "timestamp_selection": "log-event @timestamp, inclusive start / exclusive end (UTC)",
        "trace_coverage": "Only spans whose log-event timestamp is in the interval; full trace coverage is not guaranteed",
        "trace_count": len(traces), "span_count": len(spans), "duplicate_records_removed": duplicates,
        "records_excluded_by_time_or_trace": excluded,
        "spans_with_issues": sum(bool(span.issues) for span in spans),
        "bytes_scanned_including_split_queries": sum(query.get("statistics", {}).get("bytesScanned", 0) for query in queries),
        "queries": queries,
    }
    output_dir = Path(args.output_dir)
    with (output_dir / "spans.csv").open("w", encoding="utf-8-sig", newline="") as target:
        writer = csv.writer(target)
        writer.writerow(CSV_HEADERS)
        for span in ordered:
            writer.writerow([csv_safe(value) for value in span.values() + [span.pointer, span.message]])
    # Excel is populated from the completed CSV, which is also delivered for reuse.
    csv_spans = spans_from_csv(output_dir / "spans.csv")
    csv_traces = build_traces(csv_spans)
    write_workbook(template, csv_traces, [span for trace in csv_traces for span in trace.spans], metadata, output_dir / "report.xlsx")
    metadata["excel_truncated_cells"] = template.truncated_cells
    with (output_dir / "raw-results.jsonl").open("w", encoding="utf-8", newline="\n") as target:
        for record in records:
            target.write(json_text(record) + "\n")
    with (output_dir / "query-manifest.json").open("w", encoding="utf-8", newline="\n") as target:
        json.dump(metadata, target, ensure_ascii=False, indent=2, allow_nan=False)
        target.write("\n")
    print(f"Spans: {len(spans)}; traces: {len(traces)}; spans with issues: {metadata['spans_with_issues']}", file=sys.stderr)
    if template.truncated_cells:
        print(f"WARNING: {template.truncated_cells} Excel cells exceeded the cell text limit; full text is in CSV/JSONL", file=sys.stderr)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    times = sub.add_parser("time-range")
    times.add_argument("start")
    times.add_argument("end")
    trace = sub.add_parser("trace-id")
    trace.add_argument("value")
    validate = sub.add_parser("validate-template")
    validate.add_argument("template")
    report = sub.add_parser("render")
    for name in ("events", "queries", "template", "output-dir", "region", "log-group", "source"):
        report.add_argument("--" + name, required=True)
    report.add_argument("--start-ms", type=int, required=True)
    report.add_argument("--end-ms", type=int, required=True)
    report.add_argument("--trace-id", default="")
    args = parser.parse_args(argv)
    try:
        if args.command == "time-range":
            start, end = parse_time(args.start), parse_time(args.end)
            if end <= start:
                raise ReportError("--end must be later than --start")
            print(start, end)
        elif args.command == "trace-id":
            print(canonical_trace_id(args.value))
        elif args.command == "validate-template":
            Template(args.template)
        else:
            render(args)
    except (ReportError, OSError, ValueError, OverflowError, KeyError, ET.ParseError, zipfile.BadZipFile) as exc:
        # Do not echo raw messages, attributes, CLI credentials or query payloads.
        message = str(exc) if isinstance(exc, ReportError) else f"{type(exc).__name__}: check input paths, JSON and template format"
        print("ERROR: " + message, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
