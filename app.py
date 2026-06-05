#!/usr/bin/env python3
"""AutoUpdate Web Server -- 服务模块"""
from __future__ import annotations
import os, sys, json, time, ssl, threading, http.server
import urllib.parse, urllib.request, urllib.error
from pathlib import Path
from datetime import datetime, timezone, timedelta

PROJECT_ROOT = Path(__file__).resolve().parent
WHITELIST_FILE = PROJECT_ROOT / "whitelist.json"
ACME_CHALLENGE_ROOTS: list[Path] = [PROJECT_ROOT]
GISCUS_ORIGIN = "https://giscus.app"
GITHUB_API_ORIGIN = "https://api.github.com"

_p2p_signals: dict[str, list[dict]] = {}
_p2p_signals_lock = threading.RLock()
_p2p_rooms: dict[str, dict] = {}
_p2p_last_activity = 0.0
_p2p_relay_buffers: dict[str, list[dict]] = {}
_p2p_relay_lock = threading.RLock()
_p2p_relay_usage: dict[str, list[float]] = {}
RELAY_RATE_LIMIT = 5 * 1024 * 1024
RELAY_RATE_WINDOW = 300
RELAY_MAX_BUFFER = 200

_on_access_check = lambda: None  # server.py \u5c06\u66ff\u6362\u6b64\u56de\u8c03

def log(msg):
    ts = datetime.now().strftime("%H:%M:%S")
    try:
        print(f"[{ts}] {msg}")
    except UnicodeEncodeError:
        print(f"[{ts}] {msg.encode('gbk', 'replace').decode('gbk')}")

def load_whitelist():
    if not WHITELIST_FILE.exists():
        return None
    try:
        data = json.loads(WHITELIST_FILE.read_text(encoding="utf-8"))
        return [str(p).replace("\\", "/") for p in data] if isinstance(data, list) else []
    except: return None

def is_path_allowed(p, wl):
    if not wl: return False
    for e in wl:
        if e == "/" and p == "/": return True
        if e.endswith("/") and p.startswith(e): return True
        if p == e: return True
    return False

def user_agent():
    return f"AutoUpdate/2.0 Python/{sys.version_info.major}.{sys.version_info.minor}"

def format_utc8(dt):
    return dt.strftime("%Y.%m.%d %H:%M:%S") + " [UTC+8]"

def _make_ssl_context():
    ctx = ssl.create_default_context()
    _set_opt(ctx, getattr(ssl, "OP_IGNORE_UNEXPECTED_EOF", 0))
    _set_opt(ctx, getattr(ssl, "OP_LEGACY_SERVER_CONNECT", 0))
    return ctx

def _make_fallback_ssl_context():
    ctx = ssl._create_unverified_context()
    _set_opt(ctx, getattr(ssl, "OP_IGNORE_UNEXPECTED_EOF", 0))
    _set_opt(ctx, getattr(ssl, "OP_LEGACY_SERVER_CONNECT", 0))
    return ctx

def _set_opt(ctx, opt):
    if opt:
        try: ctx.options |= opt
        except: pass

def _ssl_urlopen(req, timeout):
    try:
        return urllib.request.urlopen(req, timeout=timeout, context=_make_ssl_context())
    except Exception as e:
        err = str(e).lower()
        kw = ("eof","certificate","handshake","remote end","connection aborted","connection reset","connection refused","timed out","remote disconnected")
        if any(k in err for k in kw):
            return urllib.request.urlopen(req, timeout=timeout, context=_make_fallback_ssl_context())
        raise

# ==== AutoUpdateHandler ====
class AutoUpdateHandler(http.server.SimpleHTTPRequestHandler):
    """纯静态文件服务器，访问时触发 GitHub 更新检查。"""

    # 类级共享状态
    last_check_time = 0.0
    update_in_progress = False
    check_lock = threading.Lock()
    CHECK_INTERVAL = 300  # 5 分钟冷却

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(PROJECT_ROOT), **kwargs)

    def do_GET(self):
        self._try_check_update()          # 先触发更新（403 页面也要触发）
        # ACME HTTP-01 挑战 — 从可能的 web root 提供验证文件
        if self._try_serve_acme_challenge():
            return

        parsed = urllib.parse.urlparse(self.path)
        req_path = parsed.path

        # 自有 API 端点（放在 Giscus 代理前面，避免 /api/ 路径冲突）
        if req_path == '/api/ping':
            self._send_json({'ok': True, 'server': 'autoupdate'})
            return
        if req_path == '/api/p2p/signal':
            self._handle_p2p_poll()
            return
        if req_path == '/api/p2p/room-info':
            self._handle_p2p_room_info()
            return
        if req_path == '/api/p2p/keepalive':
            self._handle_p2p_keepalive()
            return

        # Giscus 代理 — 透明转发到 giscus.app
        if self._try_giscus_proxy(req_path):
            return

        if not self._check_whitelist():
            return
        super().do_GET()

    def do_POST(self):
        self._try_check_update()
        parsed = urllib.parse.urlparse(self.path)
        req_path = parsed.path

        if req_path == '/api/github-proxy/graphql':
            self._proxy_github_graphql()
            return
        if req_path == '/api/p2p/relay/send':
            self._handle_p2p_relay_send()
            return
        if req_path == '/api/p2p/signal':
            self._handle_p2p_signal()
            return
        if req_path == '/api/p2p/join':
            self._handle_p2p_join()
            return
        if req_path == '/api/p2p/leave':
            self._handle_p2p_leave()
            return
        if req_path == '/api/ping':
            self._send_json({'ok': True, 'server': 'autoupdate'})
            return

        self.send_error(404, "Not Found")

    def do_HEAD(self):
        self._try_check_update()          # 先触发更新（403 页面也要触发）
        # ACME HTTP-01 挑战 — 从可能的 web root 提供验证文件
        if self._try_serve_acme_challenge():
            return
        if not self._check_whitelist():
            return
        super().do_HEAD()

    # -- 白名单检查 --
    def _check_whitelist(self) -> bool:
        """检查请求路径是否在白名单内。不在则返回 403。"""
        # whitelist.json 本身永远不允许直接访问
        WHITELIST_PATH = "/whitelist.json"
        parsed = urllib.parse.urlparse(self.path)
        req_path = parsed.path

        if req_path == WHITELIST_PATH:
            self.send_error(403, "Forbidden")
            return False

        # 每次请求重新加载白名单，确保 GitHub 同步后立即生效
        whitelist = load_whitelist()

        if not is_path_allowed(req_path, whitelist):
            # 记录日志
            log(f"⛔ 访问被白名单拒绝: {req_path}")
            self.send_error(403, "Forbidden")
            return False

        return True

    # -- ACME HTTP-01 挑战 --
    def _try_serve_acme_challenge(self) -> bool:
        """
        尝试提供 ACME HTTP-01 验证文件。

        遍历 ACME_CHALLENGE_ROOTS 查找验证文件；找到则返回内容，
        返回 True 表示请求已处理（无需后续操作），False 表示非 ACME 路径。
        """
        parsed = urllib.parse.urlparse(self.path)
        req_path = parsed.path

        if not req_path.startswith("/.well-known/acme-challenge/"):
            return False

        rel_path = req_path.lstrip("/")
        for root in ACME_CHALLENGE_ROOTS:
            file_path = root / rel_path
            if file_path.exists() and file_path.is_file():
                try:
                    content = file_path.read_bytes()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/octet-stream")
                    self.send_header("Content-Length", str(len(content)))
                    self.end_headers()
                    self.wfile.write(content)
                    return True
                except Exception as e:
                    log(f"读取 ACME 挑战文件失败: {e}")
                    self.send_error(500, "Internal Server Error")
                    return True

        self.send_error(404, "Not Found")
        return True

    # -- Giscus 代理 --
    _GISCUS_PROXY_PREFIXES = ('/zh-CN/', '/en/', '/zh-TW/', '/_next/', '/api/', '/themes/')
    _GISCUS_PROXY_EXACT = frozenset({
        '/default.css', '/light.css', '/dark.css', '/cider.css',
        '/site.webmanifest',
    })

    def _try_giscus_proxy(self, path: str) -> bool:
        """透明代理 Giscus 静态资源到 giscus.app。"""
        if (path not in self._GISCUS_PROXY_EXACT
                and not path.startswith(self._GISCUS_PROXY_PREFIXES)):
            return False

        target = f"{GISCUS_ORIGIN}{path}"
        qs = urllib.parse.urlparse(self.path).query
        if qs:
            target += '?' + qs

        try:
            req = urllib.request.Request(target, headers={
                "User-Agent": user_agent(),
                "Origin": GISCUS_ORIGIN,
                "Referer": f"{GISCUS_ORIGIN}/",
            })
            # 转发浏览器原始请求中的重要头
            for h in ('Cookie', 'Authorization', 'Accept', 'Accept-Language'):
                if h in self.headers:
                    req.headers[h] = self.headers[h]

            with _ssl_urlopen(req, 20) as resp:
                content = resp.read()
                ct = resp.headers.get("Content-Type", "application/octet-stream")

                # 在 JS/HTML 中把 GitHub API 地址替换为本地代理
                if 'javascript' in ct or 'html' in ct:
                    content = content.replace(
                        b'https://api.github.com',
                        b'/api/github-proxy'
                    )

                self.send_response(resp.status)
                self.send_header("Content-Type", ct)
                self.send_header("Content-Length", str(len(content)))
                if path == '/default.css' or path.startswith('/_next/'):
                    self.send_header("Cache-Control", "public, max-age=86400")
                self.end_headers()
                self.wfile.write(content)
                return True
        except urllib.error.HTTPError as e:
            # giscus.app 返回非 2xx 是正常的（如讨论未创建时 404）
            # 转发原始状态码和响应体，而不是返回 502
            try:
                body = e.read()
                self.send_response(e.code)
                self.send_header("Content-Type", e.headers.get("Content-Type", "application/json"))
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except Exception:
                self.send_error(e.code, str(e))
            return True
        except Exception as e:
            log(f"Giscus proxy error ({path}): {e}")
            self.send_error(502, "Bad Gateway")
            return True

    # -- GitHub API 代理 --
    def _proxy_github_graphql(self):
        """透明转发 GitHub GraphQL API 请求。"""
        length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(length) if length else b''

        headers = {
            "User-Agent": user_agent(),
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        auth = self.headers.get('Authorization')
        if auth:
            headers['Authorization'] = auth

        try:
            req = urllib.request.Request(
                f"{GITHUB_API_ORIGIN}/graphql", data=body, headers=headers, method='POST'
            )
            with _ssl_urlopen(req, 30) as resp:
                content = resp.read()
                self.send_response(resp.status)
                self.send_header("Content-Type", resp.headers.get("Content-Type", "application/json"))
                self.send_header("Content-Length", str(len(content)))
                self.end_headers()
                self.wfile.write(content)
        except Exception as e:
            log(f"GitHub API proxy error: {e}")
            self.send_error(502, "Bad Gateway")

    # -- P2P 信令 --
    STALE_PEER_TIMEOUT = 15   # 秒 — 超过此时间未收到轮询/保活视为断连

    def _store_signal(self, room: str, from_p: str, to_p: str, sig_type: str, data: dict):
        with _p2p_signals_lock:
            # 存储 peer_leave 时移除该 peer 残留的 peer_join 信号，避免反复触发
            if sig_type == 'peer_leave':
                leaving_peer = data.get('peer', '')
                if leaving_peer and room in _p2p_signals:
                    _p2p_signals[room] = [
                        s for s in _p2p_signals[room]
                        if not (s['type'] == 'peer_join'
                                and s['data'].get('peer') == leaving_peer)
                    ]
            _p2p_signals.setdefault(room, []).append({
                'from': from_p, 'to': to_p, 'type': sig_type,
                'data': data, 'ts': time.time(),
            })

    def _cleanup_stale_peers(self, room: str):
        """移除超过 STALE_PEER_TIMEOUT 未轮询的 peer，广播 peer_leave。"""
        if room not in _p2p_rooms:
            return
        now = time.time()
        peers_dict = _p2p_rooms[room].get("peers", {})
        timeout = getattr(self, 'STALE_PEER_TIMEOUT', 15)
        stale = []
        for pid, info in list(peers_dict.items()):
            last = info.get("last_seen", 0)
            age = now - last
            if age > timeout:
                stale.append(pid)
                log(f"[cleanup] stale peer {pid}, age={age:.1f}s, timeout={timeout}s")
        if stale:
            log(f"[cleanup] room={room}, removing {len(stale)} stale peers")
        for pid in stale:
            peers_dict.pop(pid, None)
            self._store_signal(room, pid, '*', 'peer_leave', {'peer': pid})
        if not peers_dict:
            del _p2p_rooms[room]
            _p2p_signals.pop(room, None)

    def _handle_p2p_join(self):
        global _p2p_last_activity
        _p2p_last_activity = time.time()
        try:
            body = json.loads(self.rfile.read(int(self.headers.get('Content-Length', 0))))
            room = body.get('room', '')
            room_type = body.get('room_type', 'websrc')
            username = body.get('username', '')
            password = body.get('password', '')
            if not room:
                self.send_error(400, "Missing room"); return
            import uuid
            peer_id = uuid.uuid4().hex[:8]
            with _p2p_signals_lock:
                # 加入前先清理过期 peer
                self._cleanup_stale_peers(room)
                if room not in _p2p_rooms:
                    _p2p_rooms[room] = {
                        "type": room_type,
                        "password": password,
                        "created_at": time.time(),
                        "creator": peer_id,
                        "peers": {},
                    }
                else:
                    room_pw = _p2p_rooms[room].get("password", "")
                    if room_pw and password != room_pw:
                        self._send_json({'error': 'wrong_password'})
                        return
                    if len(_p2p_rooms[room]["peers"]) >= 4:
                        self._send_json({'error': 'room_full'})
                        return
                _p2p_rooms[room]["peers"][peer_id] = {
                    "name": username or "",
                    "last_seen": time.time(),
                    "joined_at": time.time(),
                }
            self._store_signal(room, peer_id, '*', 'peer_join',
                               {'peer': peer_id, 'name': username or ''})
            with _p2p_signals_lock:
                peers_info = []
                for pid, info in _p2p_rooms.get(room, {}).get("peers", {}).items():
                    if pid != peer_id:
                        peers_info.append({
                            "id": pid,
                            "name": info.get("name", ""),
                        })
                room_info = {
                    "type": _p2p_rooms.get(room, {}).get("type", "websrc"),
                    "created_at": _p2p_rooms.get(room, {}).get("created_at", 0),
                }
            self._send_json({
                'peer': peer_id,
                'peers': [p["id"] for p in peers_info],
                'peers_info': peers_info,
                'room_info': room_info,
            })
        except Exception as e:
            log(f"P2P join error: {e}"); self.send_error(400, "Bad request")

    def _handle_p2p_leave(self):
        global _p2p_last_activity
        _p2p_last_activity = time.time()
        try:
            body = json.loads(self.rfile.read(int(self.headers.get('Content-Length', 0))))
            room, peer = body.get('room', ''), body.get('peer', '')
            with _p2p_signals_lock:
                if room in _p2p_rooms:
                    _p2p_rooms[room]["peers"].pop(peer, None)
                    if _p2p_rooms[room]["peers"]:
                        self._store_signal(room, peer, '*', 'peer_leave', {'peer': peer})
                    else:
                        # 所有用户退出 → 彻底清除房间，包括类型/状态
                        del _p2p_rooms[room]
                        _p2p_signals.pop(room, None)
            self.send_response(200); self.send_header("Content-Length", "0"); self.end_headers()
        except Exception:
            self.send_error(400, "Bad request")

    def _handle_p2p_signal(self):
        try:
            body = json.loads(self.rfile.read(int(self.headers.get('Content-Length', 0))))
            self._store_signal(
                body.get('room', ''), body.get('from', ''),
                body.get('to', ''), body.get('type', ''), body.get('data', {}),
            )
            self._send_json({'ok': True})
        except Exception:
            self.send_error(400, "Bad request")

    def _handle_p2p_poll(self):
        params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        room, peer = params.get('room', [''])[0], params.get('peer', [''])[0]
        if not room or not peer:
            self._send_json({'signals': []}); return

        # 收集数据在锁内，发送响应在锁外
        signals = []
        relay_msgs = []
        with _p2p_signals_lock:
            # 更新此 peer 的最近轮询时间，用于断连检测
            if room in _p2p_rooms:
                if peer in _p2p_rooms[room].get("peers", {}):
                    _p2p_rooms[room]["peers"][peer]["last_seen"] = time.time()
                elif _p2p_rooms[room]["peers"]:
                    # 自己之前因超时被清理 → 自动重新注册（不广播 peer_join 避免通知骚扰）
                    _p2p_rooms[room]["peers"][peer] = {
                        "name": "",
                        "last_seen": time.time(),
                        "joined_at": time.time(),
                    }
            # 清理过期的 peer
            self._cleanup_stale_peers(room)

            if room in _p2p_signals:
                keep = []
                for s in _p2p_signals[room]:
                    is_for_me = (s['to'] == peer) and s['from'] != peer
                    is_broadcast = (s['to'] == '*') and s['from'] != peer
                    if is_for_me:
                        signals.append(s)
                    elif is_broadcast:
                        signals.append(s)
                        # 广播信号保留 2s 即可，所有活跃 peer 每 1.2s 轮询，
                        # 避免信号滞留反复触发回调
                        if time.time() - s['ts'] < 2:
                            keep.append(s)
                    else:
                        keep.append(s)
                _p2p_signals[room] = [s for s in keep if time.time() - s['ts'] < 10]

            # 收集该 peer 的中转消息
            if room in _p2p_relay_buffers:
                keep_relay = []
                for msg in _p2p_relay_buffers[room]:
                    if msg['to'] == peer and msg['from'] != peer:
                        relay_msgs.append(msg)
                    else:
                        keep_relay.append(msg)
                _p2p_relay_buffers[room] = keep_relay

        # 计算该 peer 的中转配额剩余
        relay_remaining = self._get_relay_remaining(peer, room)
        self._send_json({'signals': signals, 'relay': relay_msgs, 'relay_remaining': relay_remaining})

    def _handle_p2p_room_info(self):
        global _p2p_last_activity
        _p2p_last_activity = time.time()
        """查询房间信息（类型、用户数、用户列表），先清理过期 peer。"""
        params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        room = params.get('room', [''])[0]
        if not room:
            self._send_json({'exists': False})
            return
        with _p2p_signals_lock:
            if room in _p2p_rooms:
                self._cleanup_stale_peers(room)  # 清理后再返回
                peers_dict = _p2p_rooms[room].get("peers", {}) if room in _p2p_rooms else {}
                info = {
                    'exists': room in _p2p_rooms,
                    'type': _p2p_rooms[room].get('type', 'websrc') if room in _p2p_rooms else 'websrc',
                    'has_password': bool(_p2p_rooms[room].get('password', '')) if room in _p2p_rooms else False,
                    'user_count': len(peers_dict),
                    'users': [
                        {'id': pid, 'name': info.get('name', '')}
                        for pid, info in peers_dict.items()
                    ],
                }
            else:
                info = {'exists': False}
        self._send_json(info)

    def _handle_p2p_keepalive(self):
        global _p2p_last_activity
        _p2p_last_activity = time.time()
        """轻量保活：只更新 last_seen，不处理信号/中转消息。"""
        params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        room = params.get('room', [''])[0]
        peer = params.get('peer', [''])[0]
        if room and peer:
            with _p2p_signals_lock:
                if room in _p2p_rooms and peer in _p2p_rooms[room].get("peers", {}):
                    _p2p_rooms[room]["peers"][peer]["last_seen"] = time.time()
        self._send_json({'ok': True})

    # ── P2P 中转模式 ──────────────────────────────────────

    def _check_relay_rate(self, peer_id: str, size: int) -> bool:
        """滑动窗口速率检查：每 peer 每 RELAY_RATE_WINDOW 秒最多 RELAY_RATE_LIMIT 字节。"""
        now = time.time()
        with _p2p_relay_lock:
            records = _p2p_relay_usage.setdefault(peer_id, [])
            # 移除窗口外的记录
            cutoff = now - RELAY_RATE_WINDOW
            _p2p_relay_usage[peer_id] = [(t, s) for (t, s) in records if t > cutoff]
            total = sum(s for (_, s) in _p2p_relay_usage[peer_id])
            if total + size > RELAY_RATE_LIMIT:
                return False
            _p2p_relay_usage[peer_id].append((now, size))
            return True

    def _handle_p2p_relay_send(self):
        """接受中转数据，存入缓冲区供目标 peer 轮询获取。"""
        try:
            body = json.loads(self.rfile.read(int(self.headers.get('Content-Length', 0))))
            room = body.get('room', '')
            from_p = body.get('from', '')
            to_p = body.get('to', '')
            msg_type = body.get('type', 'chat')
            data = body.get('data', '')
            msg_id = body.get('id', '')

            if not room or not from_p or not to_p or not data:
                self._send_json({'ok': False, 'error': 'missing_fields'})
                return

            # WebRTC 直连房间（websrc）无速率与文件大小限制
            with _p2p_signals_lock:
                is_webrtc = _p2p_rooms.get(room, {}).get("type") == "websrc"

            if not is_webrtc:
                # 非 WebRTC 房间：滑动窗口速率检查
                data_size = len(data)
                if not self._check_relay_rate(from_p, data_size):
                    self._send_json({'ok': False, 'error': 'rate_limit',
                                     'retry_after': RELAY_RATE_WINDOW})
                    return

            with _p2p_relay_lock:
                _p2p_relay_buffers.setdefault(room, []).append({
                    'from': from_p, 'to': to_p, 'type': msg_type,
                    'data': data, 'id': msg_id, 'ts': time.time(),
                })
                # 非 WebRTC 房间限制缓冲区大小，防止内存泄漏
                if not is_webrtc and len(_p2p_relay_buffers[room]) > RELAY_MAX_BUFFER:
                    _p2p_relay_buffers[room] = _p2p_relay_buffers[room][-RELAY_MAX_BUFFER:]

            self._send_json({'ok': True})
        except Exception as e:
            log(f"Relay send error: {e}")
            self.send_error(400, "Bad request")

    def _get_relay_remaining(self, peer_id: str, room: str = "") -> int:
        """返回该 peer 在当前窗口内的剩余可用字节数。WebRTC 房间无限制。"""
        with _p2p_signals_lock:
            if room and _p2p_rooms.get(room, {}).get("type") == "websrc":
                return 2**63 - 1  # 无限制
        now = time.time()
        with _p2p_relay_lock:
            records = _p2p_relay_usage.get(peer_id, [])
            cutoff = now - RELAY_RATE_WINDOW
            recent = sum(s for (t, s) in records if t > cutoff)
            return max(0, RELAY_RATE_LIMIT - recent)

    def _send_json(self, data: dict):
        """发送 JSON 响应。"""
        resp = json.dumps(data)
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(resp)))
        self.end_headers()
        self.wfile.write(resp.encode())

    # -- 更新触发 --
    @classmethod
    def _try_check_update(cls):
        """冷却期内不检查，否则异步触发一次更新。"""
        now = time.time()
        with cls.check_lock:
            if now - cls.last_check_time < cls.CHECK_INTERVAL:
                return
            if cls.update_in_progress:
                return
            cls.last_check_time = now
            cls.update_in_progress = True

        def _do():
            try:
                _on_access_check(cls)
            except Exception as e:
                log(f"更新异常: {e}")
            finally:
                cls.update_in_progress = False

        threading.Thread(target=_do, daemon=True).start()

    def log_message(self, fmt, *args):
        if len(args) == 3:
            # 正常请求: ("GET /path HTTP/1.1", "200", "size")
            log(f"→ {args[0]}  {args[1]} ({args[2]})")
        elif len(args) == 2:
            # 错误响应: (code, message) — 被 send_error 调用
            log(f"→ ❌ {args[0]} {args[1]}")
        else:
            log(f"→ HTTP {' '.join(str(a) for a in args)}")


# ── 启动入口 ─────────────────────────────────────────────

def main():
    import argparse

    parser = argparse.ArgumentParser(
        description="AutoUpdate Web Server - 自更新静态网站服务器"
    )
    parser.add_argument(
        "--host", default="0.0.0.0",
        help="监听地址 (默认: 0.0.0.0)"
    )
    parser.add_argument(
        "--port", type=int, default=9876,
        help="监听端口 (默认: 9876)"
    )
    parser.add_argument(
        "--interval", type=int, default=300,
        help="更新检查冷却秒数 (默认: 300)"
    )
    args = parser.parse_args()

    AutoUpdateHandler.CHECK_INTERVAL = args.interval

    server = http.server.HTTPServer(
        (args.host, args.port), AutoUpdateHandler
    )
    addr = args.host if args.host != "0.0.0.0" else "localhost"

    log("")
    log(f"{'='*50}")
    log(f"  AutoUpdate 服务器已启动")
    log(f"  地址: http://{addr}:{args.port}")
    log(f"  目录: {PROJECT_ROOT}")
    log(f"  冷却: {args.interval}s（访问时触发）")
    log(f"{'='*50}")
    log("")

    # 启动时确保 main.js 有时间戳（尚无则首次注入）
    # 在 server.py 下运行时由 server.py 注入时间戳

    try:
        # 后台线程：每 60s 清理残留的空房间与过期 peer
        def _cleanup_loop():
            while True:
                time.sleep(60)
                try:
                    now = time.time()
                    with _p2p_signals_lock:
                        # 清理超过 15 秒未轮询的 stale peer
                        stale_rooms = []
                        for r, rdata in list(_p2p_rooms.items()):
                            peers = rdata.get("peers", {})
                            dead = [pid for pid, info in list(peers.items())
                                    if now - info.get("last_seen", 0) > 15]
                            for pid in dead:
                                del peers[pid]
                            if not peers:
                                stale_rooms.append(r)
                        for r in stale_rooms:
                            del _p2p_rooms[r]
                            _p2p_signals.pop(r, None)
                            _p2p_relay_buffers.pop(r, None)
                except Exception:
                    pass

        threading.Thread(target=_cleanup_loop, daemon=True).start()
        server.serve_forever()
    except KeyboardInterrupt:
        log("\n服务已停止")
        server.server_close()


if __name__ == "__main__":
    main()
