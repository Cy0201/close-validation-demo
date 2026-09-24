"""Integration checks use real imported data and an isolated temporary rules database."""
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from http.server import ThreadingHTTPServer
import server

class DemoIntegration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.previous = server.STATE_DB
        server.STATE_DB = Path(cls.temp.name) / 'test.sqlite3'
        server.initialize()
        cls.httpd = ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f'http://127.0.0.1:{cls.httpd.server_port}'

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        server.STATE_DB = cls.previous
        cls.temp.cleanup()

    def call(self, path, value=None, method=None, headers=None):
        body = json.dumps(value, ensure_ascii=False).encode('utf-8') if value is not None else None
        req = Request(self.base + path, data=body, method=method or ('POST' if body else 'GET'),
                      headers={'Content-Type':'application/json', **(headers or {})})
        try:
            with urlopen(req, timeout=45) as response:
                return response.status, json.load(response)
        except HTTPError as exc:
            return exc.code, json.load(exc)

    def download(self, path):
        with urlopen(Request(self.base + path), timeout=45) as response:
            return response.status, response.read(), response.headers

    def post_download(self, path, value):
        body = json.dumps(value, ensure_ascii=False).encode('utf-8')
        request = Request(self.base + path, data=body, method='POST', headers={'Content-Type':'application/json'})
        with urlopen(request, timeout=45) as response:
            return response.status, response.read(), response.headers

    def run_plan(self, value=None):
        code, resp = self.call('/api/plan/run', value if value is not None else {})
        if code != 202:
            self.fail(f'方案运行未启动: {code} {resp}')
        for _ in range(600):
            time.sleep(0.25)
            _, state = self.call('/api/plan/run/status')
            if state.get('status') != 'running':
                return state.get('run') or state
        self.fail('方案运行超时未结束')

    def test_01_full_dataset_counts_and_separate_schemas(self):
        for did, expected in [('before',139203),('after',252391)]:
            code,result = self.call('/api/query', {'dataset_id':did,'sql':'SELECT COUNT(*) AS n FROM expense'})
            self.assertEqual(code,200,result)
            self.assertEqual(result['rows'],[[expected]])
            self.assertIn('raw_expense',result['expanded_sql'])
        code,result = self.call('/api/bootstrap')
        self.assertEqual(code,200)
        before,after=result['datasets']
        self.assertIn('事业部编码',[c['name'] for c in before['columns']])
        self.assertNotIn('事业部编码',[c['name'] for c in after['columns']])
        self.assertIn('产品事业部代码',[c['name'] for c in after['columns']])

    def test_02_chinese_columns_and_exact_decimal(self):
        for did in ('before','after'):
            code,r = self.call('/api/query', {'dataset_id':did,'sql':'SELECT "期间", COUNT(*) AS "行数" FROM expense GROUP BY "期间"'})
            self.assertEqual(code,200,r)
            self.assertEqual(r['rows'][0][0],'2026-08')
            code,r = self.call('/api/query', {'dataset_id':did,'sql':'SELECT "本期费用" FROM expense WHERE _source_row = 2'})
            self.assertEqual(code,200,r)
            self.assertIsInstance(r['rows'][0][0],str)

    def test_03_preview_limit_is_not_total_count(self):
        code,r = self.call('/api/query', {'dataset_id':'after','sql':'SELECT _source_row FROM expense','limit':7})
        self.assertEqual(code,200,r)
        self.assertEqual(len(r['rows']),7)
        self.assertEqual(r['total_rows'],252391)
        self.assertTrue(r['truncated'])

    def test_04_writes_and_external_sources_rejected(self):
        for sql in ['DELETE FROM expense', 'SELECT 1; DROP TABLE expense;', "SELECT * FROM read_csv('C:/secret.csv')",
                    'SELECT * FROM information_schema.tables', 'SELECT * FROM after.expense',
                    "SELECT read_blob('C:/secret.txt')", 'ATTACH DATABASE \'other.db\' AS other']:
            with self.subTest(sql=sql):
                code,result = self.call('/api/query', {'dataset_id':'before','sql':sql})
                self.assertEqual(code,400,result)
                self.assertIn('error',result)

    def test_05_cross_site_requests_rejected(self):
        code,_ = self.call('/api/query',{'dataset_id':'before','sql':'SELECT COUNT(*) FROM expense'},headers={'Origin':'https://other.example'})
        self.assertEqual(code,400)

    def test_06_rules_and_runs_are_isolated_and_persisted(self):
        code,rule = self.call('/api/rules',{'dataset_id':'before','name':'Integration test only',
            'sql':'SELECT \'全表\' AS "维度", SUM(CASE WHEN _source_row < 0 THEN 1 ELSE 0 END) AS "校验金额" FROM expense',
            'detail_sql':'SELECT _source_row FROM expense WHERE _source_row < 0','tolerance':'0'})
        self.assertEqual(code,201,rule)
        code,run = self.call('/api/rules/'+rule['id']+'/run',{})
        self.assertEqual(code,200,run)
        self.assertEqual(run['status'],'passed')
        self.assertEqual(run['failed_groups'],0)
        self.assertEqual(run['total_groups'],1)
        _,data = self.call('/api/bootstrap')
        self.assertEqual(len([r for r in data['rules'] if r['dataset_id']=='after']),0)
        self.assertTrue(any(r['id']==run['id'] for r in data['runs']))
        code,_ = self.call('/api/rules/'+rule['id'],method='DELETE')
        self.assertEqual(code,200)
        _,data=self.call('/api/bootstrap')
        self.assertFalse(any(r['id']==rule['id'] for r in data['rules']))
        self.assertTrue(any(r['id']==run['id'] for r in data['runs']))

    def test_07_invalid_dataset_and_unknown_column(self):
        code,_=self.call('/api/query',{'dataset_id':'../before','sql':'SELECT 1'})
        self.assertEqual(code,400)
        code,_=self.call('/api/query',{'dataset_id':'after','sql':'SELECT "事业部编码" FROM expense'})
        self.assertEqual(code,400)

    def test_08_cte_and_quoted_semicolon_allowed(self):
        code,r=self.call('/api/query',{'dataset_id':'before','sql':"WITH c AS (SELECT COUNT(*) AS n FROM expense) SELECT n, 'a;b' AS label FROM c;"})
        self.assertEqual(code,200,r)
        self.assertEqual(r['rows'],[[139203,'a;b']])
        code,r=self.call('/api/query',{'dataset_id':'before','sql':'SELECT COUNT(*) FROM expense WHERE "期间" = \'2026-08\' AND _source_row > 0'})
        self.assertEqual(code,200,r)
        self.assertEqual(r['rows'],[[139203]])

    def test_09_failed_groups_detail_and_csv_download(self):
        code,rule = self.call('/api/rules', {'dataset_id':'after','name':'Download contract',
            'sql':'SELECT \'固定维度\' AS "维度", CAST(1 AS DECIMAL(18,2)) AS "校验金额" FROM expense LIMIT 1',
            'detail_sql':'SELECT _source_row FROM expense LIMIT 2','tolerance':'0'})
        self.assertEqual(code,201,rule)
        _,run = self.call('/api/rules/'+rule['id']+'/run', {})
        self.assertEqual(run['status'],'failed')
        self.assertEqual(run['failed_groups'],1)
        self.assertEqual(run['detail']['total_rows'],2)
        code,data,headers = self.download('/api/runs/'+run['id']+'/download?view=aggregate')
        self.assertEqual(code,200)
        self.assertTrue(data.startswith(b'\xef\xbb\xbf'))
        self.assertIn('attachment',headers['Content-Disposition'])
        self.call('/api/rules/'+rule['id'],method='DELETE')

    def test_10_single_plan_wraps_inner_sql_and_drills_to_detail(self):
        shared = 'SELECT "期间", SUM("校验金额") AS "校验金额" FROM rule_detail GROUP BY "期间"'
        nodes = []
        for did in ('before','after'):
            for index in range(2):
                nodes.append({'dataset_id':did, 'name':f'Node {did} {index}', 'enabled':True, 'tolerance':'0',
                    'detail_sql':'SELECT _source_row, "期间", CASE WHEN _source_row = 2 THEN 1 ELSE 0 END AS "校验金额" FROM expense'})
        code,plan = self.call('/api/plan', {'name':'Plan contract','shared_sql':shared,'nodes':nodes})
        self.assertEqual(code,200,plan)
        self.assertEqual(len(plan['nodes']),4)
        code,run = self.call('/api/plan/run', {})
        self.assertEqual(code,200,run)
        self.assertEqual(run['status'],'failed')
        self.assertEqual(len([x for x in run['checks'] if x['dataset_id']=='before']),2)
        before = next(x for x in run['checks'] if x['dataset_id']=='before')
        self.assertEqual(before['failed_groups'],1)
        self.assertIn('rule_detail',before['aggregate_sql'])
        group = {name:before['rows'][0][before['columns'].index(name)] for name in before['dimensions']}
        code,detail = self.call('/api/plan-runs/'+run['id']+'/detail', {'dataset_id':'before','node_id':before['node_id'],'group':group})
        self.assertEqual(code,200,detail)
        self.assertEqual(detail['total_rows'],139203)
        code,data,_ = self.post_download('/api/plan-runs/'+run['id']+'/detail-export',
            {'dataset_id':'before','node_id':before['node_id'],'group':group})
        self.assertEqual(code,200)
        self.assertTrue(data.startswith(b'\xef\xbb\xbf'))

    def test_11_cross_table_aggregation(self):
        sql = '''WITH rule_detail AS (
          SELECT e._source_row, 1 AS amount FROM expense e
          JOIN expense_after r ON r._source_row=e._source_row WHERE e._source_row=2
        ) SELECT SUM(d.amount) AS "校验金额" FROM rule_detail d
          JOIN expense_before s ON s._source_row=d._source_row'''
        code, result = self.call('/api/query', {'dataset_id':'before','sql':sql})
        self.assertEqual(code, 200, result)
        self.assertEqual(result['rows'], [[1]])

    def test_12_empty_draft_saves_but_cannot_pass(self):
        code, plan = self.call('/api/plan', {'name':'Empty draft','shared_sql':'SELECT 0 AS "校验金额"',
                                           'tracks':[], 'nodes':[]})
        self.assertEqual(code,200,plan)
        self.assertEqual(plan['tracks'],[])
        self.assertEqual(plan['nodes'],[])
        code, run = self.call('/api/plan/run', {})
        self.assertEqual(code,400,run)

    def test_13_legacy_track_ids_are_stable(self):
        payload={'name':'Legacy','version':1,'shared_sql':'SELECT 0 AS "校验金额"',
                 'nodes':[{'node_id':'a'*32,'dataset_id':'before','name':'Legacy node'}]}
        with server.connect() as con:
            con.execute("INSERT OR REPLACE INTO validation_plans VALUES ('current',?,?,?,?)",
                        ['Legacy',json.dumps(payload),1,server.now()])
        _, first=self.call('/api/bootstrap'); _, second=self.call('/api/bootstrap')
        self.assertEqual(first['plan']['tracks'],second['plan']['tracks'])

    def test_14_global_aggregate_drilldown_and_stale_guard(self):
        node={'dataset_id':'before','name':'Global','detail_sql':'SELECT _source_row, 1 AS "校验金额" FROM expense LIMIT 2'}
        code, plan=self.call('/api/plan',{'name':'Global','shared_sql':'SELECT SUM("校验金额") AS "校验金额" FROM rule_detail','nodes':[node]})
        self.assertEqual(code,200,plan)
        code, run=self.call('/api/plan/run',{})
        self.assertEqual(code,200,run)
        check=run['checks'][0]
        body={'dataset_id':'before','node_id':check['node_id'],'group':{}}
        detail_url='/api/plan-runs/'+run['id']+'/detail'
        code, result=self.call(detail_url,body)
        self.assertEqual(code,200,result)
        self.assertEqual(result['total_rows'],2)
        with patch('server.snapshots_changed',return_value=True):
            code,_=self.call(detail_url,body);self.assertEqual(code,400)
            code,_=self.call(detail_url+'-export',body);self.assertEqual(code,400)

    def test_15_ai_detail_draft_requires_amount_contract(self):
        response=json.dumps({'sql':'SELECT _source_row, "期间", 1 AS "校验金额" FROM expense LIMIT 1',
                             'explanation':'保留原始行并输出标记金额','assumptions':[],'warnings':[]},ensure_ascii=False)
        with patch('server.config',return_value={'configured':True,'model':'gpt-5.6-luna'}), \
             patch('server.call_model',return_value=response):
            result=server.generate({'target':'detail','action':'generate','dataset_id':'before','prompt':'测试异常明细','current_sql':''})
        self.assertEqual(result['status'],'draft')
        self.assertIn('raw_expense',result['expanded_sql'])
        bad=json.dumps({'sql':'SELECT _source_row FROM expense','explanation':'缺少金额'},ensure_ascii=False)
        with patch('server.config',return_value={'configured':True,'model':'gpt-5.6-luna'}), \
             patch('server.call_model',return_value=bad):
            with self.assertRaisesRegex(ValueError,'校验金额'):
                server.generate({'target':'detail','action':'generate','dataset_id':'before','prompt':'测试','current_sql':''})

    def test_16_ai_shared_draft_validates_every_enabled_node(self):
        response=json.dumps({'sql':'SELECT "期间", SUM("校验金额") AS "校验金额" FROM rule_detail GROUP BY "期间"',
                             'explanation':'按期间汇总','assumptions':[],'warnings':[]},ensure_ascii=False)
        nodes=[{'name':'结算前节点','dataset_id':'before','detail_sql':'SELECT _source_row, "期间", 1 AS "校验金额" FROM expense LIMIT 1'},
               {'name':'结算后节点','dataset_id':'after','detail_sql':'SELECT _source_row, "期间", 1 AS "校验金额" FROM expense LIMIT 1'}]
        with patch('server.config',return_value={'configured':True,'model':'gpt-5.6-luna'}), \
             patch('server.call_model',return_value=response):
            result=server.generate({'target':'aggregate','action':'generate','dataset_id':'before','prompt':'按期间汇总','nodes':nodes})
        self.assertEqual(result['validated_nodes'],['结算前节点','结算后节点'])

    def test_17_ai_clarification_and_responses_text(self):
        response=json.dumps({'needs_clarification':['请明确金额字段','请明确筛选范围']},ensure_ascii=False)
        with patch('server.config',return_value={'configured':True,'model':'gpt-5.6-luna'}), \
             patch('server.call_model',return_value=response):
            result=server.generate({'target':'detail','action':'generate','dataset_id':'before','prompt':'检查费用是否合理'})
        self.assertEqual(result['status'],'needs_clarification')
        self.assertEqual(len(result['needs_clarification']),2)
        text=server._response_text({'output':[{'type':'message','content':[{'type':'output_text','text':'ok'}]}]})
        self.assertEqual(text,'ok')
        with patch('server.socket.gethostbyname',return_value='10.0.0.1'):
            self.assertTrue(server._private_target('internal-model'))
        self.assertIn('上游模型不可用',server._http_status_message(502))

    def test_18_compare_mode_node_validation(self):
        node_a = {'dataset_id':'before','name':'Compare pass','detail_sql':'SELECT _source_row, 1 AS "校验金额" FROM expense LIMIT 2',
                  'check_mode':'compare','compare_op':'>','compare_value':'0','report_label':'EMS收入'}
        node_b = {'dataset_id':'before','name':'Compare fail','detail_sql':'SELECT _source_row, 1 AS "校验金额" FROM expense LIMIT 2',
                  'check_mode':'compare','compare_op':'<','compare_value':'0'}
        node_c = {'dataset_id':'before','name':'Zero default','detail_sql':'SELECT _source_row, 0 AS "校验金额" FROM expense LIMIT 2'}
        code, plan = self.call('/api/plan', {'name':'Compare mode','shared_sql':'SELECT SUM("校验金额") AS "校验金额" FROM rule_detail','nodes':[node_a,node_b,node_c]})
        self.assertEqual(code,200,plan)
        saved = {n['name']:n for n in plan['nodes']}
        self.assertEqual(saved['Compare pass']['check_mode'],'compare')
        self.assertEqual(saved['Compare pass']['compare_value'],'0')
        self.assertEqual(saved['Compare pass']['report_label'],'EMS收入')
        self.assertNotIn('check_mode',saved['Zero default'])
        run = self.run_plan()
        self.assertEqual(run['status'],'failed')
        checks = {c['name']:c for c in run['checks']}
        self.assertEqual(checks['Compare pass']['validation_status'],'passed')
        self.assertEqual(checks['Compare pass']['amount_sum'],'2')
        self.assertEqual(checks['Compare pass']['rule']['label'],'EMS收入')
        self.assertEqual(checks['Compare fail']['validation_status'],'failed')
        self.assertEqual(checks['Compare fail']['failed_groups'],1)
        self.assertEqual(checks['Zero default']['validation_status'],'passed')
        self.assertEqual(checks['Zero default']['amount_sum'],'0')
        code, bad = self.call('/api/plan', {'name':'Bad','shared_sql':'SELECT 0 AS "校验金额"','nodes':[dict(node_a,compare_op='=')]})
        self.assertEqual(code,400)

if __name__=='__main__':
    unittest.main(verbosity=2)
