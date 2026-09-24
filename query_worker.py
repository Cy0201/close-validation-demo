import json
import sys
import time
from decimal import Decimal
from pathlib import Path
from engine_adapter import WrenAdapter

def serial(value):
    if isinstance(value, Decimal):
        return format(value, 'f')
    if hasattr(value, 'isoformat'):
        return value.isoformat()
    return str(value)

if __name__ == '__main__':
    sys.stdin.reconfigure(encoding='utf-8')
    sys.stdout.reconfigure(encoding='utf-8')
    try:
        req = json.load(sys.stdin)
        if not isinstance(req.get('dataset_id'), str): raise ValueError('无效的数据集。')
        data_root = Path(__file__).resolve().parent / 'data'
        directory = data_root / req['dataset_id']
        manifest = json.loads((directory / 'manifest.json').read_text(encoding='utf-8'))
        source_cfg = json.loads((data_root / 'source-config.json').read_text(encoding='utf-8'))
        if isinstance(source_cfg, dict): source_cfg = [{'id':'before','model_name':'expense_before'},{'id':'after','model_name':'expense_after'}]
        requested = req.get('allowed_dataset_ids')
        if requested is not None:
            if not isinstance(requested, list) or any(not isinstance(item, str) for item in requested):
                raise ValueError('无效的流程数据表权限。')
            allowed = set(requested)
            if req['dataset_id'] not in allowed:
                raise ValueError('当前查询不属于该流程的校验主表。')
            source_cfg = [spec for spec in source_cfg if spec.get('id') in allowed]
        workspace=[]
        for spec in source_cfg:
            mp=data_root/spec['id']/'manifest.json'
            if mp.exists():
                m=json.loads(mp.read_text(encoding='utf-8')); workspace.append({'id':spec['id'],'model_name':spec.get('model_name',spec['id']),'columns':m['columns'],'directory':str(data_root/spec['id'])})
        adapter = WrenAdapter(directory, manifest['columns'], workspace, req['dataset_id'], req.get('runtime'))
        start = time.perf_counter()
        if req.get('mode') == 'plan':
            result = {'expanded_sql': adapter.plan(req['sql'])}
        elif req.get('mode') == 'contract':
            result = adapter.contract(req['sql'], req.get('amount_column', '校验金额'))
        elif req.get('mode') == 'validate':
            result = adapter.validate_rule(req['sql'], req.get('amount_column', '校验金额'), req.get('tolerance', '0'), req.get('limit', 200), req.get('rule'))
        elif req.get('mode') == 'export':
            output = Path(req['output_path']).resolve()
            downloads = (Path(__file__).resolve().parent / 'data' / 'downloads').resolve()
            if output.parent != downloads:
                raise ValueError('无效的导出位置。')
            downloads.mkdir(parents=True, exist_ok=True)
            result = adapter.export_csv(req['sql'], output)
        else:
            result = adapter.query(req['sql'], req.get('limit', 100))
        result.update(elapsed_ms=round((time.perf_counter() - start) * 1000), engine='WrenAI + DuckDB', snapshot=manifest['sha256'],
                      snapshots={item['id']: json.loads((Path(item['directory'])/'manifest.json').read_text(encoding='utf-8'))['sha256'] for item in workspace})
        print(json.dumps(result, ensure_ascii=False, default=serial))
    except Exception as exc:
        print(json.dumps({'error': str(exc)}, ensure_ascii=False))
        sys.exit(1)
