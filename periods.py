"""Accounting-period lifecycle and immutable, versioned close packages."""
import json
import re
import shutil
import threading
import uuid
from pathlib import Path

LOCK = threading.RLock()


def month(value):
    if not isinstance(value, str) or not re.fullmatch(r'\d{4}-(0[1-9]|1[0-2])', value):
        raise ValueError('账期请使用 YYYY-MM 格式。')
    return value


def state(app):
    with app.connect() as con:
        con.execute('CREATE TABLE IF NOT EXISTS accounting_state (id INTEGER PRIMARY KEY, payload TEXT NOT NULL)')
        con.execute('CREATE TABLE IF NOT EXISTS accounting_archives (id TEXT PRIMARY KEY, period TEXT, revision INTEGER, payload TEXT NOT NULL)')
        row = con.execute('SELECT payload FROM accounting_state WHERE id=1').fetchone()
        if row:
            return json.loads(row['payload'])
        candidates = sorted({str(p) for d in app.datasets() if d.get('role') != 'reference' for p in d.get('periods', []) if re.fullmatch(r'\d{4}-(0[1-9]|1[0-2])', str(p))})
        value = {'period': candidates[-1] if candidates else app.now()[:7], 'status': 'open', 'legacy_period': candidates[-1] if candidates else app.now()[:7]}
        con.execute('INSERT INTO accounting_state VALUES (1,?)', (json.dumps(value),))
        return value


def store(app, value):
    with app.connect() as con:
        con.execute('UPDATE accounting_state SET payload=? WHERE id=1', (json.dumps(value, ensure_ascii=False),))


def listing(app):
    current = state(app)
    with app.connect() as con:
        rows = con.execute('SELECT payload FROM accounting_archives ORDER BY period DESC, revision DESC').fetchall()
    return {'current': current, 'archives': [json.loads(r['payload']) for r in rows]}


def assert_open(app):
    if state(app)['status'] != 'open':
        raise ValueError('当前账期已关账。请新建账期，或由管理员重开后修改。')


def archive(app, archive_id):
    with app.connect() as con:
        row = con.execute('SELECT payload FROM accounting_archives WHERE id=?', (str(archive_id),)).fetchone()
    if not row:
        raise ValueError('归档版本不存在。')
    return json.loads(row['payload'])


def folder(app, archive_id):
    archive(app, archive_id)
    return app.DATA / 'period-archives' / archive_id


def change(app, action, req):
    with LOCK:
        if app.PLAN_RUN_LOCK.locked() or app.REFRESH_LOCK.locked():
            raise ValueError('请等待当前运行或导入结束后操作账期。')
        current = state(app)
        if action == 'close':
            assert_open(app)
            person = str(req.get('confirmed_by', '')).strip()[:100]
            note = str(req.get('note', '')).strip()[:4000]
            if not person or req.get('confirmed') is not True:
                raise ValueError('请填写业务确认人并勾选确认。')
            data = app.bootstrap()
            plan = data['plan']
            if not plan.get('version') or not data['plan_runs']:
                raise ValueError('请先保存方案并完成当前账期检验。')
            run = data['plan_runs'][0]
            if current.get('reopened_at') and run['created_at'] <= current['reopened_at']:
                raise ValueError('重开后请重新运行检验，再次业务确认关账。')
            if run.get('plan_version') != plan['version']:
                raise ValueError('方案已变更，请重新运行后关账。')
            enabled = {n['node_id'] for n in plan['nodes'] if n.get('enabled', True)}
            if not enabled or enabled != {n['node_id'] for n in run['checks']}:
                raise ValueError('运行结果没有覆盖当前全部启用节点。')
            for check in run['checks']:
                if check.get('status') == 'error' or not check.get('snapshots') or app.snapshots_changed(check['snapshots']):
                    raise ValueError('节点执行错误或数据已更新，请重新检验后关账。')
            for d in data['datasets']:
                if d.get('role') != 'reference' and set(map(str, d.get('periods', []))) != {current['period']}:
                    raise ValueError(f'「{d["name"]}」的数据期间与账期不一致，请刷新对应月份的数据。')
            if run['status'] != 'passed' and not note:
                raise ValueError('存在未通过节点，请填写业务接受差异的确认说明。')
            with app.connect() as con:
                revision = con.execute('SELECT COALESCE(MAX(revision),0)+1 FROM accounting_archives WHERE period=?', (current['period'],)).fetchone()[0]
            identifier = uuid.uuid4().hex
            root = app.DATA / 'period-archives'
            root.mkdir(exist_ok=True)
            staging = root / (identifier + '.pending')
            target = root / identifier
            staging.mkdir()
            record = {'id': identifier, 'period': current['period'], 'revision': revision, 'closed_at': app.now(), 'confirmed_by': person, 'note': note, 'plan_version': plan['version'], 'run_id': run['id'], 'result': run['status'], 'reopened_from': current.get('reopened_from')}
            try:
                for d in data['datasets']:
                    shutil.copytree(app.DATA / d['id'], staging / 'datasets' / d['id'])
                package = {'confirmation': record, 'plan': plan, 'run': run, 'sources': app.source_config(), 'runtime': app.wren_runtime(), 'engine': data['engine'], 'semantics': [app.semantic_model(d['id']) for d in data['datasets']]}
                (staging / 'final.json').write_text(json.dumps(package, ensure_ascii=False, indent=2), encoding='utf-8')
                staging.rename(target)
                shutil.make_archive(str(root / identifier), 'zip', target)
                with app.connect() as con:
                    con.execute('INSERT INTO accounting_archives VALUES (?,?,?,?)', (identifier, current['period'], revision, json.dumps(record, ensure_ascii=False)))
                    current.update(status='closed', archive_id=identifier)
                    con.execute('UPDATE accounting_state SET payload=? WHERE id=1', (json.dumps(current),))
            except Exception:
                # Only this attempt's generated directory is removed; live data is untouched.
                for path in (staging, target):
                    if path.exists():
                        shutil.rmtree(path)
                (root / (identifier + '.zip')).unlink(missing_ok=True)
                raise
        elif action == 'new':
            if current['status'] != 'closed':
                raise ValueError('请先确认并关闭当前账期。')
            label = month(req.get('period'))
            if label <= current['period']:
                raise ValueError('新账期必须晚于当前账期。')
            with app.connect() as con:
                if con.execute('SELECT 1 FROM accounting_archives WHERE period=?', (label,)).fetchone():
                    raise ValueError('该账期已有归档，请使用管理员重开。')
            # Preserve old working files in addition to the immutable close package.
            backup = app.DATA / 'period-working' / uuid.uuid4().hex
            backup.mkdir(parents=True)
            moved = []
            try:
                for spec in app.source_config():
                    source = app.DATA / spec['id']
                    if spec.get('role') != 'reference' and source.exists():
                        source.rename(backup / spec['id']); moved.append(spec['id'])
                store(app, {'period': label, 'status': 'open', 'legacy_period': current['legacy_period'], 'created_at': app.now(), 'inherited_from': current['period']})
            except Exception:
                for did in moved:
                    (backup / did).rename(app.DATA / did)
                raise
        elif action == 'reopen':
            app.require_admin(req)
            if current['status'] != 'closed':
                raise ValueError('请先关闭当前账期，再重开历史月份。')
            reason = str(req.get('reason', '')).strip()[:4000]
            if not reason:
                raise ValueError('请填写重开原因。')
            record = archive(app, req.get('archive_id'))
            with app.connect() as con:
                latest = con.execute('SELECT id FROM accounting_archives WHERE period=? ORDER BY revision DESC LIMIT 1', (record['period'],)).fetchone()[0]
            if latest != record['id']:
                raise ValueError('只能从该账期最后归档版本重开。')
            package = json.loads((folder(app, record['id']) / 'final.json').read_text(encoding='utf-8'))
            backup = app.DATA / 'period-working' / uuid.uuid4().hex
            backup.mkdir(parents=True)
            moved, installed = [], []
            old_sources = app.source_config()
            try:
                for spec in old_sources:
                    path = app.DATA / spec['id']
                    if path.exists():
                        path.rename(backup / spec['id']); moved.append(spec['id'])
                for path in (folder(app, record['id']) / 'datasets').iterdir():
                    installed.append(path.name); shutil.copytree(path, app.DATA / path.name)
                app.save_source_config(package['sources'])
                plan = package['plan']
                with app.connect() as con:
                    con.execute('CREATE TABLE IF NOT EXISTS semantic_notes (dataset_id TEXT PRIMARY KEY, payload TEXT NOT NULL)')
                    for model in package.get('semantics', []):
                        con.execute('INSERT OR REPLACE INTO semantic_notes VALUES (?,?)', (model['dataset_id'], json.dumps({'description': model['description'], 'fields': model['fields']}, ensure_ascii=False)))
                    con.execute("INSERT OR REPLACE INTO validation_plans VALUES ('current',?,?,?,?)", (plan['name'], json.dumps(plan, ensure_ascii=False), plan['version'], plan['updated_at']))
                    value = {'period': record['period'], 'status': 'open', 'legacy_period': current['legacy_period'], 'reopened_from': record['id'], 'reopen_reason': reason, 'reopened_at': app.now()}
                    con.execute('UPDATE accounting_state SET payload=? WHERE id=1', (json.dumps(value),))
            except Exception:
                for did in installed:
                    if (app.DATA / did).exists():
                        shutil.rmtree(app.DATA / did)
                for did in moved:
                    (backup / did).rename(app.DATA / did)
                app.save_source_config(old_sources)
                raise
        else:
            raise ValueError('未知账期操作。')
        return listing(app)
