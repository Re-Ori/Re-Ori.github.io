#!/usr/bin/env python3
"""AutoUpdate Web Server -- 服务模块"""
from __future__ import annotations
import os, sys, json, time, ssl, threading, http.server
import urllib.parse, urllib.request, urllib.error
from pathlib import Path
from datetime import datetime, timezone, timedelta

PROJECT_ROOT = Path(__file__).resolve().parent
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
DATA_DIR = PROJECT_ROOT / ".data"
SHORT_LINKS_DIR = DATA_DIR / "short_link"
SHORT_LINKS_FILE = SHORT_LINKS_DIR / "short_links.json"
ANON_SL_TTL = 259200  # 72h

BBS_DIR = DATA_DIR / "bbs"
BBS_USERS_FILE = BBS_DIR / "users.json"
BBS_TOPICS_FILE = BBS_DIR / "topics.json"
BBS_TOPICS_DIR = BBS_DIR / "topics"
BBS_TOKENS_FILE = BBS_DIR / "tokens.json"

# ── 统计追踪 ──
STATS_DIR = DATA_DIR / "stats"
STATS_DAILY_FILE = STATS_DIR / "daily.json"
STATS_AGGREGATE_FILE = STATS_DIR / "aggregate.json"
_stats_lock = threading.RLock()
_STATS_DIRTY = False

def _load_stats():
    """从磁盘加载聚合统计数据，文件不存在时返回初始值"""
    try:
        if STATS_AGGREGATE_FILE.exists():
            data = json.loads(STATS_AGGREGATE_FILE.read_text(encoding="utf-8"))
            # 确保所有字段存在
            data.setdefault("started_at", 0)
            data.setdefault("requests", {"total": 0, "p2p": 0, "short_link": 0, "short_link_redirect": 0,
                "bbs": 0, "github_proxy": 0, "giscus_proxy": 0, "stats": 0, "other_api": 0, "static": 0})
            data.setdefault("p2p", {"rooms_created": 0, "signals": 0, "relay_bytes": 0, "peak_peers": 0})
            data.setdefault("short_link", {"created": 0, "redirects": 0})
            data.setdefault("bandwidth", {"sent": 0, "received": 0})
            return data
    except: pass
    return {
        "started_at": 0,
        "requests": {
            "total": 0, "p2p": 0, "short_link": 0, "short_link_redirect": 0,
            "bbs": 0, "github_proxy": 0, "giscus_proxy": 0, "stats": 0, "other_api": 0, "static": 0,
        },
        "p2p": {"rooms_created": 0, "signals": 0, "relay_bytes": 0, "peak_peers": 0},
        "short_link": {"created": 0, "redirects": 0},
        "bandwidth": {"sent": 0, "received": 0},
    }

_stats = _load_stats()

def _save_stats():
    """将聚合统计数据写入磁盘"""
    global _STATS_DIRTY
    if not _STATS_DIRTY:
        return
    try:
        STATS_DIR.mkdir(parents=True, exist_ok=True)
        with _stats_lock:
            data = dict(_stats)
        STATS_AGGREGATE_FILE.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        _STATS_DIRTY = False
    except: pass

def _incr(category: str, sub: str = "", amount=1):
    global _STATS_DIRTY
    with _stats_lock:
        if sub:
            _stats.setdefault(category, {})
            _stats[category][sub] = _stats[category].get(sub, 0) + amount
        else:
            _stats[category] = _stats.get(category, 0) + amount
        _STATS_DIRTY = True

def _track_req(path: str, method: str = "GET"):
    global _STATS_DIRTY
    cat = "static"
    with _stats_lock:
        if not _stats["started_at"]:
            _stats["started_at"] = time.time()
        _stats["requests"]["total"] += 1
        _STATS_DIRTY = True
        p = path.split("?")[0].split("#")[0]
        if p.startswith("/api/p2p/"):
            _stats["requests"]["p2p"] += 1; cat = "p2p"
        elif p.startswith("/api/short-link") or p == "/api/short-links":
            _stats["requests"]["short_link"] += 1; cat = "short_link"
        elif p.startswith("/s/"):
            _stats["requests"]["short_link_redirect"] += 1; cat = "short_link_redirect"
        elif p.startswith("/api/bbs/"):
            _stats["requests"]["bbs"] += 1; cat = "bbs"
        elif p.startswith("/api/github-proxy/"):
            _stats["requests"]["github_proxy"] += 1; cat = "github_proxy"
        elif p.startswith(("/zh-CN/", "/en/", "/_next/", "/api/oauth/")) or p in ("/default.css", "/light.css", "/dark.css", "/cider.css"):
            _stats["requests"]["giscus_proxy"] += 1; cat = "giscus_proxy"
        elif p == "/api/stats":
            _stats["requests"]["stats"] += 1; cat = "stats"
        elif p.startswith("/api/"):
            _stats["requests"]["other_api"] += 1; cat = "other_api"
        else:
            _stats["requests"]["static"] += 1
    _track_daily(cat)

_bandwidth_lock = threading.RLock()

def _track_bandwidth(sent=0, received=0):
    global _STATS_DIRTY
    with _bandwidth_lock:
        _stats["bandwidth"]["sent"] += sent
        _stats["bandwidth"]["received"] += received
        _STATS_DIRTY = True
    # 也写入每日统计
    try:
        today = _daily_date()
        daily = _load_daily()
        day = daily.setdefault(today, {})
        day["bw_sent"] = day.get("bw_sent", 0) + sent
        day["bw_recv"] = day.get("bw_recv", 0) + received
        _DAILY_DIRTY = True
    except: pass

# ── 每日统计持久化 ──

_DAILY_CACHE = None
_DAILY_DIRTY = False

def _daily_date():
    return datetime.now().strftime("%Y-%m-%d")

def _load_daily():
    global _DAILY_CACHE
    if _DAILY_CACHE is not None:
        return _DAILY_CACHE
    try:
        if STATS_DAILY_FILE.exists():
            _DAILY_CACHE = json.loads(STATS_DAILY_FILE.read_text(encoding="utf-8"))
            return _DAILY_CACHE
    except: pass
    _DAILY_CACHE = {}
    return _DAILY_CACHE

def _save_daily():
    global _DAILY_DIRTY, _DAILY_CACHE
    if not _DAILY_DIRTY or _DAILY_CACHE is None:
        return
    try:
        STATS_DIR.mkdir(parents=True, exist_ok=True)
        STATS_DAILY_FILE.write_text(json.dumps(_DAILY_CACHE, ensure_ascii=False), encoding="utf-8")
        _DAILY_DIRTY = False
    except: pass

def _track_daily(category: str):
    today = _daily_date()
    daily = _load_daily()
    day = daily.setdefault(today, {"total": 0, "p2p": 0, "short_link": 0, "short_link_redirect": 0,
        "bbs": 0, "github_proxy": 0, "giscus_proxy": 0, "other_api": 0, "static": 0})
    day["total"] = day.get("total", 0) + 1
    day[category] = day.get(category, 0) + 1
    cutoff = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
    for k in list(daily.keys()):
        if k < cutoff:
            del daily[k]
    _DAILY_DIRTY = True
    if day["total"] % 10 == 0:
        _save_daily()

def _get_daily_7d():
    daily = _load_daily()
    today = datetime.now()
    result = []
    for i in range(6, -1, -1):
        d = (today - timedelta(days=i)).strftime("%Y-%m-%d")
        day = daily.get(d, {})
        result.append({
            "date": d,
            "total": day.get("total", 0),
            "p2p": day.get("p2p", 0),
            "short_link": day.get("short_link", 0),
            "short_link_redirect": day.get("short_link_redirect", 0),
            "bbs": day.get("bbs", 0),
            "github_proxy": day.get("github_proxy", 0),
            "giscus_proxy": day.get("giscus_proxy", 0),
            "stats": day.get("stats", 0),
            "other_api": day.get("other_api", 0),
            "static": day.get("static", 0),
            "bw_sent": day.get("bw_sent", 0),
            "bw_recv": day.get("bw_recv", 0),
        })
    return result

def _stats_flusher():
    while True:
        time.sleep(60)
        try: _save_daily()
        except: pass
        try: _save_stats()
        except: pass

threading.Thread(target=_stats_flusher, daemon=True).start()

BBS_TOKENS: dict[str, dict] = {}
BBS_TOKEN_TTL = 86400  # 24h

def _bbs_load_tokens():
    """从磁盘加载 BBS Token"""
    global BBS_TOKENS
    try:
        if BBS_TOKENS_FILE.exists():
            data = json.loads(BBS_TOKENS_FILE.read_text(encoding='utf-8'))
            now = time.time()
            BBS_TOKENS = {k: v for k, v in data.items()
                          if now - v.get('at', 0) < BBS_TOKEN_TTL}
            if len(BBS_TOKENS) != len(data):
                _bbs_save_tokens()
    except Exception:
        BBS_TOKENS = {}

def _bbs_save_tokens():
    """将 BBS Token 持久化到磁盘"""
    BBS_TOKENS_FILE.parent.mkdir(parents=True, exist_ok=True)
    BBS_TOKENS_FILE.write_text(json.dumps(BBS_TOKENS, ensure_ascii=False), encoding='utf-8')

def _bbs_token():
    import hashlib, os
    return hashlib.sha256(os.urandom(32)).hexdigest()[:32]

def _bbs_id(ts=None):
    """36进制时间戳+6位随机段，可传入时间戳模拟历史时间"""
    n = int((ts if ts is not None else time.time()) * 1000)
    chars = '0123456789abcdefghijklmnopqrstuvwxyz'
    result = ''
    while n:
        result = chars[n % 36] + result
        n //= 36
    import random, string as _s
    return result + ''.join(random.choice(_s.ascii_lowercase + _s.digits) for _ in range(6))

def _bbs_topic_filename(tid, title):
    """生成可读的帖子文件名: {id}_{标题前16位}.json"""
    safe = ''.join(c if c.isalnum() or c in ' -_.,;!?@#' else '_' for c in str(title))
    safe = safe.strip('._ ')[:16] or 'untitled'
    return f'{tid}_{safe}.json'

def _bbs_find_topic_file(tid):
    """遍历 topics 目录查找指定 ID 的帖子文件"""
    if not BBS_TOPICS_DIR.exists():
        return None
    for f in BBS_TOPICS_DIR.iterdir():
        if f.name.startswith(tid + '_') and f.suffix == '.json':
            return f
    return None

def _bbs_all_topics():
    """读取并处理所有帖子（含作者解析、生成预览），每次从磁盘读取"""
    if not BBS_TOPICS_DIR.exists():
        return []
    topics = []
    for f in BBS_TOPICS_DIR.iterdir():
        if f.suffix == '.json':
            try:
                topics.append(json.loads(f.read_text(encoding='utf-8')))
            except Exception:
                pass
    topics.sort(key=lambda t: t.get('created_at', 0), reverse=True)
    for t in topics:
        aid = t.get('author_id', '')
        info = _bbs_resolve_author(aid)
        t['author_name'] = info['username'] if info else '账号不存在'
        t['author_role'] = info['role'] if info else ''
        t['author_tags'] = info['tags'] if info else []
        t['reply_count'] = len(t.get('replies', []))
        raw = t.get('content', '')
        plain = raw
        plain = __import__('re').sub(r'```[\s\S]*?```|`([^`]+)`', r'\1', plain)
        plain = __import__('re').sub(r'\[([^\]]+)\]\([^)]+\)', r'\1', plain)
        plain = __import__('re').sub(r'\*\*([^*]+)\*\*', r'\1', plain)
        plain = __import__('re').sub(r'^#{1,6}\s+', '', plain, flags=__import__('re').MULTILINE)
        plain = plain.replace('\n', ' ').strip()
        t['content_preview'] = plain[:120] + ('...' if len(plain) > 120 else '')
        t.pop('content', None)
        t.pop('replies', None)
    return topics

def _bbs_migrate_old_format():
    """迁移旧 {id}.json → {id}_{title}.json，清理 topics.json"""
    if BBS_TOPICS_DIR.exists():
        for f in list(BBS_TOPICS_DIR.iterdir()):
            if f.suffix == '.json' and '_' not in f.name:
                try:
                    data = json.loads(f.read_text(encoding='utf-8'))
                    new = _bbs_topic_filename(data.get('id', f.stem), data.get('title', ''))
                    f.rename(BBS_TOPICS_DIR / new)
                except Exception:
                    pass
    if BBS_TOPICS_FILE.exists():
        try:
            BBS_TOPICS_FILE.unlink()
        except Exception:
            pass

def _bbs_check(headers):
    t = headers.get('X-Auth-Token', '')
    e = BBS_TOKENS.get(t)
    if e and time.time() - e['at'] < BBS_TOKEN_TTL:
        return e  # {user_id, username, role, at}
    if t in BBS_TOKENS:
        BBS_TOKENS.pop(t, None)
        _bbs_save_tokens()
    return None

def _sl_load():
    try:
        if SHORT_LINKS_FILE.exists():
            return json.loads(SHORT_LINKS_FILE.read_text(encoding="utf-8"))
    except: pass
    return {}

def _sl_save(d):
    SHORT_LINKS_DIR.mkdir(parents=True, exist_ok=True)
    SHORT_LINKS_FILE.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")

# server.py \u542f\u52a8\u65f6\u4f1a\u6ce8\u5165\u6b64\u53d8\u91cf
CONFIG = {}

_on_access_check = lambda: None  # server.py \u5c06\u66ff\u6362\u6b64\u56de\u8c03

APP_LOG_FILE = PROJECT_ROOT / ".server.log"

# ── 字节计数（自动统计所有流量） ──
class _CountingWriter:
    """包裹 wfile，自动累加写入字节数，每请求结束时上报"""
    def __init__(self, inner):
        self._inner = inner
        self._start = 0
    def write(self, data):
        n = self._inner.write(data)
        self._start += n
        return n
    def flush(self):
        self._inner.flush()
    def get_and_reset(self):
        n = self._start
        self._start = 0
        return n
    def __getattr__(self, name):
        return getattr(self._inner, name)

class _CountingReader:
    """包裹 rfile，自动累加读取字节数（请求行 + 请求头 + 请求体）"""
    def __init__(self, inner):
        self._inner = inner
        self._start = 0
    def read(self, n=-1):
        data = self._inner.read(n)
        self._start += len(data)
        return data
    def readline(self, n=-1):
        data = self._inner.readline(n)
        self._start += len(data)
        return data
    def get_and_reset(self):
        n = self._start
        self._start = 0
        return n
    def __getattr__(self, name):
        return getattr(self._inner, name)

def log(msg):
    ts = datetime.now().strftime("%Y.%m.%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    try:
        print(line)
    except UnicodeEncodeError:
        print(line.encode('gbk', 'replace').decode('gbk'))
    try:
        with open(APP_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except: pass

def _load_config_direct():
    """每次直接读 reori-config.json（不依赖启动时注入的 CONFIG）"""
    try:
        cfg = PROJECT_ROOT / "reori-config.json"
        if cfg.exists():
            return json.loads(cfg.read_text(encoding="utf-8"))
    except: pass
    return {}

def load_whitelist():
    """从统一配置加载白名单。空数组 = 允许所有。"""
    cfg = _load_config_direct()
    wl = cfg.get("access", {}).get("whitelist")
    if wl is not None:
        if isinstance(wl, list) and wl:
            return [str(p).replace("\\", "/") for p in wl]
        # 空数组 = 有配置但无限制 → 返回特殊标记让 is_path_allowed 放行
        if isinstance(wl, list) and not wl:
            return ["__ALLOW_ALL__"]
    return None

def load_blacklist():
    """从统一配置加载黑名单。"""
    cfg = _load_config_direct()
    bl = cfg.get("access", {}).get("blacklist")
    if bl is not None:
        return [str(p).replace("\\", "/") for p in bl] if isinstance(bl, list) else []
    return []

def is_path_allowed(p, wl):
    if not wl: return False
    if "__ALLOW_ALL__" in wl: return True
    for e in wl:
        if e == "/" and p == "/": return True
        if e != "/" and e.endswith("/") and p.startswith(e): return True
        if p == e: return True
    return False

def _bbs_resolve_author(author_id):
    """根据 author_id 查找用户信息，找不到返回 None（表示账号不存在）。"""
    try:
        users = json.loads(BBS_USERS_FILE.read_text(encoding='utf-8'))
        for u in users:
            if u.get('id') == author_id:
                return {
                    'username': u.get('username', ''),
                    'role': u.get('role', 'user'),
                    'tags': u.get('tags', []),
                }
    except:
        pass
    return None

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
            try:
                return urllib.request.urlopen(req, timeout=timeout, context=_make_fallback_ssl_context())
            except Exception as e2:
                s2 = str(e2).lower()
                if any(k in s2 for k in ("eof","certificate","handshake")):
                    return urllib.request.urlopen(req, timeout=timeout)
                raise e2
        raise

# ==== AutoUpdateHandler ====
class AutoUpdateHandler(http.server.SimpleHTTPRequestHandler):
    """纯静态文件服务器，访问时触发 GitHub 更新检查。"""

    # 类级共享状态
    last_check_time = 0.0
    update_in_progress = False
    check_lock = threading.Lock()
    CHECK_INTERVAL = 60  # 1 分钟冷却

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(PROJECT_ROOT), **kwargs)

    def setup(self):
        super().setup()
        # 包裹 wfile 以统计发送流量，包裹 rfile 以统计接收流量（含请求行+头+体）
        try:
            if not isinstance(self.wfile, _CountingWriter):
                self.wfile = _CountingWriter(self.wfile)
        except: pass
        try:
            if not isinstance(self.rfile, _CountingReader):
                self.rfile = _CountingReader(self.rfile)
        except: pass

    def do_GET(self):
        _track_req(self.path)
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
        if req_path == '/api/stats':
            self._handle_stats()
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
        if req_path == '/api/short-links':
            self._handle_sl_list()
            return

        # BBS API — GET 路由
        if req_path == '/api/bbs/topics':
            self._handle_bbs_topics()
            return
        if req_path.startswith('/api/bbs/topics/'):
            parts = req_path[16:].split('/')
            if len(parts) == 1 and parts[0]:
                self._handle_bbs_topic_detail(parts[0])
                return
        if req_path.startswith('/api/bbs/user/'):
            uid = req_path[14:]
            if uid:
                self._handle_bbs_user_profile(uid)
                return
        if req_path == '/api/bbs/export':
            self._handle_bbs_export()
            return

        # 短链跳转 /s/<code>
        if req_path.startswith('/s/') and len(req_path) > 3:
            code = req_path[3:]
            links = _sl_load()
            if code in links:
                _incr("short_link", "redirects")
                target = links[code]
                if isinstance(target, dict):
                    # 检查匿名短链是否过期
                    expires_at = target.get('expires_at', 0)
                    if expires_at and time.time() > expires_at:
                        self.send_response(410)
                        self.send_header('Content-Type', 'text/plain; charset=utf-8')
                        self.end_headers()
                        self.wfile.write('短链已过期\n'.encode('utf-8'))
                        return
                    target = target.get('url', '')
                if target:
                    safe_target_html = target.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;').replace('"', '&quot;')
                    safe_target_js = target.replace('\\', '\\\\').replace("'", "\\'")
                    END = '</s' + 'cript>'
                    redirect_html = (
                        '<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8">'
                        + '<meta name="viewport" content="width=device-width,initial-scale=1.0">'
                        + '<title>正在跳转</title><style>'
                        + '*{margin:0;padding:0;box-sizing:border-box}'
                        + 'body{background:#fff;display:flex;justify-content:center;align-items:center;min-height:100vh;font-family:-apple-system,BlinkMacSystemFont,sans-serif}'
                        + '[data-theme="dark"] body{background:#13141f}'
                        + '.wrap{text-align:center;padding:40px}'
                        + '.logo{width:48px;height:48px;border:3px solid rgba(57,159,255,0.2);border-top-color:#399FFF;border-radius:50%;animation:spin .8s linear infinite;margin:0 auto 20px}'
                        + '@keyframes spin{to{transform:rotate(360deg)}}'
                        + 'h2{font-size:18px;color:#1a1a1a;margin-bottom:8px}'
                        + '[data-theme="dark"] h2{color:#e0e0e0}'
                        + 'p{font-size:13px;color:#8e8e93;word-break:break-all}'
                        + '.bar{width:240px;height:4px;background:rgba(0,0,0,0.06);border-radius:2px;margin:24px auto 0;overflow:hidden}'
                        + '[data-theme="dark"] .bar{background:rgba(255,255,255,0.06)}'
                        + '.fill{height:100%;width:0%;background:var(--theme-color,#399FFF);border-radius:2px;transition:width .3s}'
                        + '</style></head><body onload="setTimeout(function(){window.location.replace(\'' + safe_target_js + '\')},600)"><div class="wrap">'
                        + '<div class="logo"></div><h2>正在跳转</h2>'
                        + '<p>' + safe_target_html + '</p>'
                        + '<div class="bar"><div class="fill" id="f"></div></div></div>'
                        + '<script>'
                        + 'var t=0,i=setInterval(function(){'
                        + 'var f=document.getElementById("f");if(!f)return;'
                        + 't+=Math.random()*15+5;if(t>90){t=90;clearInterval(i)}'
                        + 'f.style.width=t+"%"},200);'
                        + END + '</body></html>'
                    )
                    resp = redirect_html.encode('utf-8')
                    self.send_response(200)
                    self.send_header('Content-Type', 'text/html; charset=utf-8')
                    self.send_header('Content-Length', str(len(resp)))
                    self.end_headers()
                    self.wfile.write(resp)
                    return
            self.send_response(404)
            self.send_header('Content-Type', 'text/plain; charset=utf-8')
            self.end_headers()
            self.wfile.write('短链不存在\n'.encode('utf-8'))
            return

        # GitHub API 代理（处理 giscus URL 重写后的请求）
        if req_path.startswith('/api/github-proxy/'):
            self._proxy_github_api(req_path)
            return

        # OAuth 路径 — 不经过 giscus 代理，直接重定向到 giscus.app
        # 让客户端浏览器直接处理 GitHub OAuth 跳转，避免代理跟随重定向导致 origin 混乱
        if req_path.startswith('/api/oauth/'):
            target = f"{GISCUS_ORIGIN}{req_path}"
            qs = urllib.parse.urlparse(self.path).query
            if qs:
                target += '?' + qs
            self.send_response(302)
            self.send_header('Location', target)
            self.end_headers()
            return

        # /session — 重定向到首页（fallback，正常 OAuth 流程不经过这里）
        if req_path == '/session':
            self.send_response(302)
            self.send_header('Location', '/index.html')
            self.end_headers()
            return

        # /manifest.json — 浏览器自动请求，返回最小响应避免 404
        if req_path == '/manifest.json':
            self._send_json({
                "name": "Origin Base",
                "short_name": "Origin",
                "start_url": "/index.html",
                "display": "browser"
            })
            return

        # Giscus 代理 — 透明转发到 giscus.app
        if self._try_giscus_proxy(req_path):
            return

        if not self._check_whitelist():
            return
        super().do_GET()

    def do_POST(self):
        _track_req(self.path, "POST")
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
        if req_path == '/api/short-link/auth':
            self._handle_sl_auth()
            return
        if req_path == '/api/short-link':
            user = _bbs_check(self.headers)
            self._handle_sl_create(user)
            return
        if req_path == '/api/short-link/delete':
            user = _bbs_check(self.headers)
            self._handle_sl_delete(user)
            return

        # BBS API
        if req_path == '/api/bbs/auth':
            self._handle_bbs_auth()
            return
        if req_path == '/api/bbs/topics':
            if self.command == 'GET':
                self._handle_bbs_topics()
            else:
                self._handle_bbs_topic_create()
            return
        if req_path.startswith('/api/bbs/topics/'):
            parts = req_path[16:].split('/')
            if len(parts) == 1 and parts[0]:
                self._handle_bbs_topic_detail(parts[0])
                return
            if len(parts) == 2 and parts[1] == 'reply':
                self._handle_bbs_topic_reply(parts[0])
                return
            if len(parts) == 2 and parts[1] == 'reply-delete':
                self._handle_bbs_topic_reply_delete(parts[0])
                return
            if len(parts) == 2 and parts[1] == 'delete':
                self._handle_bbs_topic_delete(parts[0])
                return
        if req_path == '/api/bbs/import':
            self._handle_bbs_import()
            return

        # OAuth 路径（如 /api/oauth/token）— 服务端代理到 giscus.app（避免 CORS 问题）
        if req_path.startswith('/api/oauth/'):
            self._proxy_giscus_api(req_path)
            return

        self.send_error(404, "Not Found")

    def do_HEAD(self):
        self._try_check_update()          # 先触发更新（403 页面也要触发）
        # ACME HTTP-01 挑战 — 从可能的 web root 提供验证文件
        if self._try_serve_acme_challenge():
            return
        # /session — 跟 GET 一样重定向
        if urllib.parse.urlparse(self.path).path == '/session':
            self.send_response(302)
            self.send_header('Location', '/index.html')
            self.end_headers()
            return
        if not self._check_whitelist():
            return
        super().do_HEAD()

    # -- 白名单检查 --
    def _check_whitelist(self) -> bool:
        """检查路径是否在黑名单内，再检查是否在白名单内。不在则返回 403。"""
        parsed = urllib.parse.urlparse(self.path)
        req_path = parsed.path

        # 黑名单检查（优先级高于白名单）
        blacklist = load_blacklist()
        if blacklist and is_path_allowed(req_path, blacklist):
            log(f"⛔ 访问被黑名单拒绝: {req_path}")
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

                # 仅将 JS/HTML 中的 GitHub GraphQL API 地址替换为本地代理
                if 'javascript' in ct or 'html' in ct:
                    content = content.replace(
                        b'https://api.github.com/graphql',
                        b'/api/github-proxy/graphql'
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
                try:
                    self.send_error(e.code, str(e))
                except Exception:
                    pass
            return True
        except Exception as e:
            log(f"Giscus proxy error ({path}): {e}")
            try:
                self.send_error(502, "Bad Gateway")
            except Exception:
                pass
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

    def _proxy_github_api(self, path: str):
        """通用 GitHub API GET 代理 — 将 /api/github-proxy/... 转发到 api.github.com/..."""
        # 去掉 /api/github-proxy/ 前缀，构造目标 URL
        api_path = path[len('/api/github-proxy'):]  # 保留开头的 /
        qs = urllib.parse.urlparse(self.path).query
        target = f"{GITHUB_API_ORIGIN}{api_path}"
        if qs:
            target += '?' + qs

        headers = {
            "User-Agent": user_agent(),
            "Accept": "application/json",
        }
        auth = self.headers.get('Authorization')
        if auth:
            headers['Authorization'] = auth

        try:
            req = urllib.request.Request(target, headers=headers, method='GET')
            with _ssl_urlopen(req, 20) as resp:
                content = resp.read()
                ct = resp.headers.get("Content-Type", "application/octet-stream")
                self.send_response(resp.status)
                self.send_header("Content-Type", ct)
                self.send_header("Content-Length", str(len(content)))
                self.end_headers()
                self.wfile.write(content)
        except urllib.error.HTTPError as e:
            try:
                body = e.read()
                self.send_response(e.code)
                self.send_header("Content-Type", e.headers.get("Content-Type", "application/json"))
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except Exception:
                self.send_error(e.code, str(e))
        except Exception as e:
            log(f"GitHub API proxy error ({path}): {e}")
            self.send_error(502, "Bad Gateway")

    def _proxy_giscus_api(self, path: str):
        """服务端代理 OAuth API 请求到 giscus.app（如 /api/oauth/token），避免重定向导致的 CORS 问题。"""
        target = f"{GISCUS_ORIGIN}{path}"
        qs = urllib.parse.urlparse(self.path).query
        if qs:
            target += '?' + qs

        length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(length) if length else b''

        headers = {
            "User-Agent": user_agent(),
            "Content-Type": self.headers.get('Content-Type', 'application/json'),
            "Origin": GISCUS_ORIGIN,
            "Referer": f"{GISCUS_ORIGIN}/",
        }
        for h in ('Authorization',):
            if h in self.headers:
                headers[h] = self.headers[h]

        try:
            req = urllib.request.Request(target, data=body, headers=headers,
                                         method=self.command)
            with _ssl_urlopen(req, 20) as resp:
                content = resp.read()
                self.send_response(resp.status)
                ct = resp.headers.get("Content-Type", "application/json")
                self.send_header("Content-Type", ct)
                self.send_header("Content-Length", str(len(content)))
                self.end_headers()
                self.wfile.write(content)
        except urllib.error.HTTPError as e:
            try:
                err_body = e.read()
                self.send_response(e.code)
                self.send_header("Content-Type", e.headers.get("Content-Type", "application/json"))
                self.send_header("Content-Length", str(len(err_body)))
                self.end_headers()
                self.wfile.write(err_body)
            except Exception:
                self.send_error(e.code, str(e))
        except Exception as e:
            log(f"Giscus OAuth proxy error ({path}): {e}")
            self.send_error(502, "Bad Gateway")

    # -- P2P 信令 --
    STALE_PEER_TIMEOUT = 15   # 秒 — 超过此时间未收到轮询/保活视为断连

    def _store_signal(self, room: str, from_p: str, to_p: str, sig_type: str, data: dict):
        _incr("p2p", "signals")
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
        _incr("p2p", "rooms_created")
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

            _incr("p2p", "relay_bytes", len(data))

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

    # ── 统计 ──

    def _handle_stats(self):
        """GET /api/stats — 返回全站统计数据"""
        sl_total, sl_expired = 0, 0
        try:
            links = _sl_load()
            now = time.time()
            for v in links.values():
                sl_total += 1
                if isinstance(v, dict) and v.get('expires_at') and now > v['expires_at']:
                    sl_expired += 1
        except: pass

        with _stats_lock:
            r = dict(_stats.get("requests", {}))
            p = dict(_stats.get("p2p", {}))
            sl = dict(_stats.get("short_link", {}))
            started = _stats.get("started_at", 0)

        # P2P 实时数据
        p2p_active_rooms = len(_p2p_rooms)
        p2p_total_peers = sum(len(rd.get("peers", {})) for rd in _p2p_rooms.values())
        p2p_pending_signals = sum(len(sig) for sig in _p2p_signals.values())
        p2p_relay_buffered = sum(len(buf) for buf in _p2p_relay_buffers.values())

        uptime = time.time() - started if started else 0

        bw = dict(_stats.get("bandwidth", {}))

        self._send_json({
            "uptime": round(uptime),
            "requests": r,
            "bandwidth": bw,
            "p2p": {
                "active_rooms": p2p_active_rooms,
                "total_peers": p2p_total_peers,
                "pending_signals": p2p_pending_signals,
                "relay_buffered": p2p_relay_buffered,
                "rooms_created": p.get("rooms_created", 0),
                "signals_sent": p.get("signals", 0),
                "relay_bytes": p.get("relay_bytes", 0),
                "peak_peers": p.get("peak_peers", 0),
            },
            "daily_7d": _get_daily_7d(),
            "short_links": {
                "total": sl_total,
                "expired": sl_expired,
                "created": sl.get("created", 0),
                "redirects": sl.get("redirects", 0),
            },
        })

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

    # ── 每请求结束后从 CountingWriter 上报发送字节 ──
    def handle_one_request(self):
        super().handle_one_request()
        try:
            sent = self.wfile.get_and_reset() if isinstance(self.wfile, _CountingWriter) else 0
            recv = self.rfile.get_and_reset() if isinstance(self.rfile, _CountingReader) else 0
            if sent > 0 or recv > 0:
                _track_bandwidth(sent=sent, received=recv)
        except: pass

    def log_message(self, fmt, *args):
        if len(args) == 3:
            log(f"→ {args[0]}  {args[1]} ({args[2]})")
        elif len(args) == 2:
            log(f"→ ❌ {args[0]} {args[1]}")
        else:
            log(f"→ HTTP {' '.join(str(a) for a in args)}")

    # ── 短链服务 ──

    def _handle_sl_list(self):
        links = _sl_load()
        now = time.time()
        items = sorted(
            ({'code': k, 'url': v.get('url', v) if isinstance(v, dict) else v,
              'created': v.get('created', 0) if isinstance(v, dict) else 0,
              'user': v.get('user', '') if isinstance(v, dict) else '',
              'expires_at': v.get('expires_at', 0) if isinstance(v, dict) else 0,
              'expired': v.get('expires_at', 0) and now > v.get('expires_at', 0)}
             for k, v in links.items()
             if not (isinstance(v, dict) and v.get('expires_at') and now > v['expires_at'])),
            key=lambda x: x['created'], reverse=True
        )
        self._send_json(items)

    def _handle_sl_auth(self):
        """使用 BBS 账号认证，供短链生成器使用。"""
        try:
            body = json.loads(self.rfile.read(int(self.headers.get('Content-Length', 0))))
            username = body.get('username', '').strip()
            password = body.get('password', '').strip()
            if not username or not password:
                self._send_json({'ok': False, 'error': 'credentials_required'})
                return
            # 从 BBS 用户表校验
            try:
                users = json.loads(BBS_USERS_FILE.read_text(encoding='utf-8'))
            except FileNotFoundError:
                log(f"短链登录失败: 用户文件不存在 {BBS_USERS_FILE}")
                self._send_json({'ok': False, 'error': 'server_error', 'detail': 'users_file_not_found'})
                return
            except Exception as e:
                log(f"短链登录失败: 读取用户文件出错 {BBS_USERS_FILE} — {e}")
                self._send_json({'ok': False, 'error': 'server_error', 'detail': str(e)})
                return
            for u in users:
                if u.get('username') == username and u.get('password') == password:
                    token = _bbs_token()
                    BBS_TOKENS[token] = {'user': username, 'at': time.time()}
                    _bbs_save_tokens()
                    log(f'短链/BBS 登录: {username}')
                    self._send_json({'ok': True, 'token': token, 'user': username})
                    return
            self._send_json({'ok': False, 'error': 'auth_failed'})
        except Exception as e:
            self._send_json({'ok': False, 'error': str(e)})

    def _handle_sl_create(self, user):
        _incr("short_link", "created")
        try:
            body = json.loads(self.rfile.read(int(self.headers.get('Content-Length', 0))))
            url = body.get('url', '').strip()
            if not url:
                self._send_json({'ok': False, 'error': 'url_required'})
                return
            if not url.startswith('http://') and not url.startswith('https://'):
                url = 'https://' + url
            links = _sl_load()
            # 查重
            for code, v in links.items():
                existing = v.get('url', v) if isinstance(v, dict) else v
                if existing == url:
                    self._send_json({'ok': True, 'code': code, 'url': f'/s/{code}'})
                    return
            import random, string
            chars = string.ascii_letters + string.digits
            while True:
                code = ''.join(random.choice(chars) for _ in range(6))
                if code not in links:
                    break
            entry = {'url': url, 'created': time.time()}
            username = user.get('username', '') if user else ''
            if user:
                # 已登录 → 永久链接
                entry['user'] = username
                log(f"短链创建: {code} → {url} (by {username})")
            else:
                # 匿名 → 72h 过期
                entry['expires_at'] = time.time() + ANON_SL_TTL
                log(f"短链创建: {code} → {url} (匿名, 72h过期)")
            links[code] = entry
            _sl_save(links)
            self._send_json({'ok': True, 'code': code, 'url': f'/s/{code}',
                             'expires_at': entry.get('expires_at', 0)})
        except Exception as e:
            self._send_json({'ok': False, 'error': str(e)})

    def _handle_sl_delete(self, user):
        try:
            body = json.loads(self.rfile.read(int(self.headers.get('Content-Length', 0))))
            code = body.get('code', '').strip()
            if not code:
                self._send_json({'ok': False, 'error': 'code_required'})
                return
            links = _sl_load()
            if code not in links:
                self._send_json({'ok': False, 'error': 'not_found'})
                return
            entry = links[code]
            owner = entry.get('user', '') if isinstance(entry, dict) else ''
            username = user.get('username', '') if user else ''
            # 已登录用户：只能删除自己的，管理员可以删除所有
            # 匿名：任何人都可以删除（凭 code）
            if user and user.get('role') != 'admin' and owner != username:
                self._send_json({'ok': False, 'error': 'forbidden'})
                return
            del links[code]
            _sl_save(links)
            log(f"短链删除: {code} (by {username or 'anonymous'})")
            self._send_json({'ok': True})
        except Exception as e:
            self._send_json({'ok': False, 'error': str(e)})


    # ── BBS 论坛服务 ────────────────────────────────────────

    def _handle_bbs_auth(self):
        """POST /api/bbs/auth — BBS 账号密码登录或 token 验证"""
        try:
            body = json.loads(self.rfile.read(int(self.headers.get('Content-Length', 0))))
            # 支持 token 验证：前端启动时检查 token 是否有效
            if body.get('verify'):
                info = _bbs_check(self.headers)
                if info:
                    self._send_json({'ok': True, 'token': self.headers.get('X-Auth-Token', ''),
                                     'user_id': info.get('user_id', ''),
                                     'user': info.get('username', ''),
                                     'role': info.get('role', 'user')})
                else:
                    self._send_json({'ok': False, 'error': 'token_invalid'})
                return
            username = body.get('username', '').strip()
            password = body.get('password', '').strip()
            if not username or not password:
                self._send_json({'ok': False, 'error': 'credentials_required'})
                return
            try:
                users = json.loads(BBS_USERS_FILE.read_text(encoding='utf-8'))
            except FileNotFoundError:
                log(f"BBS 登录失败: 用户文件不存在 {BBS_USERS_FILE}")
                self._send_json({'ok': False, 'error': 'server_error', 'detail': 'users_file_not_found'})
                return
            except json.JSONDecodeError as e:
                log(f"BBS 登录失败: JSON 解析错误 {BBS_USERS_FILE} — {e}")
                self._send_json({'ok': False, 'error': 'server_error', 'detail': 'users_file_corrupted'})
                return
            except Exception as e:
                log(f"BBS 登录失败: 读取用户文件出错 {BBS_USERS_FILE} — {e}")
                self._send_json({'ok': False, 'error': 'server_error', 'detail': str(e)})
                return
            for u in users:
                if u.get('username') == username and u.get('password') == password:
                    token = _bbs_token()
                    BBS_TOKENS[token] = {
                        'user_id': u.get('id', ''),
                        'username': u.get('username', ''),
                        'role': u.get('role', 'user'),
                        'at': time.time(),
                    }
                    _bbs_save_tokens()
                    log(f'BBS 登录: {u.get("id", "?")}({username})')
                    self._send_json({'ok': True, 'token': token,
                                     'user_id': u.get('id', ''),
                                     'user': u.get('username', ''),
                                     'role': u.get('role', 'user')})
                    return
            self._send_json({'ok': False, 'error': 'auth_failed'})
        except Exception as e:
            self._send_json({'ok': False, 'error': str(e)})

    def _handle_bbs_topics(self):
        """GET /api/bbs/topics — 帖子列表（每次从磁盘读取）"""
        try:
            _bbs_migrate_old_format()
            topics = _bbs_all_topics()
            params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            q = params.get('q', [None])[0]
            if q:
                q = q.strip().lower()
                topics = [t for t in topics if q in t.get('title', '').lower()
                          or q in t.get('content_preview', '').lower()
                          or q in t.get('author_name', '').lower()]
            page = int(params.get('page', ['1'])[0])
            limit = int(params.get('limit', ['20'])[0])
            limit = min(limit, 50)
            start = (page - 1) * limit
            end = start + limit
            page_items = topics[start:end] if start < len(topics) else []
            self._send_json({
                'topics': page_items,
                'total': len(topics),
                'page': page,
                'limit': limit,
            })
        except Exception as e:
            self._send_json({'ok': False, 'error': str(e)})

    def _handle_bbs_topic_create(self):
        """POST /api/bbs/topics — 创建帖子"""
        user = _bbs_check(self.headers)
        if not user:
            self._send_json({'ok': False, 'error': 'unauthorized'})
            return
        try:
            body = json.loads(self.rfile.read(int(self.headers.get('Content-Length', 0))))
            title = body.get('title', '').strip()
            content = body.get('content', '').strip()
            if not title or not content:
                self._send_json({'ok': False, 'error': 'title_and_content_required'})
                return
            import random, string, hashlib
            tid = _bbs_id()
            title = title[:16]
            topic_path = BBS_TOPICS_DIR / _bbs_topic_filename(tid, title)
            now = time.time()
            author_id = user.get('user_id', '')
            topic_data = {
                'id': tid, 'title': title, 'author_id': author_id,
                'content': content, 'created_at': now, 'updated_at': now,
                'replies': [],
            }
            BBS_TOPICS_DIR.mkdir(parents=True, exist_ok=True)
            topic_path.write_text(json.dumps(topic_data, ensure_ascii=False, indent=2), encoding='utf-8')
            log(f'BBS 新帖: {tid} "{title}" by {author_id}')
            self._send_json({'ok': True, 'id': tid})
        except Exception as e:
            self._send_json({'ok': False, 'error': str(e)})

    def _handle_bbs_topic_detail(self, tid):
        """GET /api/bbs/topics/<id> — 帖子详情（含作者名解析）"""
        try:
            topic_path = _bbs_find_topic_file(tid)
            if not topic_path or not topic_path.exists():
                self._send_json({'ok': False, 'error': 'not_found'})
                return
            topic = json.loads(topic_path.read_text(encoding='utf-8'))
            # 解析作者名
            aid = topic.get('author_id', '')
            info = _bbs_resolve_author(aid)
            topic['author_name'] = info['username'] if info else '账号不存在'
            topic['author_role'] = info['role'] if info else ''
            topic['author_tags'] = info['tags'] if info else []
            # 解析回复作者名
            for r in topic.get('replies', []):
                rid = r.get('author_id', '')
                rinfo = _bbs_resolve_author(rid)
                r['author_name'] = rinfo['username'] if rinfo else '账号不存在'
                r['author_role'] = rinfo['role'] if rinfo else ''
                r['author_tags'] = rinfo['tags'] if rinfo else []
                r.pop('author', None)  # 清理旧字段
            self._send_json(topic)
        except Exception as e:
            self._send_json({'ok': False, 'error': str(e)})

    def _handle_bbs_topic_reply(self, tid):
        """POST /api/bbs/topics/<id>/reply — 回复帖子"""
        user = _bbs_check(self.headers)
        if not user:
            self._send_json({'ok': False, 'error': 'unauthorized'})
            return
        try:
            body = json.loads(self.rfile.read(int(self.headers.get('Content-Length', 0))))
            content = body.get('content', '').strip()
            if not content:
                self._send_json({'ok': False, 'error': 'content_required'})
                return
            topic_path = _bbs_find_topic_file(tid)
            if not topic_path or not topic_path.exists():
                self._send_json({'ok': False, 'error': 'not_found'})
                return
            topic = json.loads(topic_path.read_text(encoding='utf-8'))
            import random, string as _s
            reply_id = _bbs_id()
            reply = {
                'id': reply_id,
                'author_id': user.get('user_id', ''),
                'content': content,
                'created_at': time.time(),
            }
            topic.setdefault('replies', []).append(reply)
            topic['updated_at'] = time.time()
            topic_path.write_text(json.dumps(topic, ensure_ascii=False, indent=2), encoding='utf-8')
            log(f'BBS 回复: {tid} by {user.get("user_id", "?")}')
            self._send_json({'ok': True, 'reply_id': reply_id})
        except Exception as e:
            self._send_json({'ok': False, 'error': str(e)})

    def _handle_bbs_topic_reply_delete(self, tid):
        """POST /api/bbs/topics/<id>/reply-delete — 删除回复"""
        user = _bbs_check(self.headers)
        if not user:
            self._send_json({'ok': False, 'error': 'unauthorized'})
            return
        try:
            body = json.loads(self.rfile.read(int(self.headers.get('Content-Length', 0))))
            reply_id = body.get('reply_id', '').strip()
            if not reply_id:
                self._send_json({'ok': False, 'error': 'reply_id_required'})
                return
            topic_path = _bbs_find_topic_file(tid)
            if not topic_path or not topic_path.exists():
                self._send_json({'ok': False, 'error': 'not_found'})
                return
            topic = json.loads(topic_path.read_text(encoding='utf-8'))
            user_id = user.get('user_id', '')
            role = user.get('role', 'user')
            # 找到回复并检查权限
            idx = None
            for i, r in enumerate(topic.get('replies', [])):
                if r.get('id') == reply_id:
                    if r.get('author_id') != user_id and role != 'admin':
                        self._send_json({'ok': False, 'error': 'forbidden'})
                        return
                    idx = i
                    break
            if idx is None:
                self._send_json({'ok': False, 'error': 'reply_not_found'})
                return
            # 删除回复
            topic['replies'].pop(idx)
            topic['updated_at'] = time.time()
            topic_path.write_text(json.dumps(topic, ensure_ascii=False, indent=2), encoding='utf-8')
            log(f'BBS 删除回复: {tid} reply={reply_id} by {user_id}')
            self._send_json({'ok': True})
        except Exception as e:
            self._send_json({'ok': False, 'error': str(e)})

    def _handle_bbs_topic_delete(self, tid):
        """POST /api/bbs/topics/<id>/delete — 删除帖子"""
        user = _bbs_check(self.headers)
        if not user:
            self._send_json({'ok': False, 'error': 'unauthorized'})
            return
        try:
            topic_path = _bbs_find_topic_file(tid)
            if not topic_path or not topic_path.exists():
                self._send_json({'ok': False, 'error': 'not_found'})
                return
            topic = json.loads(topic_path.read_text(encoding='utf-8'))
            user_id = user.get('user_id', '')
            role = user.get('role', 'user')
            if topic.get('author_id') != user_id and role != 'admin':
                self._send_json({'ok': False, 'error': 'forbidden'})
                return
            topic_path.unlink()
            log(f'BBS 删帖: {tid} by {user_id}')
            self._send_json({'ok': True})
        except Exception as e:
            self._send_json({'ok': False, 'error': str(e)})

    # ── 数据导入导出（ZIP）──────────────────────────────────

    def _handle_bbs_export(self):
        """GET /api/bbs/export — 导出全部论坛数据为 ZIP（仅管理员）"""
        user = _bbs_check(self.headers)
        if not user or user.get('role') != 'admin':
            self._send_json({'ok': False, 'error': 'forbidden'})
            return
        try:
            import io, zipfile
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
                # users.json
                if BBS_USERS_FILE.exists():
                    zf.writestr('users.json', BBS_USERS_FILE.read_text(encoding='utf-8'))
                # 每个帖子的详情
                if BBS_TOPICS_DIR.exists():
                    for fp in BBS_TOPICS_DIR.iterdir():
                        if fp.suffix == '.json':
                            zf.writestr(f'topics/{fp.name}', fp.read_text(encoding='utf-8'))
            data = buf.getvalue()
            self.send_response(200)
            self.send_header('Content-Type', 'application/zip')
            self.send_header('Content-Disposition', 'attachment; filename="bbs-backup.zip"')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            log(f'BBS 导出 ZIP by {user.get("user_id", "?")}')
        except Exception as e:
            self._send_json({'ok': False, 'error': str(e)})

    def _handle_bbs_import(self):
        """POST /api/bbs/import — 从 ZIP 导入论坛数据覆盖全部（仅管理员）"""
        user = _bbs_check(self.headers)
        if not user or user.get('role') != 'admin':
            self._send_json({'ok': False, 'error': 'forbidden'})
            return
        try:
            import io, zipfile
            length = int(self.headers.get('Content-Length', 0))
            raw = self.rfile.read(length)
            zf = zipfile.ZipFile(io.BytesIO(raw), 'r')
            namelist = zf.namelist()

            # 检测 ZIP 内是否有 bbs/ 前缀
            has_bbs = any(n.startswith('bbs/') for n in namelist)
            users_path = 'bbs/users.json' if has_bbs else 'users.json'
            topic_prefix = 'bbs/topics/' if has_bbs else 'topics/'

            # 覆盖 users.json
            if users_path in namelist:
                data = zf.read(users_path)
                BBS_USERS_FILE.write_text(data.decode('utf-8', errors='replace'), encoding='utf-8')

            # 清空 topics 目录再写入（覆盖模式）
            BBS_TOPICS_DIR.mkdir(parents=True, exist_ok=True)
            for f in list(BBS_TOPICS_DIR.iterdir()):
                if f.suffix == '.json':
                    f.unlink()

            for name in namelist:
                if name.startswith(topic_prefix) and name.endswith('.json'):
                    rel = name[len(topic_prefix):]
                    data = zf.read(name)
                    (BBS_TOPICS_DIR / rel).write_text(data.decode('utf-8', errors='replace'), encoding='utf-8')

            zf.close()
            log(f'BBS 导入 ZIP by {user.get("user_id", "?")}')
            self._send_json({'ok': True})
        except Exception as e:
            self._send_json({'ok': False, 'error': str(e)})

    # ── 用户主页 ──────────────────────────────────────────

    def _handle_bbs_user_profile(self, uid):
        """GET /api/bbs/user/<id> — 用户主页：用户信息 + 帖子列表"""
        try:
            info = _bbs_resolve_author(uid)
            if not info:
                self._send_json({'ok': False, 'error': 'user_not_found'})
                return
            # 收集该用户的所有帖子
            all_topics = _bbs_all_topics()
            user_topics = []
            for t in all_topics:
                if t.get('author_id') == uid:
                    user_topics.append({
                        'id': t['id'],
                        'title': t['title'],
                        'reply_count': t.get('reply_count', 0),
                        'created_at': t.get('created_at', 0),
                        'content_preview': t.get('content_preview', ''),
                    })
            user_topics.sort(key=lambda x: x.get('created_at', 0), reverse=True)
            self._send_json({
                'ok': True,
                'user_id': uid,
                'username': info['username'],
                'role': info['role'],
                'tags': info.get('tags', []),
                'topics': user_topics,
                'topic_count': len(user_topics),
            })
        except Exception as e:
            self._send_json({'ok': False, 'error': str(e)})

# ── 数据目录初始化 ─────────────────────────────────────

def _init_data_dirs():
    """确保 .data 目录结构存在"""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    BBS_TOPICS_DIR.mkdir(parents=True, exist_ok=True)
    SHORT_LINKS_DIR.mkdir(parents=True, exist_ok=True)
    _bbs_load_tokens()

# ── 启动入口 ─────────────────────────────────────────────

def main():
    # 日志轮转：保留 7 天
    try:
        if APP_LOG_FILE.exists():
            cutoff = time.time() - 7 * 86400
            with open(APP_LOG_FILE, "r", encoding="utf-8") as f:
                lines = f.readlines()
            keep = []
            for line in lines:
                if line.startswith("[") and len(line) > 20:
                    try:
                        ts = datetime.strptime(line[1:11], "%Y.%m.%d")
                        if ts.timestamp() < cutoff: continue
                    except: pass
                keep.append(line)
            if len(keep) < len(lines):
                APP_LOG_FILE.write_text("".join(keep), encoding="utf-8")
    except: pass

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
                # 清理过期的匿名短链
                try:
                    now = time.time()
                    links = _sl_load()
                    changed = False
                    for code, v in list(links.items()):
                        if isinstance(v, dict) and v.get('expires_at') and now > v['expires_at']:
                            del links[code]
                            changed = True
                    if changed:
                        _sl_save(links)
                except Exception:
                    pass

        threading.Thread(target=_cleanup_loop, daemon=True).start()
        server.serve_forever()
    except KeyboardInterrupt:
        log("\n服务已停止")
        server.server_close()


# 模块导入时自动执行数据目录初始化和 Token 加载
_init_data_dirs()

if __name__ == "__main__":
    main()

