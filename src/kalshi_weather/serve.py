"""Read-only dashboard for the live trader, on 127.0.0.1.

Usage: kalshi-weather serve [--port 8765]
"""
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path
import gzip
import json
import time
import zlib

HOST = '127.0.0.1'
HISTORY = 7200   # seconds of history sent
CHART = ('t', 'event', 'status', 'fair', 'best_bid', 'best_ask', 'last_fills')   # fields the charts use


class Log:
    """The trader's minute log for today and yesterday, re-read when it changes."""

    def __init__(self, folder):self.folder = Path(folder); self.cache = {}

    def rows(self, path):
        try:stamp = path.stat(); key = (stamp.st_size, stamp.st_mtime)
        except OSError:return []
        if self.cache.get(path, (None,))[0] != key:
            rows = []
            try:
                with gzip.open(path, 'rt') as handle:
                    for line in handle:
                        try:rows.append(json.loads(line))
                        except ValueError:pass   # a line cut short while being written
            except (EOFError, OSError, zlib.error):pass   # the last gzip member is still being written
            self.cache[path] = (key, rows)
        return self.cache[path][1]

    def since(self, t):
        days = sorted({datetime.fromtimestamp(x, timezone.utc).strftime('%Y-%m-%d') for x in (t, time.time())})
        return [r for day in days for r in self.rows(self.folder / 'log' / f'{day}.jsonl.gz') if r.get('t', 0) >= t]


def read_json(path):
    try:return json.loads(Path(path).read_text())
    except (OSError, ValueError):return None


def snapshot(folder, log):
    folder = Path(folder); now = time.time()
    return dict(server_time=now, state=read_json(folder / 'state.json'), status=read_json(folder / 'status.json'),
                stopped=(folder / 'STOP').exists(), history=[{k: r[k] for k in CHART if k in r} for r in log.since(now - HISTORY)])


def handler(folder):
    log = Log(folder); page = resources.files('kalshi_weather').joinpath('dashboard.html').read_bytes()

    class Handler(BaseHTTPRequestHandler):
        def send(self, code, body, kind):
            self.send_response(code); self.send_header('Content-Type', kind); self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store'); self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Content-Security-Policy', "default-src 'self'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; connect-src 'self'")
            self.end_headers(); self.wfile.write(body)

        def do_GET(self):
            path = self.path.split('?')[0]
            if path == '/':self.send(200, page, 'text/html; charset=utf-8')
            elif path == '/state':self.send(200, json.dumps(snapshot(folder, log), separators=(',', ':')).encode(), 'application/json')
            else:self.send(404, b'not found\n', 'text/plain')

        def log_message(self, *args):pass   # no request logging

    return Handler


def serve(folder, port=8765):
    server = ThreadingHTTPServer((HOST, port), handler(folder))
    print(f'dashboard for {folder} on http://{HOST}:{port}/ (read-only; Ctrl-C to stop)', flush=True)
    try:server.serve_forever()
    except KeyboardInterrupt:pass
    finally:server.server_close()
