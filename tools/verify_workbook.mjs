// Read-only visual and value inspection of a populated report.
import fs from 'node:fs/promises';
import path from 'node:path';
import { FileBlob, SpreadsheetFile } from '@oai/artifact-tool';

const filename = process.argv[2];
if (!filename) throw new Error('Usage: node tools/verify_workbook.mjs REPORT.xlsx');
const wb = await SpreadsheetFile.importXlsx(await FileBlob.load(filename));
const dir = path.resolve('.work/populated-previews');
await fs.mkdir(dir, { recursive: true });
console.log((await wb.inspect({ kind: 'table', range: 'Traces!A10:K13', include: 'values,formulas', tableMaxRows: 4, tableMaxCols: 11, maxChars: 5000 })).ndjson);
console.log((await wb.inspect({ kind: 'table', range: 'Dependencies!A8:H12', include: 'values', tableMaxRows: 5, tableMaxCols: 8, maxChars: 2500 })).ndjson);
// The preview renderer formats digit-only strings as numbers even with @ format.
// A display-only LRM preserves the original text appearance in PNGs. This workbook
// is never saved: the XLSX itself retains the unmodified, typed string identifiers.
for (const [name, address] of [['Traces', 'A11:A13'], ['Timeline', 'A9:A20'], ['Spans', 'A9:C13']]) {
  const range = wb.worksheets.getItem(name).getRange(address);
  range.values = range.values.map(row => row.map(value => typeof value === 'string' && /^[0-9]+$/.test(value) ? '\u200e' + value : value));
}
for (const [name, range] of [['Traces', 'A1:K13'], ['Timeline', 'A1:AT20'], ['Dependencies', 'A1:I12'], ['Spans', 'A1:M13']]) {
  const image = await wb.render({ sheetName: name, range, scale: 1, format: 'png' });
  await fs.writeFile(path.join(dir, `${name}.png`), new Uint8Array(await image.arrayBuffer()));
}
console.log((await wb.inspect({ kind: 'match', searchTerm: '#REF!|#DIV/0!|#VALUE!|#NAME\\?|#NUM!', options: { useRegex: true, maxResults: 20 }, maxChars: 1200 })).ndjson);
