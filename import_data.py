"""Import the two independent expense reports without comparing or merging them."""
from __future__ import annotations
import hashlib
import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from zipfile import BadZipFile, ZipFile
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parent
DATA = ROOT / 'data'
SOURCE = ROOT.parent / '三大表校验'
NS = '{http://schemas.openxmlformats.org/spreadsheetml/2006/main}'
AMOUNTS = {'原币借方', '原币贷方', '人民币借方', '人民币贷方', '本期费用', '汇率'}
SOURCES = [('before', '结算前费用', 'CUX_管理集团费用明细___0904_结算前.xlsx'),
           ('after', '结算后费用', 'CUX_管理集团费用明细___0907_结算后.xls')]

def now():
    return datetime.now(timezone.utc).isoformat()

def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + '.tmp')
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    os.replace(temp, path)

def quote(name):
    return '"' + name.replace('"', '""') + '"'

def file_hash(path):
    h = hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()

def column_index(ref):
    letters = re.match(r'[A-Z]+', ref).group()
    n = 0
    for ch in letters:
        n = n * 26 + ord(ch) - 64
    return n - 1

def read_ooxml_rows(path, sheet_name=None):
    """Resolve OOXML sheet relationship and cell coordinates, including sparse cells."""
    with ZipFile(path) as z:
        strings = []
        if 'xl/sharedStrings.xml' in z.namelist():
            with z.open('xl/sharedStrings.xml') as stream:
                for _, item in ET.iterparse(stream, events=('end',)):
                    if item.tag == NS + 'si':
                        strings.append(''.join(t.text or '' for t in item.iter(NS + 't')))
                        item.clear()
        workbook = ET.fromstring(z.read('xl/workbook.xml'))
        sheets = workbook.find(NS + 'sheets')
        if sheets is None or not list(sheets):
            raise ValueError('Excel 文件没有可读取的工作表。')
        available = list(sheets)
        sheet = next((item for item in available if item.get('name') == sheet_name), None) if sheet_name else None
        if sheet is None:
            # Many workbooks carry a helper sheet such as FormulaList after the
            # report. The first sheet is the workbook's primary report by order.
            sheet = available[0]
        relation_id = sheet.get('{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id')
        rels = ET.fromstring(z.read('xl/_rels/workbook.xml.rels'))
        target = next(x.get('Target') for x in rels if x.get('Id') == relation_id)
        target = target.lstrip('/') if target.startswith('/') else 'xl/' + target
        yield ('sheet', sheet.get('name'))
        with z.open(target) as stream:
            context = ET.iterparse(stream, events=('start', 'end'))
            _, root = next(context)
            for event, row in context:
                if event != 'end' or row.tag != NS + 'row':
                    continue
                vals = {}
                for cell in row.findall(NS + 'c'):
                    idx = column_index(cell.get('r'))
                    value = cell.find(NS + 'v')
                    text = value.text if value is not None else None
                    kind = cell.get('t')
                    if kind == 's' and text is not None:
                        text = strings[int(text)]
                    elif kind == 'inlineStr':
                        text = ''.join(t.text or '' for t in cell.iter(NS + 't'))
                    elif kind == 'e':
                        raise ValueError(f'Excel 错误值：第 {row.get("r")} 行 {cell.get("r")}')
                    if cell.find(NS + 'f') is not None and value is None:
                        raise ValueError(f'第 {row.get("r")} 行公式没有缓存值，请在 Excel 中重算后保存。')
                    vals[idx] = text
                yield (int(row.get('r')), vals)
                row.clear()
                root.clear()

def read_xls_rows(path, sheet_name=None):
    """Read legacy BIFF .xls workbooks while preserving the row/column contract."""
    import xlrd
    book = xlrd.open_workbook(path, on_demand=True)
    try:
        if book.nsheets < 1:
            raise ValueError('Excel 文件没有可读取的工作表。')
        index = book.sheet_names().index(sheet_name) if sheet_name and sheet_name in book.sheet_names() else 0
        sheet = book.sheet_by_index(index)
        yield ('sheet', sheet.name)
        for row_index in range(sheet.nrows):
            values = {}
            for column_index_value in range(sheet.ncols):
                cell = sheet.cell(row_index, column_index_value)
                if cell.ctype in (xlrd.XL_CELL_EMPTY, xlrd.XL_CELL_BLANK):
                    continue
                if cell.ctype == xlrd.XL_CELL_ERROR:
                    raise ValueError(f'Excel 错误值：第 {row_index + 1} 行第 {column_index_value + 1} 列')
                if cell.ctype == xlrd.XL_CELL_DATE:
                    value = xlrd.xldate_as_datetime(cell.value, book.datemode).isoformat(sep=' ')
                elif cell.ctype == xlrd.XL_CELL_NUMBER:
                    value = str(int(cell.value)) if cell.value.is_integer() else format(cell.value, '.15g')
                elif cell.ctype == xlrd.XL_CELL_BOOLEAN:
                    value = 'TRUE' if cell.value else 'FALSE'
                else:
                    value = str(cell.value)
                values[column_index_value] = value
            yield (row_index + 1, values)
    finally:
        book.release_resources()

def read_rows(path, sheet_name=None):
    """Read OOXML workbooks and genuine legacy .xls files through one iterator."""
    try:
        yield from read_ooxml_rows(path, sheet_name=sheet_name)
    except BadZipFile:
        yield from read_xls_rows(path, sheet_name=sheet_name)

def import_one(dataset_id, name, filename, force=False, source_path=None, sheet_name=None):
    path = Path(source_path) if source_path else SOURCE / filename
    directory = DATA / dataset_id
    directory.mkdir(parents=True, exist_ok=True)
    manifest_path = directory / 'manifest.json'
    database = directory / 'warehouse.duckdb'
    write_json(DATA / 'import_status.json', {'dataset_id': dataset_id, 'status': 'importing', 'phase': '核对文件指纹', 'row_count': 0, 'updated_at': now()})
    digest = file_hash(path)
    if not force and database.exists() and manifest_path.exists():
        old = json.loads(manifest_path.read_text(encoding='utf-8'))
        if old.get('sha256') == digest and old.get('importer_version') == 2:
            print(f'{dataset_id}: already imported ({old["row_count"]} rows)', flush=True)
            return old
    import duckdb
    import pyarrow as pa
    building = directory / 'building.duckdb'
    if building.exists():
        building.unlink()
    con = duckdb.connect(str(building))
    header = None
    scales = {}
    integers = {}
    text_numeric = set()
    duplicate_columns = []
    periods = set()
    count = 0
    buffer = []
    status = {'dataset_id': dataset_id, 'status': 'importing', 'phase': '读取报表', 'row_count': 0, 'updated_at': now()}
    write_json(DATA / 'import_status.json', status)
    try:
        def flush():
            nonlocal buffer
            if not buffer:
                return
            schema = pa.schema([('_source_row', pa.int64())] + [(x, pa.string()) for x in header])
            values = {key: [r[i] for r in buffer] for i, key in enumerate(schema.names)}
            con.register('_batch', pa.Table.from_pydict(values, schema=schema))
            con.execute('INSERT INTO staging SELECT * FROM _batch')
            con.unregister('_batch')
            buffer = []
            write_json(DATA / 'import_status.json', {**status, 'row_count': count, 'updated_at': now()})
        for rownum, values in read_rows(path, sheet_name=sheet_name):
            if rownum == 'sheet':
                sheet_name = values
                continue
            if header is None:
                raw_header = [values.get(i) for i in range(max(values) + 1)]
                if any(not x for x in raw_header) or '_source_row' in raw_header:
                    raise ValueError('表头有空值或使用了保留字段，不能自动导入。')
                # Excel allows duplicate labels. DuckDB needs unique column
                # identifiers, so retain every column with a deterministic suffix.
                seen = {}
                header = []
                for label in raw_header:
                    base = str(label).strip()
                    seen[base] = seen.get(base, 0) + 1
                    if seen[base] > 1:
                        duplicate_columns.append(f'{base} → {base}__{seen[base]}')
                    header.append(base if seen[base] == 1 else f'{base}__{seen[base]}')
                con.execute('CREATE TABLE staging (_source_row BIGINT, ' + ','.join(quote(x) + ' VARCHAR' for x in header) + ')')
                continue
            if not any(v not in (None, '') for v in values.values()):
                continue
            if max(values, default=-1) >= len(header):
                raise ValueError(f'第 {rownum} 行有超出表头的字段。')
            row = [values.get(i) for i in range(len(header))]
            for i, field in enumerate(header):
                value = row[i]
                if field == '期间' and value:
                    periods.add(value)
                if field not in AMOUNTS or value is None or not str(value).strip():
                    if field in AMOUNTS:
                        row[i] = None
                    continue
                try:
                    number = Decimal(value)
                    if not number.is_finite():
                        raise InvalidOperation()
                except InvalidOperation:
                    raise ValueError(f'第 {rownum} 行「{field}」不能转换为金额。') from None
                scales[field] = max(scales.get(field, 10), max(0, -number.as_tuple().exponent))
                integers[field] = max(integers.get(field, 0), max(0, number.adjusted() + 1))
                if scales[field] + integers[field] > 38:
                    text_numeric.add(field)
            count += 1
            buffer.append([rownum] + row)
            if len(buffer) >= 8000:
                flush()
                print(f'{dataset_id}: {count} rows', flush=True)
        flush()
        if not header or not count:
            raise ValueError('文件没有数据行。')
        write_json(DATA / 'import_status.json', {**status, 'phase': '转换字段并保存数据', 'row_count': count, 'updated_at': now()})
        columns = [{'name': '_source_row', 'type': 'BIGINT'}] + [
            {'name': x, 'type': f'DECIMAL(38,{scales.get(x, 10)})' if x in AMOUNTS and x not in text_numeric else 'VARCHAR'} for x in header]
        selects = [f'CAST({quote(c["name"])} AS {c["type"]}) AS {quote(c["name"])}' for c in columns]
        con.execute('CREATE TABLE raw_expense AS SELECT ' + ','.join(selects) + ' FROM staging')
        con.execute('DROP TABLE staging')
        if con.execute('SELECT COUNT(*) FROM raw_expense').fetchone()[0] != count:
            raise ValueError('导入行数核对失败。')
        con.execute('CHECKPOINT')
        con.close()
        manifest = {'id': dataset_id, 'name': name, 'stage': dataset_id, 'source_filename': path.name,
                    'source_path': str(path), 'source_sheet': sheet_name, 'sha256': digest,
                    'source_sha256': digest, 'periods': sorted(periods), 'row_count': count,
                    'columns': columns, 'source_column_count': len(header), 'imported_at': now(),
                    'status': 'ready', 'importer_version': 2,
                    'actual_format': 'xls / BIFF' if path.suffix.lower() == '.xls' else 'xlsx / OOXML',
                    'import_notes': ([f'重复列名已加后缀：{x}。' for x in duplicate_columns] +
                                     [f'「{x}」超出 DECIMAL(38) 精度，按原文本保留，未舍入。' for x in sorted(text_numeric)])}
        os.replace(building, database)
        write_json(manifest_path, manifest)
        write_json(DATA / 'import_status.json', {**status, 'status': 'completed', 'row_count': count, 'updated_at': now()})
        print(f'{dataset_id}: completed {count} rows, {len(header)} source columns', flush=True)
        return manifest
    except Exception as exc:
        con.close()
        write_json(DATA / 'import_status.json', {**status, 'status': 'failed', 'error': str(exc), 'updated_at': now()})
        raise

if __name__ == '__main__':
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser()
    parser.add_argument('--dataset')
    parser.add_argument('--name')
    parser.add_argument('--source')
    parser.add_argument('--sheet', default='')
    parser.add_argument('--force', action='store_true')
    args = parser.parse_args()
    if args.dataset:
        if args.name:
            import_one(args.dataset, args.name, Path(args.source).name, force=args.force, source_path=args.source, sheet_name=args.sheet or None)
        else:
            spec = next(item for item in SOURCES if item[0] == args.dataset)
            import_one(*spec, force=args.force, source_path=args.source)
    else:
        for spec in SOURCES:
            import_one(*spec, force=args.force)
