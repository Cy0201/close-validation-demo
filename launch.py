import json
import os
from pathlib import Path
import subprocess
import sys
import time
from urllib.request import Request, urlopen
import webbrowser

ROOT = Path(__file__).resolve().parent
PORT = int(os.environ.get('WREN_DEMO_PORT', '8765'))
URL = f'http://127.0.0.1:{PORT}'

def healthy():
    try:
        with urlopen(URL + '/api/health', timeout=2) as response:
            return json.load(response).get('app') == 'close-validation-demo'
    except Exception:
        return False

if '--stop' in sys.argv:
    if healthy():
        with urlopen(Request(URL + '/api/shutdown', data=b'{}', headers={'Content-Type':'application/json'}), timeout=3):
            pass
        print('Demo stopped.')
    else:
        print('Demo is not running.')
    sys.exit(0)

if not healthy():
    # First start imports the bundled examples. Later source refreshes are managed
    # from the UI and must not be overwritten by the original filenames here.
    required = [ROOT / 'data' / item / 'manifest.json' for item in ('before', 'after')]
    if not all(path.exists() for path in required) and not (ROOT / 'data' / 'workspace.sqlite3').exists():
        subprocess.run([sys.executable, str(ROOT / 'import_data.py')], cwd=ROOT, check=True)
    logs = ROOT / 'logs'
    logs.mkdir(exist_ok=True)
    with (logs / 'server.log').open('a', encoding='utf-8') as out, (logs / 'server-error.log').open('a', encoding='utf-8') as err:
        process = subprocess.Popen([sys.executable, str(ROOT / 'server.py')], cwd=ROOT,
                                   stdin=subprocess.DEVNULL, stdout=out, stderr=err,
                                   creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    for _ in range(60):
        if healthy():
            break
        if process.poll() is not None:
            raise RuntimeError('Server failed to start. Check logs/server-error.log or port 8765.')
        time.sleep(.25)
    else:
        raise RuntimeError('Server did not become ready. Check logs/server-error.log.')
print(URL)
if '--no-browser' not in sys.argv:
    webbrowser.open(URL)
