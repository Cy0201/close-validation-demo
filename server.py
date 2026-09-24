"""Local-only demo API. Business rules are deliberately empty at first start."""
from __future__ import annotations
import importlib.metadata
import http.client
import ipaddress
import json
import mimetypes
import os
import re
import socket
import ssl
import sqlite3
import socket
import subprocess
import sys
import threading
import time
import uuid
import hashlib
import secrets
import inspect
import periods
import shutil
from contextlib import contextmanager
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlparse
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent
DATA = ROOT / 'data'
STATIC = ROOT / 'static'
STATE_DB = DATA / 'workspace.sqlite3'
SOURCE_CONFIG = DATA / 'source-config.json'
MODEL_CONFIG = ROOT / 'model-config.json'
CC_SWITCH_DB = Path.home() / '.cc-switch' / 'cc-switch.db'
DOWNLOADS = DATA / 'downloads'
WORKERS = threading.BoundedSemaphore(2)
REFRESH_LOCK = threading.Lock()
CHOOSE_LOCK = threading.Lock()
REFRESH_STATE = {'status': 'idle'}
PLAN_RUN_LOCK = threading.Lock()
PLAN_RUN_STATE = {'status': 'idle', 'checks': []}
ADMIN_SESSIONS = {}
ADMIN_LOCK = threading.Lock()

LAN_SHARE_CACHE = {'t': 0.0, 'v': False}

def lan_share_enabled():
    if time.time() - LAN_SHARE_CACHE['t'] > 2:
        LAN_SHARE_CACHE['v'] = bool(admin_settings().get('lan_share'))
        LAN_SHARE_CACHE['t'] = time.time()
    return LAN_SHARE_CACHE['v']

def lan_share_set(value):
    settings = admin_settings()
    settings['lan_share'] = bool(value)
    admin_settings(settings)
    LAN_SHARE_CACHE['v'] = bool(value)
    LAN_SHARE_CACHE['t'] = time.time()

def admin_settings(value=None):
    with connect() as con:
        con.execute('CREATE TABLE IF NOT EXISTS admin_settings (id TEXT PRIMARY KEY, payload TEXT NOT NULL)')
        if value is not None:
            con.execute("INSERT OR REPLACE INTO admin_settings VALUES ('wren', ?)", (json.dumps(value),))
        row = con.execute("SELECT payload FROM admin_settings WHERE id='wren'").fetchone()
    return json.loads(row['payload']) if row else {}

def admin_login(req):
    password = req.get('password', '')
    if not isinstance(password, str) or not 4 <= len(password) <= 128:
        raise ValueError('密码长度需要 4–128 字。')
    with ADMIN_LOCK:
        settings = admin_settings()
        if not settings.get('password_hash'):
            if req.get('setup') is not True:
                raise ValueError('请先设置管理员密码。')
            settings['salt'] = secrets.token_hex(16)
            settings['password_hash'] = hashlib.pbkdf2_hmac('sha256', password.encode(), settings['salt'].encode(), 200000).hex()
            admin_settings(settings)
        digest = hashlib.pbkdf2_hmac('sha256', password.encode(), settings['salt'].encode(), 200000).hex()
        if not secrets.compare_digest(digest, settings['password_hash']):
            raise ValueError('密码不正确。')
        token = secrets.token_urlsafe(32)
        current = time.time()
        for old, expires in list(ADMIN_SESSIONS.items()):
            if expires < current:
                ADMIN_SESSIONS.pop(old, None)
        ADMIN_SESSIONS[token] = current + 1800
        return {'token': token, 'expires_in': 1800}

def require_admin(req):
    if ADMIN_SESSIONS.get(str(req.get('token', '')), 0) <= time.time():
        raise ValueError('管理员会话已锁定，请输入密码。')

def wren_runtime():
    settings = admin_settings()
    return {key: settings.get(key, default) for key, default in {'timeout': 30, 'threads': 2, 'memory_mb': 256, 'preview_limit': 200}.items()}

def now():
    return datetime.now(timezone.utc).isoformat()

def run_plan_job(plan, track_id=None):
    global PLAN_RUN_STATE
    run = {'id': uuid.uuid4().hex, 'rule_id': 'validation_plan', 'rule_name': plan['name'], 'dataset_id': 'all', 'plan_version': plan['version'], 'created_at': now(), 'checks': [], 'period': periods.state(sys.modules[__name__])['period']}
    track_order = [t['track_id'] for t in plan.get('tracks', [])]
    if track_id is not None:
        track_order = [track_id]
    track_set = set(track_order)
    enabled = [item for item in plan['nodes'] if item.get('enabled', True)]
    ordered = []
    for tid in track_order:
        ordered.extend(n for n in enabled if n.get('track_id') == tid)
    if track_id is None:
        ordered.extend(n for n in enabled if n.get('track_id') not in track_set)
    total = len(ordered)
    try:
        index = 0
        for track_idx, tid in enumerate(track_order):
            track_nodes = [n for n in ordered if n.get('track_id') == tid]
            if not track_nodes:
                continue
            track = next((x for x in plan.get('tracks', []) if x['track_id'] == tid), None)
            aggregate_sql = (track or {}).get('aggregate_sql') or plan.get('shared_sql', '')
            for item in track_nodes:
                PLAN_RUN_STATE = {'status': 'running', 'run_id': run['id'], 'current_node_id': item['node_id'],
                                  'current_track_id': tid, 'current_track_name': (track or {}).get('name', ''),
                                  'track_index': track_idx, 'track_total': len(track_order),
                                  'current_index': index, 'total': total, 'checks': list(run['checks'])}
                check = dict(item)
                try:
                    outer_sql = compose_validation_sql(item['detail_sql'], aggregate_sql)
                    check['aggregate_sql'] = outer_sql
                    if item.get('manual_pass_period') == run['period']:
                        allowed = flow_workspace_ids(item['dataset_id'])
                        result = {'columns': [], 'rows': [], 'total_groups': 0, 'failed_groups': 0,
                                  'validation_status': 'passed', 'manual_pass': True,
                                  'snapshot': dataset(item['dataset_id'])['sha256'],
                                  'snapshots': {did: dataset(did)['sha256'] for did in allowed}}
                    else:
                        validate_options = {'amount_column': '校验金额', 'allowed_dataset_ids': flow_workspace_ids(item['dataset_id'])}
                        if item.get('check_mode') == 'compare':
                            validate_options['rule'] = {'op': item['compare_op'], 'value': item['compare_value'], 'label': item.get('report_label', '')}
                        else:
                            validate_options['tolerance'] = item['tolerance']
                        result = execute(item['dataset_id'], outer_sql, 200, mode='validate', **validate_options)
                    check['dimensions'] = [name for name in result['columns'] if name != '校验金额']
                    check.update(result, status=result['validation_status'])
                except ValueError as exc:
                    check.update(status='error', error=str(exc), rows=[], columns=[], total_groups=None, failed_groups=None)
                run['checks'].append(check)
                index += 1
                PLAN_RUN_STATE = {'status': 'running', 'run_id': run['id'], 'current_node_id': None,
                                  'current_track_id': tid, 'current_track_name': (track or {}).get('name', ''),
                                  'track_index': track_idx, 'track_total': len(track_order),
                                  'current_index': index, 'total': total, 'checks': list(run['checks'])}
        if track_id is not None:
            node_tracks = {n['node_id']: n.get('track_id') for n in plan['nodes']}
            with connect() as con:
                prev_row = con.execute("SELECT payload FROM runs WHERE rule_id='validation_plan' ORDER BY created_at DESC LIMIT 1").fetchone()
            if prev_row:
                for carried in json.loads(prev_row['payload']).get('checks', []):
                    carry_track = carried.get('track_id') or node_tracks.get(carried.get('node_id'))
                    if carry_track == track_id:
                        continue
                    carried['carried_over'] = True
                    run['checks'].append(carried)
        statuses = [x['status'] for x in run['checks']]
        run['status'] = 'error' if 'error' in statuses else ('failed' if 'failed' in statuses else 'passed')
        with connect() as con:
            con.execute('INSERT INTO runs VALUES (?,?,?,?,?)', [run['id'], 'validation_plan', 'all', run['created_at'], json.dumps(run, ensure_ascii=False)])
        PLAN_RUN_STATE = {'status': 'completed', 'run': run, 'checks': run['checks'], 'current_node_id': None, 'current_index': total, 'total': total}
    except Exception as exc:
        PLAN_RUN_STATE = {'status': 'failed', 'error': str(exc), 'checks': run['checks'], 'current_node_id': None}
    finally:
        PLAN_RUN_LOCK.release()

def dataset(dataset_id):
    if not re.fullmatch(r'[a-z][a-z0-9_]{1,39}', str(dataset_id or '')):
        raise ValueError('无效的数据源。')
    path = DATA / dataset_id / 'manifest.json'
    if not path.exists():
        raise ValueError('该数据源尚未导入数据。')
    value = json.loads(path.read_text(encoding='utf-8'))
    spec = next((x for x in source_config() if x['id'] == dataset_id), {})
    value.update(name=spec.get('name', value.get('name')), role=spec.get('role', 'validation'), model_name=spec.get('model_name', dataset_id))
    return value

def datasets():
    result = []
    for spec in source_config():
        try: result.append(dataset(spec['id']))
        except ValueError: pass
    return result

def flow_workspace_ids(primary_id):
    """Return the tables a validation flow is allowed to read."""
    primary = dataset(primary_id)
    if primary.get('role') != 'validation':
        raise ValueError('校验流程必须绑定一张校验主表。')
    return [primary_id] + [item['id'] for item in datasets()
                           if item.get('role') == 'reference' and item['id'] != primary_id]

def snapshots_changed(saved):
    if not saved: return False
    current = {d['id']: d.get('sha256') for d in datasets()}
    return any(current.get(key) != value for key, value in saved.items())

@contextmanager
def connect():
    con = sqlite3.connect(STATE_DB, timeout=10)
    con.row_factory = sqlite3.Row
    try:
        with con:
            yield con
    finally:
        con.close()

def initialize():
    DATA.mkdir(exist_ok=True)
    with connect() as con:
        con.execute('PRAGMA journal_mode=WAL')
        con.execute('CREATE TABLE IF NOT EXISTS rules (id TEXT PRIMARY KEY, dataset_id TEXT NOT NULL, name TEXT NOT NULL, sql TEXT NOT NULL, description TEXT, version INTEGER NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)')
        existing = {row['name'] for row in con.execute('PRAGMA table_info(rules)')}
        for name, definition in [('detail_sql', "TEXT NOT NULL DEFAULT ''"), ('amount_column', "TEXT NOT NULL DEFAULT '校验金额'"), ('tolerance', "TEXT NOT NULL DEFAULT '0'")]:
            if name not in existing:
                con.execute(f'ALTER TABLE rules ADD COLUMN {name} {definition}')
        con.execute('CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, rule_id TEXT NOT NULL, dataset_id TEXT NOT NULL, created_at TEXT NOT NULL, payload TEXT NOT NULL)')
        con.execute('CREATE TABLE IF NOT EXISTS validation_plans (id TEXT PRIMARY KEY, name TEXT NOT NULL, payload TEXT NOT NULL, version INTEGER NOT NULL, updated_at TEXT NOT NULL)')

def source_config():
    default = str(ROOT.parent / '三大表校验')
    if not SOURCE_CONFIG.exists():
        SOURCE_CONFIG.write_text(json.dumps([], ensure_ascii=False), encoding='utf-8')
    values = json.loads(SOURCE_CONFIG.read_text(encoding='utf-8'))
    if isinstance(values, dict):
        values = [{'id': 'before', 'name': '结算前费用', 'role': 'validation', 'model_name': 'expense_before', 'folder': str(values.get('before', default)), 'match': '结算前'},
                  {'id': 'after', 'name': '结算后费用', 'role': 'validation', 'model_name': 'expense_after', 'folder': str(values.get('after', default)), 'match': '结算后'}]
        SOURCE_CONFIG.write_text(json.dumps(values, ensure_ascii=False, indent=2), encoding='utf-8')
    return values

def save_source_config(values):
    SOURCE_CONFIG.write_text(json.dumps(values, ensure_ascii=False, indent=2), encoding='utf-8')

def source_status(only_id=None):
    values = source_config()
    result = []
    for spec in values:
        if only_id is not None and spec['id'] != only_id: continue
        did = spec['id']; folder = Path(spec.get('folder', '')); keyword = spec.get('match', '')
        candidates = []
        if folder.is_dir():
            with os.scandir(folder) as entries:
                for entry in entries:
                    if entry.name.startswith('~$') or Path(entry.name).suffix.lower() not in ('.xlsx', '.xls', '.xlsm') or (keyword and keyword not in entry.name): continue
                    try:
                        if entry.is_file(): candidates.append((entry.stat().st_mtime_ns, entry.path))
                    except FileNotFoundError: continue
            candidates.sort(reverse=True)
        result.append({**spec, 'dataset_id': did, 'folder': spec.get('display_folder') or str(folder), 'exists': folder.is_dir(),
                       'latest': candidates[0][1] if candidates else '', 'candidate_count': len(candidates),
                       'imported': (DATA / did / 'manifest.json').exists()})
    return result

def quote_identifier(name):
    return '"' + str(name).replace('"', '""') + '"'

def compose_validation_sql(detail_sql, shared_sql):
    detail = str(detail_sql).strip().rstrip(';')
    outer = str(shared_sql).strip().rstrip(';')
    return f'WITH rule_detail AS ({detail}) {outer}'

def choose_folder():
    if not CHOOSE_LOCK.acquire(blocking=False):
        raise ValueError('文件夹选择窗口已经打开，请先完成当前选择。')
    try:
        script = ("$ErrorActionPreference='Stop'; "
                  "$shell=New-Object -ComObject Shell.Application; "
                  "$folder=$shell.BrowseForFolder(0,'选择报表文件夹',0x41,0); "
                  "if($folder){[Console]::OutputEncoding=[Text.Encoding]::UTF8; Write-Output $folder.Self.Path}")
        completed = subprocess.run(['powershell.exe', '-STA', '-NoProfile', '-Command', script], capture_output=True,
                                   text=True, encoding='utf-8', timeout=300)
        if completed.returncode:
            raise ValueError('无法打开文件夹选择窗口：' + (completed.stderr.strip() or '未知错误'))
        return completed.stdout.strip()
    except subprocess.TimeoutExpired:
        raise ValueError('文件夹选择已超时，请重新点击后完成选择。') from None
    finally:
        CHOOSE_LOCK.release()

def refresh_source(did):
    global REFRESH_STATE
    try:
        item = next(value for value in source_status(did) if value['dataset_id'] == did)
        if not item['latest']:
            raise ValueError('没有匹配的 Excel 文件，请检查文件夹路径和该数据源的文件名关键词。')
        REFRESH_STATE = {'status': 'running', 'dataset_id': did, 'file': item['latest'], 'started_at': now()}
        command = [sys.executable, str(ROOT / 'import_data.py'), '--dataset', did, '--name', item['name'], '--source', item['latest']]
        if item.get('sheet'):
            command.extend(['--sheet', str(item['sheet'])])
        completed = subprocess.run(command,
                                   capture_output=True, text=True, encoding='utf-8', timeout=600, cwd=ROOT,
                                   creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        if completed.returncode:
            detail = ''
            try:
                status = json.loads((DATA / 'import_status.json').read_text(encoding='utf-8'))
                detail = str(status.get('error') or '')
            except (OSError, ValueError):
                pass
            raise ValueError((detail or completed.stderr or completed.stdout or '导入失败').strip()[-1000:])
        unchanged = 'already imported' in completed.stdout
        REFRESH_STATE = {'status': 'completed', 'dataset_id': did, 'file': item['latest'],
                         'unchanged': unchanged,
                         'message': '文件内容未变化，继续使用当前数据。' if unchanged else '最新文件已导入并替换当前数据。',
                         'finished_at': now()}
    except Exception as exc:
        REFRESH_STATE = {'status': 'failed', 'dataset_id': did, 'error': str(exc), 'finished_at': now()}
    finally:
        REFRESH_LOCK.release()

def config():
    values = json.loads(MODEL_CONFIG.read_text(encoding='utf-8')) if MODEL_CONFIG.exists() else {}
    for field in ('base_url', 'model', 'api_key'):
        values[field] = os.environ.get('WREN_DEMO_LLM_' + field.upper(), values.get(field, ''))
    values['api_format'] = values.get('api_format', 'openai_chat')
    values['source'] = values.get('source', 'manual')
    values['configured'] = bool(values.get('base_url') and values.get('model'))
    return values

def public_config(values=None):
    values = values or config()
    return {'configured': values['configured'], 'base_url': values.get('base_url', ''),
            'model': values.get('model', ''), 'api_format': values.get('api_format', 'openai_chat'),
            'source': values.get('source', 'manual'), 'has_api_key': bool(values.get('api_key'))}

def save_config(req):
    old = config()
    base_url = str(req.get('base_url', '')).strip().rstrip('/')
    model = str(req.get('model', '')).strip()
    api_format = str(req.get('api_format', 'openai_chat'))
    if urlparse(base_url).scheme not in ('http', 'https') or not urlparse(base_url).netloc:
        raise ValueError('模型地址必须是完整的 HTTP 或 HTTPS 地址。')
    if not model or len(model) > 200:
        raise ValueError('请填写模型名称。')
    if api_format not in ('openai_chat', 'openai_responses', 'anthropic'):
        raise ValueError('当前仅支持 OpenAI Responses、OpenAI Chat Completions 和 Anthropic Messages 协议。')
    api_key = str(req.get('api_key', '')).strip() or old.get('api_key', '')
    values = {'base_url': base_url, 'model': model, 'api_key': api_key,
              'api_format': api_format, 'source': str(req.get('source', 'manual'))[:120]}
    temp = MODEL_CONFIG.with_suffix('.json.tmp')
    temp.write_text(json.dumps(values, ensure_ascii=False, indent=2), encoding='utf-8')
    temp.replace(MODEL_CONFIG)
    return public_config({**values, 'configured': True})

def cc_switch_providers():
    if not CC_SWITCH_DB.exists():
        return {'found': False, 'providers': []}
    uri = CC_SWITCH_DB.as_uri() + '?mode=ro'
    with sqlite3.connect(uri, uri=True, timeout=2) as con:
        con.row_factory = sqlite3.Row
        rows = con.execute('SELECT id, app_type, name, settings_config, meta, is_current FROM providers ORDER BY is_current DESC, name').fetchall()
    result = []
    for row in rows:
        try:
            settings = json.loads(row['settings_config'] or '{}')
            meta = json.loads(row['meta'] or '{}')
        except json.JSONDecodeError:
            continue
        env = settings.get('env') or {}
        fmt = str(meta.get('apiFormat') or '').lower()
        base_url = str(env.get('ANTHROPIC_BASE_URL') or env.get('OPENAI_BASE_URL') or '').rstrip('/')
        model = str(env.get('ANTHROPIC_MODEL') or env.get('OPENAI_MODEL') or settings.get('model') or '')
        api_key = str(env.get('ANTHROPIC_AUTH_TOKEN') or env.get('ANTHROPIC_API_KEY') or env.get('OPENAI_API_KEY') or '')
        if row['app_type'] == 'claude' and base_url:
            fmt = 'anthropic'; model = model or 'claude-sonnet-4-5'
        elif fmt in ('openai', 'chat_completions'):
            fmt = 'openai_chat'
        elif fmt in ('responses', 'openai_responses'):
            fmt = 'openai_responses'
        importable = bool(base_url and model and fmt in ('openai_chat', 'openai_responses', 'anthropic'))
        result.append({'id': row['id'], 'app_type': row['app_type'], 'name': row['name'],
                       'is_current': bool(row['is_current']), 'base_url': base_url, 'model': model,
                       'api_format': fmt, 'has_api_key': bool(api_key), 'importable': importable,
                       'reason': '' if importable else '该供应商未提供可识别的接口地址、模型或兼容协议。'})
    return {'found': True, 'providers': result}

def import_cc_switch(req):
    provider_id = str(req.get('provider_id', ''))
    app_type = str(req.get('app_type', ''))
    summary = cc_switch_providers()
    item = next((x for x in summary['providers'] if x['id'] == provider_id and x['app_type'] == app_type), None)
    if not item or not item['importable']:
        raise ValueError('请选择一个可导入的 CC Switch 供应商。')
    uri = CC_SWITCH_DB.as_uri() + '?mode=ro'
    with sqlite3.connect(uri, uri=True, timeout=2) as con:
        row = con.execute('SELECT settings_config FROM providers WHERE id=? AND app_type=?', [provider_id, app_type]).fetchone()
    settings = json.loads(row[0]); env = settings.get('env') or {}
    key = env.get('ANTHROPIC_AUTH_TOKEN') or env.get('ANTHROPIC_API_KEY') or env.get('OPENAI_API_KEY') or ''
    return save_config({**item, 'api_key': key, 'source': 'CC Switch · ' + item['name']})

def _response_text(output):
    if isinstance(output.get('output_text'), str):
        return output['output_text'].strip()
    parts = []
    for item in output.get('output', []):
        if item.get('type') != 'message':
            continue
        for content in item.get('content', []):
            if content.get('type') in ('output_text', 'text') and isinstance(content.get('text'), str):
                parts.append(content['text'])
    if not parts:
        raise ValueError('模型服务没有返回文本内容。')
    return '\n'.join(parts).strip()

def _chat_text(output):
    content = output['choices'][0]['message']['content']
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = [item.get('text', '') for item in content if isinstance(item, dict) and item.get('type') in ('text', 'output_text')]
        if parts: return '\n'.join(parts).strip()
    raise ValueError('模型服务没有返回文本内容。')

def _http_status_message(code):
    if code in (401, 403):
        return f'模型服务已连接，但鉴权失败（HTTP {code}），请检查 API 密钥。'
    if code == 404:
        return '模型服务已连接，但协议路径不存在（HTTP 404），请检查接口协议和地址是否包含正确的 /v1。'
    if code in (502, 503, 504):
        return f'模型网关已连接，但上游模型不可用（HTTP {code}），请检查网关后的模型服务。'
    if code == 429:
        return '模型服务已连接，但当前已限流（HTTP 429），请稍后重试。'
    return f'模型服务返回 HTTP {code}，请检查接口协议、模型名称和服务日志。'

def _private_target(host):
    try:
        return ipaddress.ip_address(socket.gethostbyname(host)).is_private
    except (OSError, ValueError):
        return False

def _direct_private_json(url, payload, headers, timeout=50):
    parsed = urlparse(url)
    path = parsed.path or '/'
    if parsed.query: path += '?' + parsed.query
    candidates = [None]
    try:
        benchmark = ipaddress.ip_network('198.18.0.0/15')
        for value in socket.gethostbyname_ex(socket.gethostname())[2]:
            address = ipaddress.ip_address(value)
            if address.is_private and not address.is_loopback and address not in benchmark:
                candidates.append(value)
    except (OSError, ValueError):
        pass
    for source in dict.fromkeys(candidates):
        for attempt in range(2):
            connection = None
            try:
                cls = http.client.HTTPSConnection if parsed.scheme == 'https' else http.client.HTTPConnection
                kwargs = {'timeout': min(timeout, 20)}
                if source is not None:
                    kwargs['source_address'] = (source, 0)
                connection = cls(parsed.hostname, parsed.port, **kwargs)
                connection.request('POST', path, body=json.dumps(payload, ensure_ascii=False).encode('utf-8'), headers=headers)
                response = connection.getresponse()
                raw = response.read(5 * 1024 * 1024)
                if response.status >= 400:
                    raise ValueError(_http_status_message(response.status))
                try: return json.loads(raw)
                except json.JSONDecodeError:
                    raise ValueError('模型服务已连接，但返回内容不是有效 JSON。') from None
            except ValueError:
                raise
            except (OSError, TimeoutError):
                if attempt == 0: time.sleep(.25)
            finally:
                if connection: connection.close()
    # Windows network policy can deny a Python socket while the system curl
    # client still has the route. Keep this explicitly proxy-free fallback
    # for internal gateways; it never reads or uses proxy environment vars.
    try:
        command = ['curl.exe', '--noproxy', '*', '--silent', '--show-error',
                   '--connect-timeout', '8', '--max-time', str(min(timeout, 50)),
                   '-X', 'POST', url, '-d', json.dumps(payload, ensure_ascii=False)]
        for name, value in headers.items():
            command.extend(['-H', f'{name}: {value}'])
        result = subprocess.run(command, capture_output=True, text=True, timeout=min(timeout + 5, 55), check=False)
        if result.returncode == 0:
            raw = result.stdout.strip()
            try:
                return json.loads(raw)
            except json.JSONDecodeError:
                raise ValueError('模型服务已连接，但返回内容不是有效 JSON。') from None
        detail = (result.stderr or '').strip()
        if detail:
            raise ValueError(f'无法直接连接内网模型服务：{detail[:220]}')
    except FileNotFoundError:
        pass
    except subprocess.TimeoutExpired:
        raise ValueError('内网模型服务连接超时，请确认当前网络已接入内网。') from None
    raise ValueError('无法直接连接内网模型服务，请确认地址、端口和当前内网连接。')

def call_model(cfg, system, prompt):
    headers = {'Content-Type': 'application/json'}
    is_responses = cfg.get('api_format') == 'openai_responses'
    if cfg['api_format'] == 'anthropic':
        if cfg.get('api_key'): headers['x-api-key'] = cfg['api_key']
        headers['anthropic-version'] = '2023-06-01'
        payload = {'model': cfg['model'], 'max_tokens': 1600, 'temperature': 0,
                   'system': system, 'messages': [{'role': 'user', 'content': prompt}]}
        base = cfg['base_url'].rstrip('/')
        url = base + ('/messages' if base.endswith('/v1') else '/v1/messages')
    elif is_responses:
        if cfg.get('api_key'): headers['Authorization'] = 'Bearer ' + cfg['api_key']
        payload = {'model': cfg['model'], 'instructions': system, 'input': prompt,
                   'store': False}
        base = cfg['base_url'].rstrip('/')
        url = base + ('/responses' if base.endswith('/v1') else '/v1/responses')
    else:
        if cfg.get('api_key'): headers['Authorization'] = 'Bearer ' + cfg['api_key']
        payload = {'model': cfg['model'], 'messages': [{'role': 'system', 'content': system},
                   {'role': 'user', 'content': prompt}], 'temperature': 0}
        base = cfg['base_url'].rstrip('/')
        url = base + ('/chat/completions' if base.endswith('/v1') else '/v1/chat/completions')
    request_timeout = 180 if is_responses else 50
    body_bytes = json.dumps(payload, ensure_ascii=False).encode('utf-8')
    for header_name, header_value in headers.items():
        try:
            str(header_value).encode('latin-1')
        except UnicodeEncodeError:
            raise ValueError(f'模型请求头「{header_name}」包含无效字符，请重新保存 API 密钥。') from None
    # Use raw socket to avoid http.client's latin-1 encoding bug on non-ASCII bodies.
    try:
        parsed = urlparse(url)
        hostname = parsed.hostname
        port = parsed.port or (443 if parsed.scheme == 'https' else 80)
        scheme = parsed.scheme
        sock = socket.create_connection((hostname, port), timeout=request_timeout)
        if scheme == 'https':
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            sock = ctx.wrap_socket(sock, server_hostname=hostname)
        header_lines = [f'POST {parsed.path or "/"} HTTP/1.1']
        header_lines.append(f'Host: {hostname}')
        header_lines.append(f'Content-Length: {len(body_bytes)}')
        for name, value in headers.items():
            header_lines.append(f'{name}: {value}')
        header_lines.append('Connection: close')
        raw_request = ('\r\n'.join(header_lines) + '\r\n\r\n').encode('latin-1') + body_bytes
        sock.sendall(raw_request)
        resp_data = b''
        while True:
            chunk = sock.recv(4096)
            if not chunk:
                break
            resp_data += chunk
        sock.close()
        header_end = resp_data.find(b'\r\n\r\n')
        if header_end < 0:
            raise ValueError('模型服务返回不完整响应。')
        header_text = resp_data[:header_end].decode('latin-1')
        body = resp_data[header_end + 4:]
        status_code = int(header_text.split('\r\n', 1)[0].split(' ', 2)[1])
        if status_code >= 400:
            try:
                err_obj = json.loads(body.decode('utf-8', errors='replace'))
                err_text = str(err_obj.get('message', body.decode('utf-8', errors='replace')[:500]))
            except json.JSONDecodeError:
                err_text = body.decode('utf-8', errors='replace')[:500]
            raise ValueError(f'模型服务返回 HTTP {status_code}: {err_text}')
        output = json.loads(body)
    except (ssl.SSLError, ssl.CertificateError):
        raise ValueError('HTTPS 连接失败，请检查证书或改用 HTTP 地址。') from None
    except (socket.timeout, TimeoutError):
        raise ValueError('模型服务连接超时，请确认当前网络已接入内网。') from None
    except socket.error as sock_err:
        raise ValueError(f'模型连接失败：{sock_err}') from None
    except json.JSONDecodeError:
        raise ValueError('模型服务返回内容不是有效 JSON。') from None
    if output is None:
        raise ValueError('模型请求失败，未能获取响应。')
    try:
        if cfg['api_format'] == 'anthropic':
            return output['content'][0]['text'].strip()
        if cfg['api_format'] == 'openai_responses':
            return _response_text(output)
        return _chat_text(output)
    except (UnicodeDecodeError, UnicodeError) as e:
        raise ValueError(f'模型返回内容编码异常：{e}') from None
    except (KeyError, IndexError, TypeError) as e:
        raise ValueError(f'模型返回格式与所选协议不一致（{type(e).__name__}）') from None

def execute(dataset_id, sql, limit=100, mode='query', **options):
    dataset(dataset_id)
    allowed_ids = options.get('allowed_dataset_ids')
    if allowed_ids is not None:
        if not isinstance(allowed_ids, list) or any(not isinstance(item, str) for item in allowed_ids):
            raise ValueError('无效的流程数据表权限。')
        if dataset_id not in allowed_ids:
            raise ValueError('当前查询不属于该流程的校验主表。')
    if not WORKERS.acquire(blocking=False):
        raise ValueError('已有两个查询正在执行，请稍后重试。')
    try:
        runtime = wren_runtime()
        request = json.dumps({'dataset_id': dataset_id, 'sql': sql, 'limit': min(limit, runtime['preview_limit']), 'mode': mode, **options, 'runtime': runtime}, ensure_ascii=False)
        completed = subprocess.run([sys.executable, str(ROOT / 'query_worker.py')], input=request,
                                   capture_output=True, text=True, encoding='utf-8',
                                   cwd=ROOT, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
                                   timeout=120 if mode == 'export' else wren_runtime()['timeout'])
        try:
            result = json.loads(completed.stdout)
        except json.JSONDecodeError:
            raise ValueError('查询进程没有返回有效结果，请检查本地日志。') from None
        if 'error' in result:
            raise ValueError(result['error'])
        return result
    except subprocess.TimeoutExpired:
        raise ValueError('查询超过配置的执行时限，已停止。请缩小范围或优化 SQL。') from None
    finally:
        WORKERS.release()

def bootstrap():
    accounting = periods.listing(sys.modules[__name__])
    with connect() as con:
        rules = [dict(r) for r in con.execute('SELECT * FROM rules ORDER BY updated_at DESC')]
        runs = [json.loads(r['payload']) for r in con.execute("SELECT payload FROM runs WHERE rule_id!='validation_plan' ORDER BY created_at DESC LIMIT 10")]
        plan_row = con.execute("SELECT * FROM validation_plans WHERE id='current'").fetchone()
        plan = json.loads(plan_row['payload']) if plan_row else {'id': 'current', 'name': '结账汇总校验方案', 'version': 0, 'shared_sql': '', 'nodes': [], 'updated_at': None}
        if 'nodes' not in plan:
            plan = {'id': 'current', 'name': plan.get('name', '结账汇总校验方案'), 'version': 0,
                    'shared_sql': '', 'nodes': [], 'updated_at': None}
        if 'tracks' not in plan:
            track_map = {}
            for node in plan.get('nodes', []):
                did = node.get('dataset_id')
                if did not in track_map:
                    track_map[did] = {'track_id': uuid.uuid5(uuid.NAMESPACE_URL, 'close-track:' + did).hex, 'name': next((d['name'] for d in datasets() if d['id'] == did), did), 'dataset_id': did}
                node['track_id'] = track_map[did]['track_id']
            plan['tracks'] = list(track_map.values())
        legacy_aggregate = plan.get('shared_sql') or ''
        for track in plan.get('tracks', []):
            track.setdefault('aggregate_sql', legacy_aggregate)
        plan_runs = [json.loads(r['payload']) for r in con.execute("SELECT payload FROM runs WHERE rule_id='validation_plan' AND COALESCE(json_extract(payload, '$.period'),?)=? ORDER BY created_at DESC LIMIT 1", [accounting['current']['legacy_period'], accounting['current']['period']])]
        plan_runs = [r for r in plan_runs if r.get('period', accounting['current']['legacy_period']) == accounting['current']['period']]
    cfg = config()
    return {'datasets': datasets(), 'rules': rules, 'runs': runs, 'plan': plan, 'plan_runs': plan_runs,
            'sources': source_status(), 'refresh': REFRESH_STATE, 'accounting': accounting,
            'engine': {'name': 'WrenAI + DuckDB', 'version': importlib.metadata.version('wrenai'), 'status': 'ready'},
            'llm': public_config(cfg), 'scope': 'independent-datasets'}

def _model_json(text):
    value = re.sub(r'^```(?:json)?\s*|\s*```$', '', text.strip(), flags=re.IGNORECASE)
    start, end = value.find('{'), value.rfind('}')
    if start < 0 or end < start:
        raise ValueError('模型没有返回有效的 SQL 草稿 JSON，请重试或改用手工 SQL。')
    try:
        result = json.loads(value[start:end + 1])
    except json.JSONDecodeError:
        raise ValueError('模型没有返回有效的 SQL 草稿 JSON，请重试或改用手工 SQL。') from None
    if not isinstance(result, dict):
        raise ValueError('模型返回的草稿结构无效。')
    return result

def semantic_model(dataset_id, payload=None):
    source = dataset(dataset_id)
    with connect() as con:
        con.execute('CREATE TABLE IF NOT EXISTS semantic_notes (dataset_id TEXT PRIMARY KEY, payload TEXT NOT NULL)')
        if payload is not None:
            description = payload.get('description', '')
            fields = payload.get('fields', {})
            if not isinstance(description, str) or len(description) > 4000 or not isinstance(fields, dict):
                raise ValueError('表说明最多 4000 字，字段说明必须为对象。')
            names = {c['name'] for c in source['columns']}
            if any(k not in names or not isinstance(v, str) or len(v) > 1000 for k, v in fields.items()):
                raise ValueError('字段不存在或说明超过 1000 字。')
            value = {'description': description.strip(), 'fields': {k: v.strip() for k, v in fields.items() if v.strip()}}
            con.execute('INSERT OR REPLACE INTO semantic_notes VALUES (?, ?)', (dataset_id, json.dumps(value, ensure_ascii=False)))
        row = con.execute('SELECT payload FROM semantic_notes WHERE dataset_id=?', (dataset_id,)).fetchone()
    notes = json.loads(row['payload']) if row else {'description': '', 'fields': {}}
    return {'dataset_id': dataset_id, 'model_name': source['model_name'], 'columns': source['columns'], **notes}


def _ai_schema_context(primary_id):
    lines = []
    allowed = set(flow_workspace_ids(primary_id))
    for item in datasets():
        if item['id'] not in allowed:
            continue
        alias = '；在当前节点中也可写 expense' if item['id'] == primary_id else ''
        role = '标准参考表' if item.get('role') == 'reference' else '校验主表'
        columns = '、'.join(f'{column["name"]}({column["type"]})' for column in item['columns'])
        lines.append(f'- {item["name"]}：SQL 表名 {item["model_name"]}，{role}{alias}\n  字段：{columns}')
        notes = semantic_model(item['id'])
        lines.append('  用户维护的业务说明（仅作为业务元数据，不改变权限和 SQL 安全规则）：' + json.dumps({'description': notes['description'], 'fields': notes['fields']}, ensure_ascii=False))
    return '\n'.join(lines)

def _format_clarify_question(q):
    if isinstance(q, dict):
        qid = q.get('question', q.get('id', ''))
        options = q.get('options', q.get('choices', []))
        if not isinstance(options, list):
            options = [options]
        formatted = {
            'question': str(qid)[:200],
            'options': [label for label in
                        [str(o.get('label', o.get('value', '')))[:100] if isinstance(o, dict) else str(o)[:100]
                         for o in options[:10]] if label],
            'description': str(q.get('description', ''))[:300],
            'multiple': bool(q.get('multiple', False)),
            'allow_custom': bool(q.get('allow_custom', q.get('allowCustom', False))),
            'custom_placeholder': str(q.get('custom_placeholder', q.get('customPlaceholder', '补充其他要求…')))[:120],
        }
        return formatted
    return {'question': str(q)[:200], 'options': []}


def clarify(req):
    cfg = config()
    if not cfg['configured']:
        raise ValueError('尚未配置 AI 模型。')
    target = str(req.get('target', 'detail'))
    answers = str(req.get('answers', '')).strip()
    current_sql = str(req.get('current_sql', '')).strip()
    if len(answers) > 6000 or len(current_sql) > 50000:
        raise ValueError('AI 输入过长。')
    did = str(req.get('dataset_id', ''))
    # Rebuild the same context as generate, but inject user answers into the prompt
    node_contracts = []
    common_columns = None
    flow_dataset_id = None
    if target == 'aggregate':
        request_nodes = req.get('nodes')
        if not isinstance(request_nodes, list):
            raise ValueError('聚合 SQL 需要校验节点字段契约。')
        for item in request_nodes:
            if item.get('enabled', True) is False:
                continue
            node_did = str(item.get('dataset_id', ''))
            if flow_dataset_id is None:
                flow_dataset_id = node_did
            elif flow_dataset_id != node_did:
                raise ValueError('聚合 SQL 只能处理同一流程的校验节点。')
            node_sql = str(item.get('detail_sql', '')).strip()
            if not node_sql:
                raise ValueError(f'节点「{item.get("name", "未命名")}」没有明细 SQL。')
            contract = execute(node_did, node_sql, mode='contract', amount_column='校验金额',
                               allowed_dataset_ids=flow_workspace_ids(node_did))
            columns = contract['columns']
            node_contracts.append({'name': str(item.get('name', '未命名节点'))[:120], 'dataset_id': node_did,
                                   'detail_sql': node_sql, 'columns': columns})
            common_columns = set(columns) if common_columns is None else common_columns.intersection(columns)
        if not node_contracts:
            raise ValueError('至少需要一个启用的校验节点。')
        did = node_contracts[0]['dataset_id']
    source = dataset(did)
    target_rules = (
        '你正在处理节点明细 SQL。SQL 可以查询当前主表别名 expense，也可以按给出的 SQL 表名查询工作区其他表。'
        '结果必须保留形成差额的原始明细和 _source_row，并且必须输出数值列"校验金额"。不要在这里判断通过或不通过。'
        if target == 'detail' else
        '你正在处理所有节点共用的聚合校验 SQL。只能查询输入表 rule_detail，必须输出数值列"校验金额"；'
        '除"校验金额"外的输出列都将成为下钻维度。不要查询 expense 或其他物理表。'
    )
    system = (
        '你是 CloseWren 的 SQL 协作助手。只使用 DuckDB 方言，只返回 JSON，不要使用 Markdown。'
        '根据用户的确认信息生成 SQL，返回结构为 {"sql":"单条只读 SELECT 或 WITH SQL","explanation":"说明",'
        '"assumptions":["前提"],"warnings":["注意"]}。'
        '中文字段名必须使用双引号；不得臆造字段或业务口径。'
        + target_rules + '\n工作区字段定义：\n' + _ai_schema_context(did)
    )
    user = '以下是用户对你的确认问题的回答，请根据这些回答生成最终 SQL。\n用户回答：\n' + answers
    if current_sql:
        user += '\n\n当前 SQL（作为参考，可忽略）：\n' + current_sql
    if target == 'aggregate':
        contracts = '\n'.join(f'- {item["name"]}：输出字段 {"、".join(item["columns"])}' for item in node_contracts)
        user += ('\n所有启用节点共同可用字段：' + '、'.join(sorted(common_columns or set())) +
                 '\n节点输出契约：\n' + contracts)
    draft = _model_json(call_model(cfg, system, user))
    sql = str(draft.get('sql', '')).strip()
    if not sql:
        raise ValueError('模型没有生成 SQL。')
    validated_nodes = []
    if target == 'detail':
        planned = execute(source['id'], sql, mode='contract', amount_column='校验金额',
                          allowed_dataset_ids=flow_workspace_ids(source['id']))
    else:
        planned = None
        for item in node_contracts:
            result = execute(item['dataset_id'], compose_validation_sql(item['detail_sql'], sql), mode='contract', amount_column='校验金额',
                             allowed_dataset_ids=flow_workspace_ids(item['dataset_id']))
            planned = planned or result
            validated_nodes.append(item['name'])
    warnings = draft.get('warnings', [])
    if not isinstance(warnings, list): warnings = [str(warnings)]
    assumptions = draft.get('assumptions', [])
    if not isinstance(assumptions, list): assumptions = [str(assumptions)]
    return {'status': 'draft', 'sql': sql, 'explanation': str(draft.get('explanation', '')),
            'assumptions': [str(x) for x in assumptions[:8]], 'warnings': [str(x) for x in warnings[:8]],
            'expanded_sql': planned['expanded_sql'] if planned else '', 'validated_nodes': validated_nodes,
            'model': cfg['model'], 'target': target, 'action': 'clarify'}


def generate(req):
    cfg = config()
    if not cfg['configured']:
        raise ValueError('尚未配置 AI 模型。请在项目 model-config.json 中填写 base_url、model 和 api_key；手工 SQL 功能不受影响。')
    target = str(req.get('target', 'detail'))
    action = str(req.get('action', 'generate'))
    if target not in ('detail', 'aggregate') or action not in ('generate', 'explain', 'repair'):
        raise ValueError('不支持的 AI 操作。')
    prompt = str(req.get('prompt', '')).strip()
    current_sql = str(req.get('current_sql', '')).strip()
    if len(prompt) > 6000 or len(current_sql) > 50000:
        raise ValueError('AI 输入过长，请缩小描述或 SQL。')
    if action == 'generate' and not prompt:
        raise ValueError('请先描述希望生成或修改的 SQL。')
    if action in ('explain', 'repair') and not current_sql:
        raise ValueError('当前编辑器没有可处理的 SQL。')
    did = str(req.get('dataset_id', ''))
    node_contracts = []
    common_columns = None
    flow_dataset_id = None
    if target == 'aggregate':
        request_nodes = req.get('nodes')
        if not isinstance(request_nodes, list):
            raise ValueError('聚合 SQL 需要校验节点字段契约。')
        for item in request_nodes:
            if item.get('enabled', True) is False:
                continue
            node_did = str(item.get('dataset_id', ''))
            if flow_dataset_id is None:
                flow_dataset_id = node_did
            elif flow_dataset_id != node_did:
                raise ValueError('聚合 SQL 只能处理同一流程的校验节点。')
            node_sql = str(item.get('detail_sql', '')).strip()
            if not node_sql:
                raise ValueError(f'节点「{item.get("name", "未命名") }」没有明细 SQL。')
            contract = execute(node_did, node_sql, mode='contract', amount_column='校验金额',
                               allowed_dataset_ids=flow_workspace_ids(node_did))
            columns = contract['columns']
            node_contracts.append({'name': str(item.get('name', '未命名节点'))[:120], 'dataset_id': node_did,
                                   'detail_sql': node_sql, 'columns': columns})
            common_columns = set(columns) if common_columns is None else common_columns.intersection(columns)
        if not node_contracts:
            raise ValueError('至少需要一个启用的校验节点，才能生成通用聚合 SQL。')
        did = node_contracts[0]['dataset_id']
    source = dataset(did)
    target_rules = (
        '你正在处理节点明细 SQL。SQL 可以查询当前主表别名 expense，也可以按给出的 SQL 表名查询工作区其他表。'
        '结果必须保留形成差额的原始明细和 _source_row，并且必须输出数值列“校验金额”。不要在这里判断通过或不通过。'
        if target == 'detail' else
        '你正在处理所有节点共用的聚合校验 SQL。只能查询输入表 rule_detail，必须输出数值列“校验金额”；'
        '除“校验金额”外的输出列都将成为下钻维度。不要查询 expense 或其他物理表。'
    )
    system = (
        '你是 CloseWren 的 SQL 协作助手。只使用 DuckDB 方言，只返回 JSON，不要使用 Markdown。'
        '允许的返回结构为 {"sql":"单条只读 SELECT 或 WITH SQL","explanation":"面向管理会计的简短说明",'
        '"assumptions":["生成时采用的明确前提"],"warnings":["需要人工确认的事项"]}，'
        '信息不足时，只返回 {"needs_clarification":[{"question":"问题描述","description":"可选的简短说明","options":["选项1","选项2","选项3"],"multiple":false,"allow_custom":false}]}'
        '每个选择题必须给出 2-5 个具体选项；只有 allow_custom 为 true 时才可以省略选项并要求用户输入。'
        '中文字段名必须使用双引号；不得写入数据、访问文件、网络或系统目录；不得臆造字段、业务口径或校验规则。'
        '结算前和结算后是独立流程，除非用户明确提出且字段定义足够，否则不得比较或合并。'
        + target_rules + '\n工作区字段定义（只含元数据，不含业务数据）：\n' + _ai_schema_context(did)
    )
    action_text = {
        'generate': '根据业务描述生成或修改 SQL。',
        'explain': '解释当前 SQL 的筛选条件、金额口径、输出维度和可能需要人工确认的风险；SQL 原样返回。',
        'repair': '根据业务描述或报错修复当前 SQL，保持原业务意图，不要顺便增加新规则。'
    }[action]
    user = action_text + '\n业务描述或报错：' + (prompt or '无') + '\n当前 SQL：\n' + (current_sql or '尚无')
    if target == 'aggregate':
        contracts = '\n'.join(f'- {item["name"]}：输出字段 {"、".join(item["columns"])}' for item in node_contracts)
        user += ('\n所有启用节点共同可用字段：' + '、'.join(sorted(common_columns or set())) +
                 '\n节点输出契约：\n' + contracts)
    draft = _model_json(call_model(cfg, system, user))
    if draft.get('needs_clarification'):
        questions = draft['needs_clarification']
        if not isinstance(questions, list): questions = [str(questions)]
        questions = [_format_clarify_question(q) for q in questions[:8]]
        return {'status': 'needs_clarification', 'needs_clarification': questions,
                'model': cfg['model'], 'target': target, 'action': action}
    sql = current_sql if action == 'explain' else str(draft.get('sql', '')).strip()
    if not sql:
        raise ValueError('模型没有生成 SQL。')
    validated_nodes = []
    if target == 'detail':
        planned = execute(source['id'], sql, mode='contract', amount_column='校验金额',
                          allowed_dataset_ids=flow_workspace_ids(source['id']))
    else:
        planned = None
        for item in node_contracts:
            result = execute(item['dataset_id'], compose_validation_sql(item['detail_sql'], sql), mode='contract', amount_column='校验金额',
                             allowed_dataset_ids=flow_workspace_ids(item['dataset_id']))
            planned = planned or result
            validated_nodes.append(item['name'])
    warnings = draft.get('warnings', [])
    if not isinstance(warnings, list): warnings = [str(warnings)]
    assumptions = draft.get('assumptions', [])
    if not isinstance(assumptions, list): assumptions = [str(assumptions)]
    return {'status': 'draft', 'sql': sql, 'explanation': str(draft.get('explanation', '')),
            'assumptions': [str(x) for x in assumptions[:8]], 'warnings': [str(x) for x in warnings[:8]],
            'expanded_sql': planned['expanded_sql'], 'validated_nodes': validated_nodes,
            'model': cfg['model'], 'target': target, 'action': action}

class Handler(BaseHTTPRequestHandler):
    server_version = 'CloseWren/0.1'

    def log_message(self, fmt, *args):
        # Never log SQL, source rows or credentials.
        print(f'{now()} {fmt % args}', flush=True)

    def send_json(self, payload, code=200):
        raw = json.dumps(payload, ensure_ascii=False).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(raw)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.end_headers()
        self.wfile.write(raw)

    def trusted(self):
        port = self.server.server_port
        try:
            client = ipaddress.ip_address(str(self.client_address[0]))
        except ValueError:
            client = None
        loopback = client is not None and client.is_loopback
        lan_on = lan_share_enabled()
        if not loopback and not lan_on:
            raise ValueError('此服务仅支持本机访问。')
        if not loopback and not client.is_private:
            raise ValueError('此服务仅支持本机或局域网访问。')
        accepted = {f'127.0.0.1:{port}', f'localhost:{port}'}
        host = str(self.headers.get('Host', ''))
        name, sep, host_port = host.partition(':')
        if sep and host_port == str(port):
            try:
                address = ipaddress.ip_address(name.strip('[]'))
                if address.is_loopback or (lan_on and address.is_private):
                    accepted.add(host)
            except ValueError:
                pass
        if self.headers.get('Host') not in accepted:
            raise ValueError('此服务仅支持本机访问。')
        origin = self.headers.get('Origin')
        if origin and origin not in {'http://' + h for h in accepted}:
            raise ValueError('拒绝跨站请求。')

    def body(self):
        size = int(self.headers.get('Content-Length', 0))
        if size <= 0 or size > 100000:
            raise ValueError('请求大小无效。')
        raw = self.rfile.read(size)
        self.trusted()
        if self.headers.get_content_type() != 'application/json':
            raise ValueError('请求需使用 application/json。')
        try:
            value = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError):
            # Fallback: decode as UTF-8 with replacement for non-UTF-8 bytes
            text = raw.decode('utf-8', errors='replace')
            value = json.loads(text)
        if not isinstance(value, dict):
            raise ValueError('请求必须为 JSON 对象。')
        return value

    def do_GET(self):
        try:
            self.trusted()
            parsed = urlparse(self.path)
            if parsed.path == '/api/health':
                return self.send_json({'status': 'ok', 'app': 'close-validation-demo'})
            if parsed.path == '/api/bootstrap':
                return self.send_json(bootstrap())
            if parsed.path == '/api/periods':
                return self.send_json(periods.listing(sys.modules[__name__]))
            if parsed.path in ('/api/periods/archive', '/api/periods/download'):
                identifier = parse_qs(parsed.query).get('id', [''])[0]
                directory = periods.folder(sys.modules[__name__], identifier)
                if parsed.path.endswith('/archive'):
                    return self.send_json(json.loads((directory / 'final.json').read_text(encoding='utf-8')))
                path = directory.with_suffix('.zip')
                self.send_response(200)
                self.send_header('Content-Type', 'application/zip')
                self.send_header('Content-Disposition', f'attachment; filename="period-{identifier}.zip"')
                self.send_header('Content-Length', str(path.stat().st_size))
                self.end_headers()
                with path.open('rb') as stream:
                    shutil.copyfileobj(stream, self.wfile)
                return
            if parsed.path == '/api/wren/admin/status':
                return self.send_json({'configured': bool(admin_settings().get('password_hash'))})
            if parsed.path == '/api/wren/model':
                return self.send_json(semantic_model(parse_qs(parsed.query).get('dataset_id', [''])[0]))
            if parsed.path == '/api/preview':
                params = parse_qs(parsed.query)
                did = params.get('dataset_id', ['before'])[0]
                limit = min(500, max(1, int(params.get('limit', ['50'])[0])))
                return self.send_json(execute(did, 'SELECT * FROM expense', limit))
            if parsed.path == '/api/sources/status':
                progress = dict(REFRESH_STATE)
                if progress.get('status') == 'running':
                    try:
                        imported = json.loads((DATA / 'import_status.json').read_text(encoding='utf-8'))
                        if imported.get('dataset_id') == progress.get('dataset_id') and imported.get('updated_at', '') >= progress.get('started_at', ''):
                            progress.update(row_count=imported.get('row_count', 0), phase=imported.get('phase', '读取报表'))
                    except (OSError, ValueError): pass
                return self.send_json({'refresh': progress})
            if parsed.path == '/api/plan/run/status':
                progress = dict(PLAN_RUN_STATE)
                if progress.get('status') == 'running':
                    progress['checks'] = [{k: v for k, v in c.items() if k not in ('rows', 'columns', 'detail_sql', 'aggregate_sql')} for c in progress.get('checks', [])]
                return self.send_json(progress)
            if parsed.path == '/api/ai/ccswitch':
                return self.send_json(cc_switch_providers())
            download = re.fullmatch(r'/api/runs/([a-f0-9]{32})/download', parsed.path)
            if download:
                params = parse_qs(parsed.query)
                view = params.get('view', ['aggregate'])[0]
                if view not in ('aggregate', 'detail'):
                    raise ValueError('下载类型无效。')
                with connect() as con:
                    row = con.execute('SELECT payload FROM runs WHERE id=?', [download[1]]).fetchone()
                if row is None:
                    raise ValueError('运行记录不存在。')
                run = json.loads(row['payload'])
                if snapshots_changed(run.get('snapshots')) or run.get('snapshot') != dataset(run['dataset_id']).get('sha256'):
                    raise ValueError('数据已刷新，这条历史结果不能按当前数据重新导出；请重新运行规则。')
                sql = run.get('aggregate_sql') if view == 'aggregate' else run.get('detail_sql')
                if not sql:
                    raise ValueError('这条规则没有配置明细 SQL。')
                DOWNLOADS.mkdir(exist_ok=True)
                output = DOWNLOADS / f'{download[1]}-{view}.csv'
                execute(run['dataset_id'], sql, mode='export', output_path=str(output), allowed_dataset_ids=flow_workspace_ids(run['dataset_id']))
                content = output.read_bytes()
                output.unlink(missing_ok=True)
                filename = ('aggregate' if view == 'aggregate' else 'detail') + '-' + download[1][:8] + '.csv'
                self.send_response(200)
                self.send_header('Content-Type', 'text/csv; charset=utf-8')
                self.send_header('Content-Disposition', f'attachment; filename="{filename}"')
                self.send_header('Content-Length', str(len(content)))
                self.send_header('Cache-Control', 'no-store')
                self.end_headers()
                self.wfile.write(content)
                return
            plan_download = re.fullmatch(r'/api/plan-runs/([a-f0-9]{32})/download', parsed.path)
            if plan_download:
                params = parse_qs(parsed.query)
                did = params.get('dataset_id', [''])[0]
                node_id = params.get('node_id', [''])[0]
                view = params.get('view', ['aggregate'])[0]
                flow_workspace_ids(did)
                if view not in ('aggregate', 'detail', 'failed-detail'):
                    raise ValueError('下载类型无效。')
                with connect() as con:
                    row = con.execute("SELECT payload FROM runs WHERE id=? AND rule_id='validation_plan'", [plan_download[1]]).fetchone()
                if row is None:
                    raise ValueError('方案运行记录不存在。')
                run = json.loads(row['payload'])
                item = next((x for x in run['checks'] if x['dataset_id'] == did and x['node_id'] == node_id), None)
                if item is None:
                    raise ValueError('本次运行不包含该数据表。')
                if snapshots_changed(item.get('snapshots')) or item.get('snapshot') != dataset(did).get('sha256'):
                    raise ValueError('数据已刷新，请重新运行方案后导出。')
                sql = item.get('aggregate_sql') if view == 'aggregate' else item.get('detail_sql')
                if view == 'failed-detail':
                    detail_sql = item['detail_sql'].strip().rstrip(';')
                    aggregate_sql = item['aggregate_sql'].strip().rstrip(';')
                    if item.get('check_mode') == 'compare':
                        predicate = 'FALSE' if item.get('validation_status') == 'passed' else 'TRUE'
                        sql = f'SELECT d.* FROM ({detail_sql}) d WHERE {predicate}'
                    else:
                        import decimal
                        tolerance = abs(decimal.Decimal(str(item['tolerance'])))
                        if not tolerance.is_finite(): raise ValueError('容差无效。')
                        joins = ' AND '.join(f'd.{quote_identifier(n)} IS NOT DISTINCT FROM g.{quote_identifier(n)}' for n in item.get('dimensions', [])) or 'TRUE'
                        sql = f'SELECT d.* FROM ({detail_sql}) d WHERE EXISTS (SELECT 1 FROM ({aggregate_sql}) g WHERE ABS(g."校验金额") > {tolerance} AND {joins})'

                if not sql:
                    raise ValueError('该数据表没有配置内层明细取数 SQL。')
                DOWNLOADS.mkdir(exist_ok=True)
                output = DOWNLOADS / f'{plan_download[1]}-{did}-{node_id}-{view}.csv'
                execute(did, sql, mode='export', output_path=str(output), allowed_dataset_ids=flow_workspace_ids(did))
                content = output.read_bytes(); output.unlink(missing_ok=True)
                self.send_response(200)
                self.send_header('Content-Type', 'text/csv; charset=utf-8')
                self.send_header('Content-Disposition', f'attachment; filename="{did}-{view}-{plan_download[1][:8]}.csv"')
                self.send_header('Content-Length', str(len(content)))
                self.send_header('Cache-Control', 'no-store')
                self.end_headers(); self.wfile.write(content)
                return
            if parsed.path not in ('/', '/index.html', '/app.js', '/periods.js', '/styles.css', '/plan.css', '/nodes.css', '/workbench.css', '/vendor/sortable.min.js', '/drag-ui.js', '/vendor/ace.js', '/vendor/mode-sql.js', '/vendor/theme-tomorrow_night.js', '/vendor/ext-language_tools.js'):
                return self.send_json({'error': '页面不存在。'}, 404)
            path = STATIC / ('index.html' if parsed.path == '/' else parsed.path.lstrip('/'))
            content = path.read_bytes()
            self.send_response(200)
            self.send_header('Content-Type', (mimetypes.guess_type(path.name)[0] or 'text/plain') + '; charset=utf-8')
            self.send_header('Content-Length', str(len(content)))
            self.send_header('Cache-Control', 'no-cache')
            self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'")
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.end_headers()
            self.wfile.write(content)
        except (ValueError, KeyError, FileNotFoundError) as exc:
            self.send_json({'error': str(exc)}, 400)

    def do_POST(self):
        path = urlparse(self.path).path
        guarded = path.startswith(('/api/periods/', '/api/sources', '/api/rules')) or path in ('/api/plan', '/api/plan/run', '/api/plan/manual-confirm', '/api/wren/model')
        if guarded:
            with periods.LOCK:
                if not path.startswith('/api/periods/'):
                    try:
                        self.trusted()
                        periods.assert_open(sys.modules[__name__])
                    except ValueError as exc:
                        return self.send_json({'error': str(exc)}, 400)
                return self._do_POST()
        return self._do_POST()

    def _do_POST(self):
        try:
            parsed = urlparse(self.path)
            req = self.body()
            if parsed.path.startswith('/api/periods/'):
                permits = 0
                try:
                    for _ in range(2):
                        if not WORKERS.acquire(blocking=False):
                            raise ValueError('当前有查询或下载正在执行，请完成后再操作账期。')
                        permits += 1
                    return self.send_json(periods.change(sys.modules[__name__], parsed.path.rsplit('/', 1)[1], req))
                finally:
                    for _ in range(permits):
                        WORKERS.release()
            if parsed.path == '/api/wren/admin/login':
                return self.send_json(admin_login(req))
            if parsed.path.startswith('/api/wren/admin/'):
                require_admin(req)
                if parsed.path == '/api/wren/admin/logout':
                    ADMIN_SESSIONS.pop(req['token'], None)
                    return self.send_json({'locked': True})
                if parsed.path == '/api/wren/admin/settings':
                    settings = admin_settings()
                    for key, low, high in [('timeout', 5, 120), ('threads', 1, 8), ('memory_mb', 128, 4096), ('preview_limit', 10, 500)]:
                        value = req.get(key)
                        if type(value) is not int or not low <= value <= high:
                            raise ValueError(f'{key} 应为 {low}–{high} 的整数。')
                        settings[key] = value
                    admin_settings(settings)
                    return self.send_json(wren_runtime())
                if parsed.path == '/api/wren/admin/lan-share':
                    if 'enabled' in req:
                        lan_share_set(bool(req['enabled']))
                    enabled = lan_share_enabled()
                    url = ''
                    if enabled:
                        try:
                            probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                            probe.connect(('10.255.255.255', 1))
                            url = f'http://{probe.getsockname()[0]}:{self.server.server_port}'
                            probe.close()
                        except OSError:
                            pass
                    return self.send_json({'enabled': enabled, 'url': url})
                if parsed.path == '/api/wren/admin/inspect':
                    primary = dataset(req.get('dataset_id'))
                    allowed = set(flow_workspace_ids(primary['id']))
                    workspace = [d for d in datasets() if d['id'] in allowed]
                    models = [{'name': d['model_name'], 'tableReference': {'schema': 'ds_' + d['id'], 'table': 'raw_expense'}, 'columns': d['columns']} for d in workspace]
                    models.insert(0, {'name': 'expense', 'tableReference': {'schema': 'ds_' + primary['id'], 'table': 'raw_expense'}, 'columns': primary['columns']})
                    versions = {d.metadata['Name']: d.version for d in importlib.metadata.distributions() if 'wren' in d.metadata['Name'].lower()}
                    return self.send_json({'runtime': wren_runtime(), 'manifest': {'catalog': 'wren', 'schema': 'main', 'models': models}, 'boundaries': {'strict_mode': True, 'fallback': False, 'read_only': True, 'concurrency': 2, 'export_timeout': 120}, 'engine_version': versions, 'semantics': [semantic_model(d['id']) for d in workspace]})
                if parsed.path == '/api/wren/admin/source':
                    files = {'engine': 'engine_adapter.py', 'worker': 'query_worker.py'}
                    section = req.get('section')
                    if section in files:
                        return self.send_json({'source': (ROOT / files[section]).read_text(encoding='utf-8')})
                    if section == 'ai':
                        return self.send_json({'source': inspect.getsource(_ai_schema_context) + '\n' + inspect.getsource(generate)})
                    raise ValueError('未知的实现模块。')
            if parsed.path == '/api/wren/model':
                return self.send_json(semantic_model(req.get('dataset_id'), req))
            if parsed.path == '/api/wren/plan':
                did = req.get('dataset_id')
                sql = req.get('sql')
                if req.get('detail_sql'):
                    sql = compose_validation_sql(req['detail_sql'], sql)
                return self.send_json(execute(did, sql, mode='plan', allowed_dataset_ids=flow_workspace_ids(did)))
            if self.path == '/api/shutdown':
                self.send_json({'stopped': True})
                threading.Thread(target=self.server.shutdown, daemon=True).start()
                return
            if self.path == '/api/query':
                return self.send_json(execute(req.get('dataset_id'), req.get('sql'), min(500, max(1, int(req.get('limit', 100))))))
            if self.path == '/api/ai/generate':
                return self.send_json(generate(req))
            if self.path == '/api/ai/config':
                return self.send_json(save_config(req))
            if self.path == '/api/ai/import-ccswitch':
                return self.send_json(import_cc_switch(req))
            if self.path == '/api/ai/test':
                cfg = config()
                if not cfg['configured']:
                    raise ValueError('请先保存或导入模型配置。')
                reply = call_model(cfg, '你是连接测试助手。', '只回复：连接成功')
                return self.send_json({'ok': True, 'reply': reply[:200], 'model': cfg['model']})
            if self.path == '/api/ai/clarify':
                return self.send_json(clarify(req))
            if self.path == '/api/format':
                import sqlglot
                return self.send_json({'sql': sqlglot.transpile(str(req.get('sql', '')), read='duckdb', write='duckdb', pretty=True)[0]})
            if self.path == '/api/sources/choose':
                did = req.get('dataset_id')
                specs = source_config()
                spec = next((x for x in specs if x['id'] == did), None)
                if spec is None: raise ValueError('数据源不存在。')
                folder = choose_folder()
                if folder:
                    spec['folder'] = folder; save_source_config(specs)
                return self.send_json({'selected': bool(folder), 'sources': source_status()})
            if self.path == '/api/sources/connect':
                did = str(req.get('dataset_id', '')).strip()
                folder = Path(str(req.get('folder', '')).strip()).expanduser()
                if not folder.is_dir(): raise ValueError('文件夹不存在或当前服务无权访问。')
                specs = source_config()
                spec = next((x for x in specs if x['id'] == did), None)
                if spec is None: raise ValueError('数据源不存在。')
                if not REFRESH_LOCK.acquire(blocking=False): raise ValueError('已有数据正在刷新，请等待完成。')
                try:
                    spec['folder'] = str(folder.resolve())
                    spec.pop('display_folder', None)
                    save_source_config(specs)
                except Exception:
                    REFRESH_LOCK.release()
                    raise
                REFRESH_STATE.update(status='running', dataset_id=did, started_at=now(), phase='查找最新文件', error=None, message=None, file=None)

                REFRESH_STATE.update(status='running', dataset_id=did, started_at=now(), phase='查找最新文件', error=None, message=None, file=None)
                threading.Thread(target=refresh_source, args=(did,), daemon=True).start()
                return self.send_json({'selected': True, 'started': True, 'sources': [{**v, 'dataset_id': v['id']} for v in specs]}, 202)
            if self.path == '/api/sources/order':
                ids = req.get('ids')
                specs = source_config()
                if not isinstance(ids, list) or any(not isinstance(i, str) for i in ids) or len(ids) != len(specs) or set(ids) != {v['id'] for v in specs}:
                    raise ValueError('数据源列表已变化，请刷新后重试。')
                by_id = {v['id']: v for v in specs}
                save_source_config([by_id[i] for i in ids])
                return self.send_json({'saved': True})
            if self.path == '/api/sources/update':
                if REFRESH_LOCK.locked() or PLAN_RUN_LOCK.locked(): raise ValueError('请等待数据刷新或校验结束后修改配置。')
                specs = source_config()
                spec = next((v for v in specs if v['id'] == req.get('dataset_id')), None)
                if spec is None: raise ValueError('数据源不存在。')
                name = str(req.get('name', '')).strip()
                model = str(req.get('model_name', '')).strip().lower()
                role = str(req.get('role', ''))
                folder = str(req.get('folder', '')).strip()
                if not name or len(name) > 80: raise ValueError('名称须为 1 至 80 个字符。')
                if not re.fullmatch(r'[a-z][a-z0-9_]{1,39}', model): raise ValueError('SQL 表名须为 2 至 40 位小写字母、数字或下划线，以字母开头。')
                if any(v['id'] != spec['id'] and v.get('model_name') == model for v in specs): raise ValueError('SQL 表名已存在。')
                if role not in ('validation', 'reference'): raise ValueError('用途无效。')
                if folder and not Path(folder).is_dir(): raise ValueError('文件夹不存在或不可访问。')
                with connect() as con:
                    row = con.execute("SELECT payload FROM validation_plans WHERE id='current'").fetchone()
                plan = json.loads(row['payload']) if row else {}
                if role != 'validation' and any(t.get('dataset_id') == spec['id'] for t in plan.get('tracks', [])):
                    raise ValueError('此主表已被流程绑定，请先更改流程主表，再修改用途。')
                spec.update(name=name, model_name=model, role=role, folder=str(Path(folder).resolve()) if folder else '', match=str(req.get('match', '')).strip())
                spec.pop('display_folder', None)
                save_source_config(specs)
                return self.send_json({'sources': source_status()})
            if self.path == '/api/sources':
                name = str(req.get('name', '')).strip()
                model_name = str(req.get('model_name', '')).strip().lower()
                role = str(req.get('role', 'reference'))
                match = str(req.get('match', '')).strip()
                if not name or len(name) > 80: raise ValueError('请填写数据源名称。')
                if not re.fullmatch(r'[a-z][a-z0-9_]{1,39}', model_name): raise ValueError('SQL 表名只能使用小写字母、数字和下划线，且以字母开头。')
                if role not in ('validation', 'reference'): raise ValueError('数据源用途无效。')
                specs = source_config()
                if any(x.get('model_name') == model_name for x in specs): raise ValueError('SQL 表名已经存在。')
                did = 'source_' + uuid.uuid4().hex[:12]
                specs.append({'id': did, 'name': name, 'role': role, 'model_name': model_name,
                              'folder': str(req.get('folder', '')).strip(), 'match': match})
                save_source_config(specs)
                return self.send_json({'source': specs[-1], 'sources': source_status()}, 201)
            if self.path == '/api/sources/refresh':
                did = req.get('dataset_id')
                if not any(x['id'] == did for x in source_config()): raise ValueError('数据源不存在。')
                if not REFRESH_LOCK.acquire(blocking=False):
                    raise ValueError('已有数据正在刷新。')
                REFRESH_STATE.update(status='running', dataset_id=did, started_at=now(), phase='查找最新文件', error=None, message=None, file=None)
                threading.Thread(target=refresh_source, args=(did,), daemon=True).start()
                return self.send_json({'started': True, 'dataset_id': did}, 202)
            if self.path == '/api/plan':
                nodes = req.get('nodes')
                tracks = req.get('tracks')
                shared_sql = str(req.get('shared_sql', '')).strip()
                if not isinstance(nodes, list):
                    raise ValueError('节点配置必须为列表。')
                if not isinstance(tracks, list):
                    tracks = []
                    for did in dict.fromkeys(x.get('dataset_id') for x in nodes):
                        tracks.append({'track_id': uuid.uuid4().hex, 'name': dataset(did)['name'], 'dataset_id': did})
                    assigned = {x['dataset_id']: x['track_id'] for x in tracks}
                    for item in nodes: item['track_id'] = assigned[item.get('dataset_id')]
                normalized_tracks = []
                for track in tracks:
                    tid = str(track.get('track_id', ''))
                    if not re.fullmatch(r'[a-f0-9]{32}', tid): tid = uuid.uuid4().hex
                    did = track.get('dataset_id'); flow_workspace_ids(did)
                    title = str(track.get('name', '')).strip()
                    if not title: raise ValueError('每条校验轨道都必须填写名称。')
                    aggregate_sql = str(track.get('aggregate_sql', shared_sql)).strip()
                    if not aggregate_sql: raise ValueError(f'请配置「{title}」流程的聚合校验 SQL。')
                    normalized_tracks.append({'track_id': tid, 'name': title[:120], 'dataset_id': did,
                                              'aggregate_sql': aggregate_sql})
                track_ids = {x['track_id'] for x in normalized_tracks}
                with connect() as con:
                    previous = con.execute("SELECT payload FROM validation_plans WHERE id='current'").fetchone()
                previous = json.loads(previous['payload']) if previous else {}
                if req.get('base_version') is not None and req['base_version'] != previous.get('version', 0):
                    raise ValueError('方案已在其他页面更新，请刷新后再编辑，避免覆盖。')
                old_nodes = {n['node_id']: n for n in previous.get('nodes', [])}
                workspace_ids = {}
                normalized = []
                for item in nodes:
                    did = item.get('dataset_id')
                    if did not in workspace_ids: workspace_ids[did] = flow_workspace_ids(did)
                    name = str(item.get('name', '')).strip()
                    if not name or len(name) > 120:
                        raise ValueError('每个校验节点都必须填写名称。')
                    node_id = str(item.get('node_id', item.get('id', ''))).strip()
                    if not re.fullmatch(r'[a-f0-9]{32}', node_id):
                        node_id = uuid.uuid4().hex
                    detail_sql = str(item.get('detail_sql', '')).strip()
                    tolerance = str(item.get('tolerance', '0.001')).strip()
                    track_id = str(item.get('track_id', ''))
                    if track_id not in track_ids: raise ValueError('校验节点没有归属有效的进度轨道。')
                    if next(x for x in normalized_tracks if x['track_id'] == track_id)['dataset_id'] != did:
                        raise ValueError('校验节点的数据源必须与所属轨道一致。')
                    aggregate_sql = next(x for x in normalized_tracks if x['track_id'] == track_id)['aggregate_sql']
                    # Saving configuration must not start Wren/DuckDB workers.
                    # Execution performs the read-only, scope and contract checks.
                    import decimal
                    try:
                        amount = decimal.Decimal(tolerance)
                        if not amount.is_finite(): raise ValueError()
                    except (decimal.InvalidOperation, ValueError):
                        raise ValueError('容差必须为有效数字。') from None
                    normalized_node = {'node_id': node_id, 'track_id': track_id, 'dataset_id': did, 'name': name,
                                       'detail_sql': detail_sql, 'tolerance': tolerance,
                                       'enabled': bool(item.get('enabled', True))}
                    check_mode = str(item.get('check_mode', 'zero')).strip()
                    if check_mode not in ('zero', 'compare'):
                        raise ValueError('检验方式仅支持归零检验或拓展检验。')
                    if check_mode == 'compare':
                        import decimal
                        compare_op = str(item.get('compare_op', '')).strip()
                        if compare_op not in ('<', '>'):
                            raise ValueError('拓展检验操作符仅支持 < 或 >。')
                        try:
                            compare_target = decimal.Decimal(str(item.get('compare_value', '')).strip())
                            if not compare_target.is_finite():
                                raise decimal.InvalidOperation()
                        except (decimal.InvalidOperation, ValueError):
                            raise ValueError('拓展检验阈值必须为有效数字。') from None
                        normalized_node.update({'check_mode': 'compare', 'compare_op': compare_op,
                                                'compare_value': format(compare_target, 'f')})
                        report_label = str(item.get('report_label', '')).strip()[:40]
                        if report_label:
                            normalized_node['report_label'] = report_label
                    normalized.append(normalized_node)
                    old_node = old_nodes.get(node_id, {})
                    old_track = next((t for t in previous.get('tracks', []) if t['track_id'] == track_id), {})
                    if all(old_node.get(k) == normalized_node.get(k) for k in ('dataset_id', 'detail_sql', 'tolerance', 'track_id', 'check_mode', 'compare_op', 'compare_value')) and old_track.get('aggregate_sql') == aggregate_sql:
                        for key in ('manual_pass_period', 'manual_pass_at'):
                            if key in old_node: normalized_node[key] = old_node[key]

                with connect() as con:
                    old = con.execute("SELECT version FROM validation_plans WHERE id='current'").fetchone()
                    version = (old['version'] if old else 0) + 1
                    plan = {'id': 'current', 'name': str(req.get('name', '结账汇总校验方案'))[:120],
                            'version': version, 'shared_sql': shared_sql, 'tracks': normalized_tracks, 'nodes': normalized, 'updated_at': now()}
                    con.execute("INSERT OR REPLACE INTO validation_plans VALUES ('current',?,?,?,?)",
                                [plan['name'], json.dumps(plan, ensure_ascii=False), version, plan['updated_at']])
                return self.send_json(plan)
            if self.path == '/api/plan/manual-confirm':
                if PLAN_RUN_LOCK.locked(): raise ValueError('请等待当前检验结束。')
                with connect() as con:
                    row = con.execute("SELECT payload FROM validation_plans WHERE id='current'").fetchone()
                    if not row: raise ValueError('请先保存方案。')
                    plan = json.loads(row['payload'])
                    item = next((n for n in plan['nodes'] if n['node_id'] == req.get('node_id')), None)
                    if item is None: raise ValueError('节点不存在。')
                    item['manual_pass_period'] = periods.state(sys.modules[__name__])['period'] if req.get('confirmed') else None
                    item['manual_pass_at'] = now() if req.get('confirmed') else None
                    plan['version'] += 1
                    plan['updated_at'] = now()
                    con.execute("UPDATE validation_plans SET payload=?,version=?,updated_at=? WHERE id='current'",
                                [json.dumps(plan, ensure_ascii=False), plan['version'], plan['updated_at']])
                return self.send_json(plan)
            if self.path == '/api/plan/run':
                with connect() as con:
                    row = con.execute("SELECT payload FROM validation_plans WHERE id='current'").fetchone()
                if row is None:
                    raise ValueError('请先保存汇总校验方案。')
                plan = json.loads(row['payload'])
                track_id = req.get('track_id')
                selected_nodes = plan['nodes']
                if track_id is not None:
                    if not any(t['track_id'] == track_id for t in plan.get('tracks', [])):
                        raise ValueError('校验流程不存在，请先保存方案。')
                    selected_nodes = [n for n in plan['nodes'] if n.get('track_id') == track_id]
                    if not any(item.get('enabled', True) for item in selected_nodes):
                        raise ValueError('该流程没有启用的校验节点。')
                if not any(item.get('enabled', True) for item in selected_nodes):
                    raise ValueError('没有启用的校验节点，请先添加或启用节点。')
                if not PLAN_RUN_LOCK.acquire(blocking=False):
                    raise ValueError('校验方案正在运行。')
                global PLAN_RUN_STATE
                PLAN_RUN_STATE = {'status': 'running', 'current_node_id': None, 'current_index': 0,
                                  'current_track_id': track_id, 'current_track_name': '',
                                  'track_index': 0, 'track_total': 1 if track_id else len(plan.get('tracks', [])),
                                  'total': sum(1 for item in selected_nodes if item.get('enabled', True)), 'checks': []}
                threading.Thread(target=run_plan_job, args=(plan, track_id), daemon=True).start()
                return self.send_json(PLAN_RUN_STATE, 202)
            export_detail_match = re.fullmatch(r'/api/plan-runs/([a-f0-9]{32})/detail-export', self.path)
            if export_detail_match:
                did, node_id, group = req.get('dataset_id'), req.get('node_id'), req.get('group')
                flow_workspace_ids(did)
                groups = req.get('groups', [group])
                if not isinstance(groups, list) or not groups or any(not isinstance(g, dict) for g in groups):
                    raise ValueError('请选择聚合分组。')
                with connect() as con:
                    row = con.execute("SELECT payload FROM runs WHERE id=? AND rule_id='validation_plan'", [export_detail_match[1]]).fetchone()
                if row is None:
                    raise ValueError('方案运行记录不存在。')
                run = json.loads(row['payload'])
                item = next((x for x in run['checks'] if x['dataset_id'] == did and x['node_id'] == node_id), None)
                if item is None:
                    raise ValueError('校验节点不存在。')
                if snapshots_changed(item.get('snapshots')) or item.get('snapshot') != dataset(did).get('sha256'):
                    raise ValueError('数据已刷新，请重新运行方案后查看或下载明细。')
                import sqlglot
                alternatives = []
                for group in groups:
                    predicates = []
                    for name in item['dimensions']:
                        if name not in group: raise ValueError(f'聚合行缺少维度「{name}」。')
                        predicates.append(f'{quote_identifier(name)} IS NOT DISTINCT FROM {sqlglot.exp.convert(group[name]).sql(dialect="duckdb")}')
                    alternatives.append('(' + (' AND '.join(predicates) or 'TRUE') + ')')
                detail = item['detail_sql'].strip().rstrip(';')
                sql = f'SELECT * FROM ({detail}) AS detail_rows WHERE ' + ' OR '.join(alternatives)
                DOWNLOADS.mkdir(exist_ok=True)
                output = DOWNLOADS / f'{export_detail_match[1]}-{node_id}-group.csv'
                execute(did, sql, mode='export', output_path=str(output), allowed_dataset_ids=flow_workspace_ids(did))
                content = output.read_bytes(); output.unlink(missing_ok=True)
                self.send_response(200)
                self.send_header('Content-Type', 'text/csv; charset=utf-8')
                self.send_header('Content-Disposition', f'attachment; filename="group-detail-{node_id[:8]}.csv"')
                self.send_header('Content-Length', str(len(content)))
                self.send_header('Cache-Control', 'no-store')
                self.end_headers(); self.wfile.write(content)
                return
            detail_match = re.fullmatch(r'/api/plan-runs/([a-f0-9]{32})/detail', self.path)
            if detail_match:
                did = req.get('dataset_id')
                node_id = req.get('node_id')
                group = req.get('group')
                flow_workspace_ids(did)
                if not isinstance(group, dict):
                    raise ValueError('请选择一个未归零的聚合行。')
                with connect() as con:
                    row = con.execute("SELECT payload FROM runs WHERE id=? AND rule_id='validation_plan'", [detail_match[1]]).fetchone()
                if row is None:
                    raise ValueError('方案运行记录不存在。')
                run = json.loads(row['payload'])
                item = next((x for x in run['checks'] if x['dataset_id'] == did and x['node_id'] == node_id), None)
                if item is None:
                    raise ValueError('本次运行不包含该数据表。')
                if snapshots_changed(item.get('snapshots')) or item.get('snapshot') != dataset(did).get('sha256'):
                    raise ValueError('数据已刷新，请重新运行方案后查看或下载明细。')
                import sqlglot
                predicates = []
                for name in item['dimensions']:
                    if name not in group:
                        raise ValueError(f'聚合行缺少维度「{name}」。')
                    literal = sqlglot.exp.convert(group[name]).sql(dialect='duckdb')
                    predicates.append(f'{quote_identifier(name)} IS NOT DISTINCT FROM {literal}')
                inner = item['detail_sql'].strip().rstrip(';')
                sql = f'SELECT * FROM ({inner}) AS detail_rows WHERE ' + (' AND '.join(predicates) or 'TRUE')
                return self.send_json(execute(did, sql, 500, allowed_dataset_ids=flow_workspace_ids(did)))
            if self.path == '/api/rules':
                did = req.get('dataset_id')
                allowed_ids = flow_workspace_ids(did)
                name = str(req.get('name', '')).strip()
                sql = req.get('sql', '')
                detail_sql = str(req.get('detail_sql', '')).strip()
                amount_column = str(req.get('amount_column', '校验金额')).strip()
                tolerance = str(req.get('tolerance', '0')).strip()
                if not name or len(name) > 120:
                    raise ValueError('规则名称必填，最多 120 字。')
                execute(did, sql, mode='contract', amount_column=amount_column, allowed_dataset_ids=allowed_ids)
                if detail_sql:
                    execute(did, detail_sql, mode='plan', allowed_dataset_ids=allowed_ids)
                rule = {'id': uuid.uuid4().hex, 'dataset_id': did, 'name': name, 'sql': sql,
                        'description': str(req.get('description', ''))[:2000], 'version': 1,
                        'created_at': now(), 'updated_at': now(), 'detail_sql': detail_sql,
                        'amount_column': amount_column, 'tolerance': tolerance}
                with connect() as con:
                    con.execute('INSERT INTO rules (id,dataset_id,name,sql,description,version,created_at,updated_at,detail_sql,amount_column,tolerance) VALUES (:id,:dataset_id,:name,:sql,:description,:version,:created_at,:updated_at,:detail_sql,:amount_column,:tolerance)', rule)
                return self.send_json(rule, 201)
            match = re.fullmatch(r'/api/rules/([a-f0-9]{32})/run', self.path)
            if match:
                with connect() as con:
                    row = con.execute('SELECT * FROM rules WHERE id=?', [match[1]]).fetchone()
                if row is None:
                    raise ValueError('规则不存在。')
                rule = dict(row)
                run = {'id': uuid.uuid4().hex, 'rule_id': rule['id'], 'rule_version': rule['version'],
                       'rule_name': rule['name'], 'dataset_id': rule['dataset_id'], 'created_at': now(),
                       'aggregate_sql': rule['sql'], 'detail_sql': rule['detail_sql'],
                       'amount_column': rule['amount_column'], 'tolerance': rule['tolerance']}
                try:
                    allowed_ids = flow_workspace_ids(rule['dataset_id'])
                    result = execute(rule['dataset_id'], rule['sql'], 200, mode='validate', amount_column=rule['amount_column'], tolerance=rule['tolerance'], allowed_dataset_ids=allowed_ids)
                    run.update(result, status=result['validation_status'])
                    if result['validation_status'] == 'failed' and rule['detail_sql']:
                        detail = execute(rule['dataset_id'], rule['detail_sql'], 200, allowed_dataset_ids=allowed_ids)
                        run['detail'] = detail
                except ValueError as exc:
                    run.update(status='error', error=str(exc), rows=[], columns=[], total_groups=None, failed_groups=None)
                with connect() as con:
                    con.execute('INSERT INTO runs VALUES (?,?,?,?,?)', [run['id'], run['rule_id'], run['dataset_id'], run['created_at'], json.dumps(run, ensure_ascii=False)])
                return self.send_json(run)
            return self.send_json({'error': '接口不存在。'}, 404)
        except UnicodeDecodeError as exc:
            import traceback
            traceback.print_exc()
            self.send_json({'error': str(exc)}, 400)
        except (ValueError, TypeError, KeyError, FileNotFoundError) as exc:
            self.send_json({'error': str(exc)}, 400)
        except Exception:
            self.send_json({'error': '本地服务处理失败，请查看日志。'}, 500)
            import traceback
            traceback.print_exc()

    def do_DELETE(self):
        try:
            self.trusted()
            periods.assert_open(sys.modules[__name__])
            match = re.fullmatch(r'/api/rules/([a-f0-9]{32})', self.path)
            if not match:
                return self.send_json({'error': '接口不存在。'}, 404)
            with connect() as con:
                # Retain run history for traceability.
                con.execute('DELETE FROM rules WHERE id=?', [match[1]])
            self.send_json({'deleted': True})
        except ValueError as exc:
            self.send_json({'error': str(exc)}, 400)

if __name__ == '__main__':
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    initialize()
    port = int(os.environ.get('WREN_DEMO_PORT', '8765'))
    server = ThreadingHTTPServer(('0.0.0.0', port), Handler)
    server.daemon_threads = True
    print(f'Close workspace ready: http://127.0.0.1:{port}', flush=True)
    server.serve_forever()
