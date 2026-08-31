#!/usr/bin/env python3
"""
camera-server/server.py
rpicam-jpeg で都度撮影する軽量版。
mjpg-streamer を使わないので待機中 CPU ほぼ 0%。
"""

import subprocess
import threading
import time
import os
import logging
import uuid
import tempfile
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

# ── 設定 ────────────────────────────────────────────────
HOST            = '0.0.0.0'
PORT            = 5000
CAM_WIDTH       = 640
CAM_HEIGHT      = 480
CAM_CMD         = [
    'rpicam-jpeg',
    '--width',  str(CAM_WIDTH),
    '--height', str(CAM_HEIGHT),
    '-t', '1',          # 起動後1msで撮影
    '--mode', '1640:1232', '--nopreview',
    '-o', '',           # 実行時に差し替え
]
IDLE_TIMEOUT    = 10   # 全セッション消滅後、何秒でカメラを「使用停止」とみなすか
SESSION_TIMEOUT = 20   # ハートビートが途絶えてから何秒でセッション消滅とみなすか
# ────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%H:%M:%S',
)
log = logging.getLogger(__name__)


# ── セッション管理 ────────────────────────────────────────
class SessionManager:
    def __init__(self):
        self._sessions    = {}
        self._lock        = threading.Lock()
        self._on_empty    = None
        self._on_nonempty = None

    def set_callbacks(self, on_empty, on_nonempty):
        self._on_empty    = on_empty
        self._on_nonempty = on_nonempty

    def start(self):
        t = threading.Thread(target=self._reap_loop, daemon=True)
        t.start()

    def new_session(self):
        sid = uuid.uuid4().hex
        with self._lock:
            was_empty = len(self._sessions) == 0
            self._sessions[sid] = time.monotonic()
            log.info('セッション開始 %s (合計 %d)', sid[:8], len(self._sessions))
        if was_empty and self._on_nonempty:
            self._on_nonempty()
        return sid

    def heartbeat(self, sid):
        with self._lock:
            if sid in self._sessions:
                self._sessions[sid] = time.monotonic()
                return True
        return False

    def remove(self, sid):
        with self._lock:
            if sid not in self._sessions:
                return
            del self._sessions[sid]
            remaining = len(self._sessions)
            log.info('セッション終了 %s (残 %d)', sid[:8], remaining)
        if remaining == 0 and self._on_empty:
            self._on_empty()

    def count(self):
        with self._lock:
            return len(self._sessions)

    def _reap_loop(self):
        while True:
            time.sleep(SESSION_TIMEOUT / 2)
            now = time.monotonic()
            with self._lock:
                dead = [sid for sid, t in self._sessions.items()
                        if now - t > SESSION_TIMEOUT]
            for sid in dead:
                log.info('セッションタイムアウト %s', sid[:8])
                self.remove(sid)


sessions = SessionManager()


# ── カメラ管理（rpicam-jpeg 都度撮影）────────────────────
class CameraManager:
    """
    セッションがある間だけ「有効」状態になる。
    実際の撮影は snapshot() を呼ぶたびに rpicam-jpeg を起動する。
    待機中はプロセスなし → CPU 0%。
    """
    def __init__(self):
        self._active     = False
        self._lock       = threading.Lock()
        self._snap_lock  = threading.Lock()  # 同時撮影防止
        self._stop_timer = None
        self._ready      = threading.Event()

    def activate(self):
        with self._lock:
            if self._stop_timer:
                self._stop_timer.cancel()
                self._stop_timer = None
            self._active = True
            self._ready.set()
            log.info('カメラ有効化')

    def schedule_stop(self):
        with self._lock:
            if self._stop_timer:
                self._stop_timer.cancel()
            self._stop_timer = threading.Timer(IDLE_TIMEOUT, self._do_stop)
            self._stop_timer.daemon = True
            self._stop_timer.start()
            log.info('%d 秒後にカメラを無効化します', IDLE_TIMEOUT)

    def _do_stop(self):
        with self._lock:
            if sessions.count() > 0:
                return
            self._active = False
            self._ready.clear()
            log.info('カメラ無効化')

    def is_running(self):
        return self._active

    def wait_until_ready(self, timeout=5):
        return self._ready.wait(timeout=timeout)

    def snapshot(self):
        """rpicam-jpeg で1枚撮影して JPEG バイト列を返す。失敗時は None。"""
        with self._snap_lock:
            tmp = tempfile.NamedTemporaryFile(suffix='.jpg', delete=False)
            tmp.close()
            cmd = CAM_CMD[:-1] + [tmp.name]
            try:
                r = subprocess.run(
                    cmd,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=10,
                )
                if r.returncode != 0:
                    log.warning('rpicam-jpeg 失敗 (code %d)', r.returncode)
                    return None
                with open(tmp.name, 'rb') as f:
                    return f.read()
            except subprocess.TimeoutExpired:
                log.warning('rpicam-jpeg タイムアウト')
                return None
            except Exception as e:
                log.warning('snapshot エラー: %s', e)
                return None
            finally:
                try:
                    os.unlink(tmp.name)
                except OSError:
                    pass


camera = CameraManager()

sessions.set_callbacks(
    on_empty    = camera.schedule_stop,
    on_nonempty = camera.activate,
)
sessions.start()


# ── HTTP ハンドラ ─────────────────────────────────────────
class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def do_GET(self):
        path = self.path.split('?')[0]

        if path in ('/', '/index.html'):
            self._serve_index()
        elif path == '/connect':
            self._serve_connect()
        elif path == '/ping':
            self._serve_ping()
        elif path == '/disconnect':
            self._serve_disconnect()
        elif path == '/snapshot':
            self._serve_snapshot()
        elif path == '/status':
            self._serve_status()
        elif path == '/events':
            self._serve_events()
        elif path == '/shutdown':
            self._serve_shutdown()
        else:
            self.send_error(404)

    def _serve_index(self):
        html_path = os.path.join(os.path.dirname(__file__), 'index.html')
        try:
            with open(html_path, 'rb') as f:
                data = f.read()
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', len(data))
            self.end_headers()
            self.wfile.write(data)
        except FileNotFoundError:
            self.send_error(404, 'index.html not found')

    def _json_ok(self, obj: dict):
        import json
        body = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', len(body))
        self.end_headers()
        self.wfile.write(body)

    def _serve_connect(self):
        sid = sessions.new_session()
        self._json_ok({'sid': sid})

    def _serve_ping(self):
        sid = self._get_sid()
        if sid and sessions.heartbeat(sid):
            self._json_ok({'ok': True})
        else:
            self._json_ok({'ok': False, 'reconnect': True})

    def _serve_disconnect(self):
        sid = self._get_sid()
        if sid:
            sessions.remove(sid)
        self._json_ok({'ok': True})

    def _get_sid(self):
        from urllib.parse import urlparse, parse_qs
        qs = parse_qs(urlparse(self.path).query)
        vals = qs.get('sid')
        return vals[0] if vals else None

    def _serve_snapshot(self):
        if not camera.wait_until_ready(timeout=5):
            self.send_error(503, 'Camera not ready')
            return
        data = camera.snapshot()
        if data is None:
            self.send_error(503, 'Snapshot failed')
            return
        self.send_response(200)
        self.send_header('Content-Type', 'image/jpeg')
        self.send_header('Content-Length', len(data))
        self.send_header('Cache-Control', 'no-cache')
        self.end_headers()
        self.wfile.write(data)

    def _serve_status(self):
        import json
        body = json.dumps({
            'running':  camera.is_running(),
            'ready':    camera._ready.is_set(),
            'sessions': sessions.count(),
        }).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', len(body))
        self.end_headers()
        self.wfile.write(body)

    def _serve_events(self):
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.send_header('Cache-Control', 'no-cache')
        self.send_header('Connection', 'keep-alive')
        self.end_headers()
        prev_running = None
        prev_ready   = None
        try:
            while True:
                running = camera.is_running()
                ready   = camera._ready.is_set()
                if running != prev_running or ready != prev_ready:
                    msg = (
                        f'data: {{"running":{str(running).lower()},'
                        f'"ready":{str(ready).lower()}}}\n\n'
                    )
                    self.wfile.write(msg.encode())
                    self.wfile.flush()
                    prev_running = running
                    prev_ready   = ready
                time.sleep(0.5)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _serve_shutdown(self):
        self._json_ok({'ok': True})
        log.info('シャットダウン要求を受信')
        threading.Timer(1.0, lambda: subprocess.run(['sudo', 'shutdown', '-h', 'now'])).start()


# ── エントリーポイント ────────────────────────────────────
if __name__ == '__main__':
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    log.info('Camera Server 起動 → http://0.0.0.0:%d', PORT)
    log.info('ブラウザで http://192.168.10.117:%d にアクセス', PORT)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info('停止')
