// Development-only builder. RHEL runtime does not need Node or artifact-tool.
import fs from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { Workbook, SpreadsheetFile } from '@oai/artifact-tool';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const previewDir = path.join(root, '.work', 'previews');
await fs.mkdir(previewDir, { recursive: true });
await fs.mkdir(path.join(root, 'templates'), { recursive: true });
const wb = Workbook.create();
const fontFamily = 'Meiryo UI';
const palette = { ink: '#202020', header: '#333333', normal: '#D9D9D9', error: '#A6A6A6', fault: '#494949', throttle: '#707070', line: '#BDBDBD', muted: '#595959', gray: '#F0F0F0' };
const traceHeaders = ['Trace ID', 'ルートサービス', 'ルート操作', '開始 (UTC)', '経過時間 (ms)', 'スパン数', 'Fault', 'Error', 'Throttle', '親未取得', '取得分の注意事項'];
const timelineHeaders = ['スパン / 親子階層', 'サービス', '状態', 'HTTP', '開始差 (ms)', '時間 (ms)'];
const dependencyHeaders = ['呼び出し元', '呼び出し先', '呼び出し数', 'トレース数', '異常数', '平均 (ms)', 'p95 (ms)', '最大 (ms)', '備考'];
const spanHeaders = ['Trace ID', 'Span ID', 'Parent span ID', 'Service', 'Name', 'Kind', 'Start (UTC)', 'End (UTC)', 'Duration (ms)', 'Status', 'HTTP method', 'HTTP URL', 'HTTP status', 'Remote service', 'Remote operation', 'Status message', 'Resource attributes (JSON)', 'Attributes (JSON)', 'Events (JSON)', 'Links (JSON)', 'Start (ns)', 'End (ns)', 'Log timestamp (UTC)', 'Issues'];

function col(index) {
  let result = '';
  while (index > 0) { const digit = (index - 1) % 26; result = String.fromCharCode(65 + digit) + result; index = Math.floor((index - 1) / 26); }
  return result;
}
function base(name, title, lastColumn, lastRow) {
  const sheet = wb.worksheets.add(name);
  sheet.showGridLines = false;
  sheet.getRange(`A1:${lastColumn}${lastRow}`).format = { font: { name: fontFamily, size: 10, color: palette.ink }, rowHeight: 24, verticalAlignment: 'center' };
  sheet.getRange('A2').values = [[title]];
  sheet.getRange('A2').format.font = { name: fontFamily, size: 14, bold: true, color: palette.ink };
  sheet.getRange(`A3:${lastColumn}3`).format.borders = { bottom: { style: 'thin', color: palette.line } };
  sheet.getRange('A1').format.rowHeight = 9;
  sheet.tabColor = palette.header;
  return sheet;
}
function headers(sheet, row, values) {
  const range = sheet.getRange(`A${row}:${col(values.length)}${row}`);
  range.values = [values];
  range.format = { fill: palette.header, font: { name: fontFamily, size: 10, color: '#FFFFFF', bold: true }, horizontalAlignment: 'center', verticalAlignment: 'center', rowHeight: 32, wrapText: true };
}
function widths(sheet, values) {
  values.forEach((width, i) => { sheet.getRange(`${col(i + 1)}1:${col(i + 1)}13`).format.columnWidth = width; });
}
function context(sheet, range) {
  sheet.getRange(range).merge();
  sheet.getRange(range.split(':')[0]).values = [['実行時に指定したログ時刻範囲をUTCで表示します。開始を含み、終了を含みません。']];
  sheet.getRange(range).format.font = { name: fontFamily, size: 10, color: palette.muted };
}

const traces = base('Traces', 'トレース一覧', 'K', 11);
widths(traces, [39, 25, 34, 27, 16, 10, 9, 9, 10, 11, 42]);
traces.getRange('A4').values = [['ログ時刻範囲']];
context(traces, 'B4:K4');
traces.getRange('A5:D5').values = [['リージョン', '実行時に設定', 'ロググループ', 'aws/spans']];
traces.getRange('A6:J6').values = [['トレース数', 0, 'スパン数', 0, '異常スパン数', 0, '親未取得', 0, 'クエリ数', 0]];
traces.getRange('A6:J6').format.fill = palette.gray;
for (const address of ['B6', 'D6', 'F6', 'H6', 'J6']) traces.getRange(address).format.font = { name: fontFamily, size: 10, bold: true, color: palette.ink };
traces.getRange('A7:K7').merge();
traces.getRange('A7').values = [['対象ログ時刻範囲で取得したスパンのみを表示します。トレース全体の完全性は保証されません。Trace IDからタイムラインに移動できます。']];
traces.getRange('A7:K7').format = { font: { name: fontFamily, size: 10, color: palette.muted }, wrapText: true, rowHeight: 32 };
headers(traces, 10, traceHeaders);
traces.getRange('A11:K11').values = [['', '', '', null, null, null, null, null, null, null, '']];
traces.getRange('A11').setNumberFormat('@');
traces.getRange('A11:C11').format.horizontalAlignment = 'left';
traces.getRange('D11:J11').format.horizontalAlignment = 'right';
traces.getRange('D11').setNumberFormat('yyyy-mm-dd hh:mm:ss.000');
traces.getRange('E11').setNumberFormat('#,##0.000');
traces.getRange('F11:J11').setNumberFormat('#,##0');
traces.freezePanes.freezeRows(10);

const timeline = base('Timeline', 'スパンタイムライン', 'AZ', 11);
widths(timeline, [49, 24, 11, 8, 14, 14, ...Array(40).fill(2.6), 39, 21, 21, 12, 8, 42]);
context(timeline, 'A4:AT4');
timeline.getRange('A5:F5').values = [['状態の凡例', 'OK / UNSET', 'ERROR', 'FAULT', 'THROTTLE', '']];
for (const [address, color, foreground] of [['B5', palette.normal, palette.ink], ['C5', palette.error, palette.ink], ['D5', palette.fault, '#FFFFFF'], ['E5', palette.throttle, '#FFFFFF']]) {
  timeline.getRange(address).format = { fill: color, font: { name: fontFamily, size: 10, color: foreground }, horizontalAlignment: 'center' };
}
timeline.getRange('A6:AT6').merge();
timeline.getRange('A6').values = [['スパンの親子階層と、各トレース内の相対時間を表示します。バーは40区間で表示し、正確な値は開始差と時間で確認します。']];
timeline.getRange('A6:AT6').format.font = { name: fontFamily, size: 10, color: palette.muted };
headers(timeline, 8, timelineHeaders);
timeline.getRange('G8:AT8').format = { fill: palette.header, font: { name: fontFamily, size: 10, bold: true, color: '#FFFFFF' }, horizontalAlignment: 'center', rowHeight: 32 };
for (const [range, label] of [['G8:P8', '0–25%'], ['Q8:Z8', '25–50%'], ['AA8:AJ8', '50–75%'], ['AK8:AT8', '75–100%']]) {
  timeline.getRange(range).merge(); timeline.getRange(range.split(':')[0]).values = [[label]];
}
timeline.getRange('AU8:AZ8').values = [['Trace ID', 'Span ID', 'Parent span ID', 'Kind', 'Depth', '注意事項']];
timeline.getRange('AU8:AZ8').format = { fill: palette.header, font: { name: fontFamily, size: 10, bold: true, color: '#FFFFFF' }, horizontalAlignment: 'center' };
timeline.getRange('A9:AZ9').values = [['']];
timeline.getRange('A9:AZ9').format = { fill: palette.gray, font: { name: fontFamily, size: 10, color: palette.ink, bold: true }, rowHeight: 28 };
timeline.getRange('A9').setNumberFormat('@');
timeline.getRange('A9').format.horizontalAlignment = 'left';
timeline.getRange('G9:AT9').format.horizontalAlignment = 'center';
timeline.getRange('E9:F9').setNumberFormat('#,##0.000');
timeline.getRange('A10:AZ10').values = [['']];
timeline.getRange('A10:B10').format.horizontalAlignment = 'left';
timeline.getRange('C10:D10').format.horizontalAlignment = 'center';
timeline.getRange('E10:F10').format.horizontalAlignment = 'right';
timeline.getRange('AU10:AW10').setNumberFormat('@');
timeline.getRange('AU10:AZ10').format.horizontalAlignment = 'left';
timeline.getRange('E10:F10').setNumberFormat('#,##0.000');
timeline.getRange('D10').setNumberFormat('0');
timeline.getRange('G10:AT10').format.borders = { bottom: { style: 'hair', color: palette.gray } };
timeline.freezePanes.freezeRows(8);

const deps = base('Dependencies', 'サービス間の呼び出し', 'I', 9);
widths(deps, [32, 32, 13, 13, 11, 15, 15, 15, 50]);
context(deps, 'A4:I4');
deps.getRange('A6:I6').merge();
deps.getRange('A6').values = [['取得済み親子スパンのサービスが異なる接続を集計します。時間は呼び出し先のスパン時間、p95はnearest-rank法です。']];
deps.getRange('A6:I6').format.font = { name: fontFamily, size: 10, color: palette.muted };
headers(deps, 8, dependencyHeaders);
deps.getRange('A9:I9').values = [['', '', null, null, null, null, null, null, '']];
deps.getRange('A9:B9').format.horizontalAlignment = 'left';
deps.getRange('C9:H9').format.horizontalAlignment = 'right';
deps.getRange('C9:E9').setNumberFormat('#,##0');
deps.getRange('F9:H9').setNumberFormat('#,##0.000');
deps.freezePanes.freezeRows(8);

const spans = base('Spans', 'スパン詳細', 'X', 9);
widths(spans, [39, 21, 21, 25, 40, 12, 27, 27, 15, 12, 14, 50, 14, 28, 28, 45, 60, 60, 60, 50, 23, 23, 27, 45]);
context(spans, 'A4:M4');
spans.getRange('A6:M6').merge();
spans.getRange('A6').values = [['属性、イベント、リンクはJSON形式です。Excelのセル上限を超える文字列は省略表示し、全文はspans.csvとraw-results.jsonlに保存します。']];
spans.getRange('A6:M6').format.font = { name: fontFamily, size: 10, color: palette.muted };
headers(spans, 8, spanHeaders);
spans.getRange('A9:X9').values = [['']];
spans.getRange('A9:C9').setNumberFormat('@');
spans.getRange('A9:F9').format.horizontalAlignment = 'left';
spans.getRange('G9:I9').format.horizontalAlignment = 'right';
spans.getRange('J9:X9').format.horizontalAlignment = 'left';
spans.getRange('M9').format.horizontalAlignment = 'center';
for (const range of ['G9:H9', 'W9']) spans.getRange(range).setNumberFormat('yyyy-mm-dd hh:mm:ss.000');
spans.getRange('I9').setNumberFormat('#,##0.000');
spans.getRange('M9').setNumberFormat('0');
spans.getRange('U9:V9').setNumberFormat('@');
spans.freezePanes.freezeRows(8);

wb.recalculate();
const errors = await wb.inspect({ kind: 'match', searchTerm: '#REF!|#DIV/0!|#VALUE!|#NAME\\?|#NUM!', options: { useRegex: true, maxResults: 20 }, maxChars: 1500 });
console.log(errors.ndjson);
for (const [name, range] of [['Traces', 'A1:K11'], ['Timeline', 'A1:AT11'], ['Dependencies', 'A1:I9'], ['Spans', 'A1:M9']]) {
  const blob = await wb.render({ sheetName: name, range, scale: 1, format: 'png' });
  await fs.writeFile(path.join(previewDir, `${name}.png`), new Uint8Array(await blob.arrayBuffer()));
}
const file = await SpreadsheetFile.exportXlsx(wb);
await file.save(path.join(root, 'templates', 'trace-report.xlsx'));
await fs.rm(path.join(root, 'templates', 'trace-report.xlsx.inspect.ndjson'), { force: true });
console.log('Created templates/trace-report.xlsx');
