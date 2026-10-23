"""Start local DB and app; only stop child processes created by THIS launcher."""
import os
import shutil
import socket
import subprocess
import sys
import time
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def port_open(port):
    with socket.socket() as s:
        s.settimeout(.3)
        return s.connect_ex(('127.0.0.1', port)) == 0


def main():
    os.chdir(ROOT)
    children = []
    env = dict(os.environ)
    # Existing user API keys cannot turn on a paid backend in this project.
    env.setdefault('PRESALES_MODEL_MODE', 'extractive')
    env['PRESALES_DATABASE_URL'] = 'postgresql://postgres@127.0.0.1:55432/postgres?sslmode=disable'
    logs = ROOT/'.storage'
    logs.mkdir(exist_ok=True)
    handles = []
    try:
        if port_open(8128):
            raise SystemExit('Port 8128 is already in use. Existing processes were left running.')
        if not port_open(55432):
            node = shutil.which('node')
            if not node or not (ROOT/'node_modules/@electric-sql/pglite').exists():
                raise SystemExit('Install Node.js and run npm ci first.')
            output = (logs/'postgres.log').open('a', encoding='utf-8')
            handles.append(output)
            p = subprocess.Popen([node, 'scripts/local_postgres.mjs'], env=env, stdout=output,
                                 stderr=subprocess.STDOUT, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            children.append(p)
            for _ in range(120):
                if p.poll() is not None:
                    raise SystemExit('Local DB failed; see .storage/postgres.log')
                if port_open(55432):
                    break
                time.sleep(.25)
            else:
                raise SystemExit('Timed out waiting for local DB.')
        app = subprocess.Popen([sys.executable, 'run.py'], env=env)
        children.append(app)
        print('Starting http://127.0.0.1:8128 — public household-battery facts; missing prices stay null.')
        for _ in range(240):
            if app.poll() is not None:
                raise SystemExit('Application failed to start; check the terminal output.')
            if port_open(8128):
                webbrowser.open('http://127.0.0.1:8128')
                break
            time.sleep(.25)
        else:
            raise SystemExit('Timed out waiting for the application. Check the terminal output.')
        app.wait()
    except KeyboardInterrupt:
        pass
    finally:
        for child in reversed(children):
            if child.poll() is None:
                if os.name == 'nt':
                    # Windows venv python.exe may be a redirector. Stop the process tree
                    # rooted at our own child, so the real interpreter cannot remain orphaned.
                    subprocess.run(['taskkill', '/PID', str(child.pid), '/T', '/F'],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                   creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                else:
                    child.terminate()
                try:
                    child.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    child.kill()
        for handle in handles:
            handle.close()


if __name__ == '__main__':
    main()
