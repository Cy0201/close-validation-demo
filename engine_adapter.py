"""Real Wren semantic planning, followed by isolated read-only DuckDB execution."""
from __future__ import annotations
import base64
import csv
import json
from decimal import Decimal, InvalidOperation
from pathlib import Path
import duckdb
import sqlglot
from sqlglot import exp
from wren.config import WrenConfig
from wren.engine import WrenEngine

FUNCTIONS = set('COUNT SUM AVG MIN MAX ABS ROUND CEIL CEILING FLOOR COALESCE NULLIF IF CASE CAST TRY_CAST EXTRACT DATE_TRUNC STRFTIME STRPTIME TRY_STRPTIME LENGTH CHAR_LENGTH LOWER UPPER TRIM LTRIM RTRIM SUBSTRING SUBSTR REPLACE CONCAT CONCAT_WS SPLIT_PART REGEXP_LIKE REGEXP_MATCHES REGEXP_REPLACE REGEXP_EXTRACT ROW_NUMBER RANK DENSE_RANK LAG LEAD FIRST_VALUE LAST_VALUE NTH_VALUE NTILE PERCENT_RANK CUME_DIST STDDEV STDDEV_SAMP STDDEV_POP VARIANCE VAR_SAMP VAR_POP MEDIAN QUANTILE_CONT QUANTILE_DISC PERCENTILE_CONT PERCENTILE_DISC GREATEST LEAST POWER POW SQRT SIGN MOD IFNULL IIF ISNAN ISFINITE DATE DATE_ADD DATE_DIFF DATEDIFF CURRENT_DATE CURRENT_TIMESTAMP YEAR MONTH DAY DAYOFMONTH WEEK QUARTER STR_POSITION POSITION STARTS_WITH ENDS_WITH CONTAINS LEFT RIGHT ASCII UNICODE BOOL_AND BOOL_OR EVERY ANY_VALUE COUNT_IF SUM_IF GROUP_CONCAT STRING_AGG LISTAGG ARRAY_AGG LIST FILTER ARRAY_CONTAINS ARRAY_LENGTH'.split())

def validate(sql, physical=False, allowed_tables=None, allowed_schemas=None):
    if not isinstance(sql, str) or not sql.strip() or len(sql) > 50000:
        raise ValueError('请填写 SQL，长度不得超过 50,000 字符。')
    statements = [s for s in sqlglot.parse(sql, read='duckdb') if s is not None]
    if len(statements) != 1 or not isinstance(statements[0], (exp.Select, exp.Union, exp.Intersect, exp.Except)):
        raise ValueError('只允许一条 SELECT / WITH 查询，不能修改数据或执行命令。')
    tree = statements[0]
    forbidden = (exp.Insert, exp.Update, exp.Delete, exp.Create, exp.Drop, exp.Command, exp.Copy, exp.Into)
    if any(isinstance(n, forbidden) for n in tree.walk()):
        raise ValueError('查询包含写入或外部操作。')
    ctes = {c.alias_or_name.casefold() for c in tree.find_all(exp.CTE)}
    allowed = set(allowed_tables or ({'raw_expense'} if physical else {'expense'}))
    for table in tree.find_all(exp.Table):
        if not isinstance(table.this, exp.Identifier):
            raise ValueError('不允许表函数、外部文件或网络数据源。')
        if table.name.casefold() not in allowed | ctes:
            raise ValueError('只能查询当前数据集的 expense 模型及本条 SQL 内定义的 CTE。')
        if table.catalog or (table.db and table.db not in set(allowed_schemas or ({'main'} if physical else set()))):
            raise ValueError('不允许跨数据库或跨数据集查询。')
    return tree.sql(dialect='duckdb')

class WrenAdapter:
    def __init__(self, dataset_dir, columns, workspace=None, dataset_id=None, runtime=None):
        self.runtime = runtime or {}
        self.dataset_dir = Path(dataset_dir)
        self.columns = columns
        self.workspace = workspace or [{'id': dataset_id or 'current', 'model_name': 'expense', 'columns': columns, 'directory': str(dataset_dir)}]
        current = next((x for x in self.workspace if x['id'] == dataset_id), self.workspace[0])
        models = [{'name': 'expense', 'tableReference': {'schema': 'ds_' + current['id'], 'table': 'raw_expense'}, 'columns': [{'name': c['name'], 'type': c['type']} for c in columns]}]
        for item in self.workspace:
            models.append({'name': item['model_name'], 'tableReference': {'schema': 'ds_' + item['id'], 'table': 'raw_expense'}, 'columns': [{'name': c['name'], 'type': c['type']} for c in item['columns']]})
        self.manifest = {'catalog': 'wren', 'schema': 'main', 'models': models}
        encoded = base64.b64encode(json.dumps(self.manifest, ensure_ascii=False).encode()).decode()
        self.engine = WrenEngine(encoded, 'duckdb', {}, config=WrenConfig(strict_mode=True), fallback=False)

    def plan(self, sql):
        sql = validate(sql, allowed_tables={'expense'} | {x['model_name'] for x in self.workspace})
        planned = self.engine.dry_plan(sql)
        validate(planned, physical=True, allowed_tables={'raw_expense'}, allowed_schemas={'ds_' + x['id'] for x in self.workspace})
        return planned

    def query(self, sql, limit=100):
        planned = self.plan(sql)
        con = self._connect()
        try:
            total = con.execute(f'SELECT COUNT(*) FROM ({planned}) AS result_count').fetchone()[0]
            cursor = con.execute(f'SELECT * FROM ({planned}) AS result_preview LIMIT ?', [max(1, min(int(limit), 500))])
            names = [c[0] for c in cursor.description]
            rows = [list(r) for r in cursor.fetchall()]
            return {'columns': names, 'rows': rows, 'total_rows': total,
                    'truncated': total > len(rows), 'expanded_sql': planned}
        finally:
            con.close()

    def contract(self, sql, amount_column='校验金额'):
        planned = self.plan(sql)
        con = self._connect()
        try:
            cursor = con.execute(f'SELECT * FROM ({planned}) AS rule_contract LIMIT 0')
            columns = [item[0] for item in cursor.description]
            if amount_column not in columns:
                raise ValueError(f'聚合校验 SQL 必须输出名为「{amount_column}」的金额列；其余列可作为校验维度。')
            return {'columns': columns, 'amount_column': amount_column, 'expanded_sql': planned}
        finally:
            con.close()

    def validate_rule(self, sql, amount_column='校验金额', tolerance='0', preview_limit=200, rule=None):
        planned = self.plan(sql)
        try:
            allowed = abs(Decimal(str(tolerance)))
        except InvalidOperation:
            raise ValueError('容差必须是有效数字。') from None
        compare = None
        if rule is not None:
            op = rule.get('op') if isinstance(rule, dict) else None
            if op not in ('<', '>'):
                raise ValueError('拓展检验操作符仅支持 < 或 >。')
            try:
                target = Decimal(str(rule.get('value')))
            except (InvalidOperation, TypeError):
                raise ValueError('拓展检验阈值必须是有效数字。') from None
            if not target.is_finite():
                raise ValueError('拓展检验阈值必须是有限数字。')
            compare = (op, target)
        con = self._connect()
        try:
            cursor = con.execute(f'SELECT * FROM ({planned}) AS rule_result')
            columns = [item[0] for item in cursor.description]
            if amount_column not in columns:
                raise ValueError(f'聚合校验 SQL 必须输出名为「{amount_column}」的金额列。')
            amount_index = columns.index(amount_column)
            total_groups = failed_groups = 0
            amount_sum = Decimal('0')
            failed_rows, passing_rows, compare_rows = [], [], []
            while True:
                batch = cursor.fetchmany(5000)
                if not batch:
                    break
                for source_row in batch:
                    row = list(source_row)
                    total_groups += 1
                    raw = row[amount_index]
                    try:
                        amount = Decimal(str(raw))
                        if not amount.is_finite():
                            raise InvalidOperation()
                    except (InvalidOperation, TypeError):
                        raise ValueError(f'第 {total_groups} 个聚合结果的「{amount_column}」不是有效金额。') from None
                    amount_sum += amount
                    if compare is not None:
                        if len(compare_rows) < preview_limit:
                            compare_rows.append(row)
                    elif abs(amount) > allowed:
                        failed_groups += 1
                        if len(failed_rows) < preview_limit:
                            failed_rows.append(row)
                    elif len(passing_rows) < preview_limit:
                        passing_rows.append(row)
            result = {'columns': columns, 'total_rows': total_groups, 'total_groups': total_groups,
                      'expanded_sql': planned, 'amount_column': amount_column,
                      'amount_sum': format(amount_sum, 'f')}
            if compare is None:
                preview = failed_rows if failed_groups else passing_rows
                result.update(rows=preview, failed_groups=failed_groups,
                              validation_status='passed' if failed_groups == 0 else 'failed',
                              truncated=total_groups > len(preview),
                              tolerance=format(allowed, 'f'))
            else:
                op, target = compare
                passed = amount_sum < target if op == '<' else amount_sum > target
                result.update(rows=compare_rows, failed_groups=0 if passed else total_groups,
                              validation_status='passed' if passed else 'failed',
                              truncated=total_groups > len(compare_rows),
                              rule={'op': op, 'value': format(target, 'f'), 'label': str(rule.get('label') or '')})
            return result
        finally:
            con.close()

    def export_csv(self, sql, output_path):
        planned = self.plan(sql)
        con = self._connect()
        try:
            cursor = con.execute(f'SELECT * FROM ({planned}) AS export_result')
            columns = [item[0] for item in cursor.description]
            count = 0
            with Path(output_path).open('w', encoding='utf-8-sig', newline='') as stream:
                writer = csv.writer(stream)
                writer.writerow(columns)
                while True:
                    batch = cursor.fetchmany(5000)
                    if not batch:
                        break
                    for row in batch:
                        writer.writerow([self._safe_csv(value) for value in row])
                        count += 1
            return {'row_count': count, 'columns': columns, 'expanded_sql': planned}
        finally:
            con.close()

    def _connect(self):
        con = duckdb.connect(':memory:', config={'threads': str(self.runtime.get('threads', 2)), 'memory_limit': str(self.runtime.get('memory_mb', 256)) + 'MB'})
        for item in self.workspace:
            path = str(Path(item['directory']) / 'warehouse.duckdb').replace("'", "''")
            schema = 'ds_' + item['id']
            con.execute(f"ATTACH '{path}' AS \"{schema}\" (READ_ONLY)")
        return con

    @staticmethod
    def _safe_csv(value):
        if value is None:
            return ''
        if isinstance(value, (int, float, Decimal)):
            return value
        text = str(value)
        return "'" + text if text[:1] in ('=', '+', '-', '@') else text

    def count(self, sql):
        return self.query(sql, 1)['total_rows']
