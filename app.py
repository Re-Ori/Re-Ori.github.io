#!/usr/bin/env python3
"""AutoUpdate Web Server -- 服务模块"""
from __future__ import annotations
import os, sys, json, time, ssl, threading, http.server, uuid, io, zipfile
import urllib.parse, urllib.request, urllib.error
from pathlib import Path
from datetime import datetime, timezone, timedelta
import atexit

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
BBS_NOTIFICATIONS_FILE = BBS_DIR / "notifications.json"
BBS_INVITES_FILE = BBS_DIR / "invites.json"
BBS_FILES_DIR = BBS_DIR / "files"
BBS_FILES_META_FILE = BBS_DIR / "files_meta.json"
STORAGE_DEFAULT_QUOTA = 256 * 1024 * 1024  # 256MB

# ── 统计追踪 ──
STATS_DIR = DATA_DIR / "stats"
STATS_DAILY_FILE = STATS_DIR / "daily.json"
STATS_AGGREGATE_FILE = STATS_DIR / "aggregate.json"
_stats_lock = threading.RLock()
_STATS_DIRTY = False

# stats 响应缓存（避免每请求重建 1008 个 slot 条目 + 读短链磁盘）
_stats_cache: dict | None = None
_stats_cache_ts = 0.0
_STATS_CACHE_TTL = 10.0  # 秒

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

def _save_stats(force=False):
    """将聚合统计数据写入磁盘（原子写入：先写 tmp 再 rename）"""
    global _STATS_DIRTY
    if not _STATS_DIRTY and not force:
        return
    try:
        STATS_DIR.mkdir(parents=True, exist_ok=True)
        with _stats_lock:
            data = dict(_stats)
        tmp = STATS_AGGREGATE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        tmp.replace(STATS_AGGREGATE_FILE)
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
        hour = datetime.now().strftime("%H")
        daily = _load_daily()
        day = daily.setdefault(today, {})
        day["bw_sent"] = day.get("bw_sent", 0) + sent
        day["bw_recv"] = day.get("bw_recv", 0) + received
        # 每10分钟带宽明细
        slot = str((datetime.now().hour * 60 + datetime.now().minute) // 10)
        bw_slots = day.setdefault("bw_slots", {})
        bs = bw_slots.setdefault(slot, {"sent": 0, "recv": 0})
        bs["sent"] = bs.get("sent", 0) + sent
        bs["recv"] = bs.get("recv", 0) + received
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

def _save_daily(force=False):
    global _DAILY_DIRTY, _DAILY_CACHE
    if (not _DAILY_DIRTY or _DAILY_CACHE is None) and not force:
        return
    try:
        STATS_DIR.mkdir(parents=True, exist_ok=True)
        tmp = STATS_DAILY_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(_DAILY_CACHE, ensure_ascii=False), encoding="utf-8")
        tmp.replace(STATS_DAILY_FILE)
        _DAILY_DIRTY = False
    except: pass

def _track_daily(category: str):
    today = _daily_date()
    now = datetime.now()
    slot = str((now.hour * 60 + now.minute) // 10)  # 0-143, 每10分钟
    daily = _load_daily()
    day = daily.setdefault(today, {"total": 0, "p2p": 0, "short_link": 0, "short_link_redirect": 0,
        "bbs": 0, "github_proxy": 0, "giscus_proxy": 0, "other_api": 0, "static": 0})
    day["total"] = day.get("total", 0) + 1
    day[category] = day.get(category, 0) + 1
    # 每10分钟明细
    slots = day.setdefault("slots", {})
    s = slots.setdefault(slot, {"total": 0, "p2p": 0, "short_link": 0, "short_link_redirect": 0,
        "bbs": 0, "github_proxy": 0, "giscus_proxy": 0, "other_api": 0, "static": 0})
    s["total"] = s.get("total", 0) + 1
    s[category] = s.get(category, 0) + 1
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
    CATS = ("total","p2p","short_link","short_link_redirect","bbs",
            "github_proxy","giscus_proxy","stats","other_api","static")
    for i in range(6, -1, -1):
        d = (today - timedelta(days=i)).strftime("%Y-%m-%d")
        day = daily.get(d, {})
        entry = {"date": d}
        for c in CATS:
            entry[c] = day.get(c, 0)
        entry["bw_sent"] = day.get("bw_sent", 0)
        entry["bw_recv"] = day.get("bw_recv", 0)

        # 每10分钟明细 — 对象套数组，避免 144×字段名 重复
        raw = day.get("slots", {})
        out = {}
        for c in CATS:
            out[c] = []
        for si in range(144):
            sd = raw.get(str(si), {})
            for c in CATS:
                out[c].append(sd.get(c, 0))
        entry["slots"] = out

        # 带宽明细同理
        bw_raw = day.get("bw_slots", {})
        bw_out = {"sent": [], "recv": []}
        for si in range(144):
            bd = bw_raw.get(str(si), {})
            bw_out["sent"].append(bd.get("sent", 0))
            bw_out["recv"].append(bd.get("recv", 0))
        entry["bw_slots"] = bw_out

        result.append(entry)
    return result

def _stats_flusher():
    while True:
        time.sleep(60)
        try: _save_daily()
        except: pass
        try: _save_stats()
        except: pass

threading.Thread(target=_stats_flusher, daemon=True).start()

# 注册退出钩子，确保进程正常退出时统计数据和 daily 数据落盘
atexit.register(_save_stats, force=True)
atexit.register(_save_daily, force=True)

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
    """将 BBS Token 持久化到磁盘（原子写入）"""
    BBS_TOKENS_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = BBS_TOKENS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(BBS_TOKENS, ensure_ascii=False), encoding='utf-8')
    tmp.replace(BBS_TOKENS_FILE)

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

def _bbs_load_files_meta():
    try:
        if BBS_FILES_META_FILE.exists():
            return json.loads(BBS_FILES_META_FILE.read_text(encoding='utf-8'))
    except: pass
    return []

def _bbs_save_files_meta(meta):
    BBS_FILES_META_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = BBS_FILES_META_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(meta, ensure_ascii=False), encoding='utf-8')
    tmp.replace(BBS_FILES_META_FILE)

def _bbs_ensure_storage_quota(users):
    """确保所有用户有 storage_quota 字段"""
    changed = False
    for u in users:
        if 'storage_quota' not in u:
            u['storage_quota'] = STORAGE_DEFAULT_QUOTA
            changed = True
    return changed

def _sl_load():
    try:
        if SHORT_LINKS_FILE.exists():
            return json.loads(SHORT_LINKS_FILE.read_text(encoding="utf-8"))
    except: pass
    return {}

def _sl_save(d):
    SHORT_LINKS_DIR.mkdir(parents=True, exist_ok=True)
    tmp = SHORT_LINKS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    tmp.replace(SHORT_LINKS_FILE)

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
                tags = u.get('tags', [])
                visible_tags = [t for t in tags if not (isinstance(t, str) and t.startswith('_'))]
                return {
                    'username': u.get('username', ''),
                    'role': u.get('role', 'user'),
                    'tags': visible_tags,
                    'last_login': u.get('last_login', 0),
                }
    except:
        pass
    return None

def _load_notifications():
    try:
        if BBS_NOTIFICATIONS_FILE.exists():
            return json.loads(BBS_NOTIFICATIONS_FILE.read_text(encoding='utf-8'))
    except: pass
    return {"notifications": []}

def _save_notifications(data):
    BBS_NOTIFICATIONS_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = BBS_NOTIFICATIONS_FILE.with_suffix(".tmp.json")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
    tmp.replace(BBS_NOTIFICATIONS_FILE)

def _create_notification(typ, to_uid, from_uid, topic_id, topic_title, reply_id, preview):
    import hashlib, os
    nid = hashlib.sha256(os.urandom(16)).hexdigest()[:12]
    nd = _load_notifications()
    nd["notifications"].append({
        "id": nid, "type": typ,
        "to_user_id": to_uid, "from_user_id": from_uid,
        "topic_id": topic_id, "topic_title": topic_title,
        "reply_id": reply_id, "content_preview": preview[:80],
        "created_at": time.time(), "read": False,
    })
    _save_notifications(nd)
    return nid

def _parse_mentions(content):
    import re
    return re.findall(r'@\{([^}]+)\}', content)

def _invite_code():
    import hashlib, os
    return hashlib.sha256(os.urandom(64)).hexdigest()

def _load_invites():
    try:
        if BBS_INVITES_FILE.exists():
            return json.loads(BBS_INVITES_FILE.read_text(encoding='utf-8'))
    except: pass
    return {"invites": []}

def _save_invites(data):
    BBS_INVITES_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = BBS_INVITES_FILE.with_suffix(".tmp.json")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
    tmp.replace(BBS_INVITES_FILE)

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
            if len(parts) == 2 and parts[1] == 'replies':
                self._handle_bbs_topic_replies(parts[0])
                return
        if req_path.startswith('/api/bbs/emb/'):
            emb_parts = req_path[13:].split('/')
            if len(emb_parts) == 2 and emb_parts[0] and emb_parts[1].startswith('image'):
                self._handle_bbs_emb(emb_parts[0], emb_parts[1][5:])
                return
        if req_path == '/api/bbs/notifications':
            self._handle_bbs_notifications()
            return
        if req_path.startswith('/api/bbs/user/'):
            uid = req_path[14:]
            if uid:
                self._handle_bbs_user_profile(uid)
                return
        if req_path == '/api/bbs/files':
            self._handle_bbs_files_list()
            return
        if req_path == '/api/bbs/files/tree':
            self._handle_bbs_files_tree()
            return
        if req_path == '/api/bbs/files/share/list':
            self._handle_bbs_share_list()
            return
        if req_path.startswith('/api/bbs/files/share/'):
            share_parts = req_path[21:].split('/')
            if len(share_parts) == 2 and share_parts[1] == 'download':
                self._handle_bbs_share_download(share_parts[0])
                return
            if len(share_parts) == 2 and share_parts[1] == 'list':
                self._handle_bbs_share_list_folder(share_parts[0])
                return
            if len(share_parts) == 1 and share_parts[0]:
                self._handle_bbs_share_access(share_parts[0])
                return
        if req_path.startswith('/api/bbs/files/admin/'):
            admin_parts = req_path[21:]
            if admin_parts == 'users':
                self._handle_bbs_files_admin_users()
                return
            if admin_parts == 'files':
                self._handle_bbs_files_admin_user_files()
                return
            if admin_parts == 'shares':
                self._handle_bbs_files_admin_share_list()
                return
            if admin_parts == 'tree':
                self._handle_bbs_files_admin_tree()
                return
        if req_path.startswith('/api/bbs/files/'):
            file_parts = req_path[15:].split('/')
            if len(file_parts) == 2 and file_parts[1] == 'download':
                self._handle_bbs_file_download(file_parts[0])
                return
        if req_path == '/api/bbs/export':
            self._handle_bbs_export()
            return
        if req_path == '/api/admin/download-source':
            self._handle_admin_download_source()
            return
        if req_path == '/api/admin/maintenance':
            self._handle_admin_maintenance()
            return
        if req_path == '/api/admin/users':
            self._handle_admin_users()
            return
        if req_path == '/api/admin/invites':
            self._handle_admin_invites()
            return
        if req_path.startswith('/api/invite/'):
            code = req_path[12:]
            if code:
                self._handle_invite_info(code)
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
        if req_path == '/api/bbs/notifications/read':
            self._handle_bbs_notifications_read()
            return
        if req_path == '/api/bbs/notifications/delete':
            self._handle_bbs_notifications_delete()
            return
        if req_path == '/api/bbs/files/upload':
            self._handle_bbs_file_upload()
            return
        if req_path == '/api/bbs/files/folder':
            self._handle_bbs_folder_create()
            return
        if req_path == '/api/bbs/files/share':
            self._handle_bbs_share_create()
            return
        if req_path.startswith('/api/bbs/files/share/') and req_path.endswith('/verify'):
            code = req_path[21:-7]
            if code:
                self._handle_bbs_share_verify(code)
                return
        if req_path.startswith('/api/bbs/files/share/') and req_path.endswith('/update'):
            code = req_path[21:-7]
            if code:
                self._handle_bbs_share_update(code)
                return
        if req_path.startswith('/api/bbs/files/share/') and req_path.endswith('/delete'):
            code = req_path[21:-7]
            if code:
                self._handle_bbs_share_delete(code)
                return
        if req_path.startswith('/api/bbs/files/admin/') and req_path.endswith('/quota'):
            self._handle_bbs_files_admin_set_quota()
            return
        if req_path.startswith('/api/bbs/files/'):
            file_parts = req_path[15:].split('/')
            if len(file_parts) == 2 and file_parts[1] == 'delete':
                self._handle_bbs_file_delete(file_parts[0])
                return
        if req_path == '/api/bbs/import':
            self._handle_bbs_import()
            return
        if req_path == '/api/admin/upload-source':
            self._handle_admin_upload_source()
            return
        if req_path == '/api/admin/maintenance':
            self._handle_admin_maintenance()
            return
        if req_path == '/api/admin/restart':
            self._handle_admin_restart()
            return
        if req_path == '/api/admin/users':
            self._handle_admin_user_create()
            return
        if req_path.startswith('/api/admin/users/') and req_path.endswith('/delete'):
            uid = req_path[17:-7]
            self._handle_admin_user_delete(uid)
            return
        if req_path.startswith('/api/admin/users/'):
            uid = req_path[17:]
            self._handle_admin_user_update(uid)
            return
        if req_path == '/api/admin/invites':
            self._handle_admin_invite_create()
            return
        if req_path.startswith('/api/admin/invites/') and req_path.endswith('/delete'):
            code = req_path[19:-7]
            if code:
                self._handle_admin_invite_delete(code)
                return
        if req_path.startswith('/api/invite/') and req_path.endswith('/register'):
            code = req_path[12:-9]
            if code:
                self._handle_invite_register(code)
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
        global _stats_cache, _stats_cache_ts
        now_ts = time.time()

        # 缓存命中：跳过磁盘读取和 slot 构建，直接复用
        if _stats_cache and now_ts - _stats_cache_ts < _STATS_CACHE_TTL:
            sl_total, sl_expired = _stats_cache["sl"]
            daily_7d = _stats_cache["daily_7d"]
        else:
            sl_total, sl_expired = 0, 0
            try:
                links = _sl_load()
                for v in links.values():
                    sl_total += 1
                    if isinstance(v, dict) and v.get('expires_at') and now_ts > v['expires_at']:
                        sl_expired += 1
            except: pass
            daily_7d = _get_daily_7d()
            _stats_cache = {"sl": (sl_total, sl_expired), "daily_7d": daily_7d}
            _stats_cache_ts = now_ts

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
            "daily_7d": daily_7d,
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
            # 迁移：确保所有用户有存储配额字段
            if _bbs_ensure_storage_quota(users):
                tmp = BBS_USERS_FILE.with_suffix(".tmp.json")
                tmp.write_text(json.dumps(users, ensure_ascii=False, indent=2), encoding='utf-8')
                tmp.replace(BBS_USERS_FILE)
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
                    log(f'短链/BBS 登录: {u.get("id", "?")}({username})')
                    self._send_json({'ok': True, 'token': token,
                                     'user_id': u.get('id', ''),
                                     'user': u.get('username', ''),
                                     'role': u.get('role', 'user')})
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
            # 迁移：确保所有用户有存储配额字段
            if _bbs_ensure_storage_quota(users):
                tmp = BBS_USERS_FILE.with_suffix(".tmp.json")
                tmp.write_text(json.dumps(users, ensure_ascii=False, indent=2), encoding='utf-8')
                tmp.replace(BBS_USERS_FILE)
            for u in users:
                if u.get('username') == username and u.get('password') == password:
                    token = _bbs_token()
                    BBS_TOKENS[token] = {
                        'user_id': u.get('id', ''),
                        'username': u.get('username', ''),
                        'role': u.get('role', 'user'),
                        'at': time.time(),
                    }
                    # 更新上次登录时间
                    u['last_login'] = time.time()
                    tmp = BBS_USERS_FILE.with_suffix(".tmp.json")
                    tmp.write_text(json.dumps(users, ensure_ascii=False, indent=2), encoding='utf-8')
                    tmp.replace(BBS_USERS_FILE)
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
            log(f"BBS 帖子列表: 磁盘共 {len(topics)} 条")
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
            resp = {
                'topics': page_items,
                'total': len(topics),
                'page': page,
                'limit': limit,
            }
            # 管理员查看时附带隐藏标签映射
            req_user = _bbs_check(self.headers)
            if req_user and req_user.get('role') == 'admin':
                try:
                    all_u = json.loads(BBS_USERS_FILE.read_text(encoding='utf-8'))
                    hidden_map = {}
                    for u in all_u:
                        all_tags = u.get('tags', [])
                        h = [t[1:] for t in all_tags if isinstance(t, str) and t.startswith('_')]
                        if h:
                            hidden_map[u.get('id', '')] = h
                    if hidden_map:
                        resp['hidden_tags_map'] = hidden_map
                except: pass
            self._send_json(resp)
        except Exception as e:
            self._send_json({'ok': False, 'error': str(e)})

    def _handle_bbs_topic_create(self):
        """POST /api/bbs/topics — 创建帖子"""
        user = _bbs_check(self.headers)
        if not user:
            self._send_json({'ok': False, 'error': 'unauthorized'})
            return
        if self._check_maintenance_block(user):
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
            # 支持 ?content_only=1 跳过回复解析（前端分块加载用）
            parsed = urllib.parse.urlparse(self.path)
            params = urllib.parse.parse_qs(parsed.query)
            content_only = 'content_only' in params

            topic_path = _bbs_find_topic_file(tid)
            if not topic_path or not topic_path.exists():
                self._send_json({'ok': False, 'error': 'not_found'})
                return
            topic = json.loads(topic_path.read_text(encoding='utf-8'))
            # 剥离 base64 图片，替换为占位标记
            import re as _re
            emb_counter = [0]
            def _strip_emb(m):
                idx = emb_counter[0]
                emb_counter[0] += 1
                return f'![_emb_{idx}_](/api/bbs/emb/{tid}/image{idx})'
            content = topic.get('content', '')
            has_emb = 'data:image/' in content
            if has_emb:
                topic['content'] = _re.sub(r'!\[([^\]]*)\]\(data:image/[^,]+;base64,[^"\')\s]+\)', _strip_emb, content)
            # 同样处理回复中的图片
            for r in topic.get('replies', []):
                rc = r.get('content', '')
                if 'data:image/' in rc:
                    r['content'] = _re.sub(r'!\[([^\]]*)\]\(data:image/[^,]+;base64,[^"\')\s]+\)', _strip_emb, rc)
            if emb_counter[0] > 0:
                topic['emb_count'] = emb_counter[0]
            # 解析作者名
            aid = topic.get('author_id', '')
            info = _bbs_resolve_author(aid)
            topic['author_name'] = info['username'] if info else '账号不存在'
            topic['author_role'] = info['role'] if info else ''
            topic['author_tags'] = info['tags'] if info else []
            if content_only:
                topic.pop('replies', None)
                self._send_json(topic)
                return
            # 解析回复作者名
            for r in topic.get('replies', []):
                rid = r.get('author_id', '')
                rinfo = _bbs_resolve_author(rid)
                r['author_name'] = rinfo['username'] if rinfo else '账号不存在'
                r['author_role'] = rinfo['role'] if rinfo else ''
                r['author_tags'] = rinfo['tags'] if rinfo else []
                r.pop('author', None)  # 清理旧字段
            # 构建用户 ID→用户名 映射表（供前端 @ 渲染）
            user_map = {}
            user_map[aid] = topic.get('author_name', '')
            for r in topic.get('replies', []):
                rid = r.get('author_id', '')
                if rid and rid not in user_map:
                    rinfo = _bbs_resolve_author(rid)
                    user_map[rid] = rinfo['username'] if rinfo else rid
            topic['user_map'] = user_map
            # 管理员查看时附带隐藏标签映射
            req_user = _bbs_check(self.headers)
            if req_user and req_user.get('role') == 'admin':
                try:
                    all_u = json.loads(BBS_USERS_FILE.read_text(encoding='utf-8'))
                    hidden_map = {}
                    for u in all_u:
                        all_tags = u.get('tags', [])
                        h = [t[1:] for t in all_tags if isinstance(t, str) and t.startswith('_')]
                        if h:
                            hidden_map[u.get('id', '')] = h
                    if hidden_map:
                        topic['hidden_tags_map'] = hidden_map
                except: pass
            self._send_json(topic)
        except Exception as e:
            self._send_json({'ok': False, 'error': str(e)})

    def _handle_bbs_topic_replies(self, tid):
        """GET /api/bbs/topics/<id>/replies — 仅返回帖子回复（分块加载用）"""
        try:
            topic_path = _bbs_find_topic_file(tid)
            if not topic_path or not topic_path.exists():
                self._send_json({'ok': False, 'error': 'not_found'}); return
            topic = json.loads(topic_path.read_text(encoding='utf-8'))
            # 剥离回复中的 base64 图片
            import re as _re
            emb_counter = [0]
            def _strip_emb(m):
                idx = emb_counter[0]
                emb_counter[0] += 1
                return f'![_emb_{idx}_](/api/bbs/emb/{tid}/image{idx})'
            replies = topic.get('replies', [])
            for r in replies:
                rc = r.get('content', '')
                if 'data:image/' in rc:
                    r['content'] = _re.sub(r'!\[([^\]]*)\]\(data:image/[^,]+;base64,[^"\')\s]+\)', _strip_emb, rc)
                rid = r.get('author_id', '')
                rinfo = _bbs_resolve_author(rid)
                r['author_name'] = rinfo['username'] if rinfo else '账号不存在'
                r['author_role'] = rinfo['role'] if rinfo else ''
                r['author_tags'] = rinfo['tags'] if rinfo else []
                r.pop('author', None)
            # 管理员隐藏标签
            req_user = _bbs_check(self.headers)
            hidden_map = {}
            if req_user and req_user.get('role') == 'admin':
                try:
                    all_u = json.loads(BBS_USERS_FILE.read_text(encoding='utf-8'))
                    for u in all_u:
                        at = u.get('tags', [])
                        h = [t[1:] for t in at if isinstance(t, str) and t.startswith('_')]
                        if h: hidden_map[u.get('id', '')] = h
                except: pass
            self._send_json({'ok': True, 'replies': replies, 'hidden_tags_map': hidden_map if hidden_map else None})
        except Exception as e:
            self._send_json({'ok': False, 'error': str(e)})

    def _handle_bbs_emb(self, tid, idx):
        """GET /api/bbs/emb/<tid>/<idx> — 返回嵌入的 base64 图片"""
        try:
            topic_path = _bbs_find_topic_file(tid)
            if not topic_path or not topic_path.exists():
                self.send_error(404); return
            topic = json.loads(topic_path.read_text(encoding='utf-8'))
            # 从帖子和回复中收集所有 base64 图片
            import re as _re
            all_data = topic.get('content', '')
            for r in topic.get('replies', []):
                all_data += '\n' + r.get('content', '')
            matches = list(_re.finditer(r'data:image/[^,]+;base64,[^"\')\s]+', all_data))
            if int(idx) < len(matches):
                data_url = matches[int(idx)].group(0)
                fmt = data_url.split(';')[0].split('/')[1]  # png, jpeg, gif...
                b64_data = data_url.split(',', 1)[1]
                import base64
                raw = base64.b64decode(b64_data)
                self.send_response(200)
                self.send_header('Content-Type', f'image/{fmt}')
                self.send_header('Content-Length', str(len(raw)))
                self.send_header('Cache-Control', 'public, max-age=86400')
                self.end_headers()
                self.wfile.write(raw)
            else:
                self.send_error(404)
        except Exception as e:
            self.send_error(404)

    def _handle_bbs_topic_reply(self, tid):
        """POST /api/bbs/topics/<id>/reply — 回复帖子"""
        user = _bbs_check(self.headers)
        if not user:
            self._send_json({'ok': False, 'error': 'unauthorized'})
            return
        if self._check_maintenance_block(user):
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
            reply_to = body.get('reply_to', '')
            reply = {
                'id': reply_id,
                'author_id': user.get('user_id', ''),
                'content': content,
                'created_at': time.time(),
            }
            if reply_to:
                reply['reply_to'] = reply_to
            topic.setdefault('replies', []).append(reply)
            topic['updated_at'] = time.time()
            topic_path.write_text(json.dumps(topic, ensure_ascii=False, indent=2), encoding='utf-8')
            # 创建通知（已通知集合，避免重复）
            author_id = user.get('user_id', '')
            topic_author = topic.get('author_id', '')
            topic_title = topic.get('title', '')
            preview = content[:40].replace('\n', ' ')
            notified = set()
            if topic_author and topic_author != author_id:
                _create_notification('reply', topic_author, author_id, tid, topic_title, reply_id, preview)
                notified.add(topic_author)
            # 通知被回复者（二级回复）
            if reply_to:
                for r in topic.get('replies', []):
                    if r.get('id') == reply_to and r.get('author_id') != author_id:
                        if r['author_id'] not in notified:
                            _create_notification('reply_nested', r['author_id'], author_id, tid, topic_title, reply_id, preview)
                            notified.add(r['author_id'])
                        break
            # 通知 @{id} 提及的用户
            for mentioned_uid in _parse_mentions(content):
                if mentioned_uid != author_id and mentioned_uid not in notified:
                    _create_notification('mention', mentioned_uid, author_id, tid, topic_title, reply_id, preview)
                    notified.add(mentioned_uid)
            log(f'BBS 回复: {tid} by {author_id}')
            self._send_json({'ok': True, 'reply_id': reply_id})
        except Exception as e:
            self._send_json({'ok': False, 'error': str(e)})

    def _handle_bbs_topic_reply_delete(self, tid):
        """POST /api/bbs/topics/<id>/reply-delete — 删除回复"""
        user = _bbs_check(self.headers)
        if not user:
            self._send_json({'ok': False, 'error': 'unauthorized'})
            return
        if self._check_maintenance_block(user):
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
        if self._check_maintenance_block(user):
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

    def _handle_admin_download_source(self):
        """GET /api/admin/download-source — 下载网站源码 ZIP（仅管理员）"""
        user = _bbs_check(self.headers)
        if not user or user.get('role') != 'admin':
            self._send_json({'ok': False, 'error': 'forbidden'})
            return
        try:
            import io, zipfile, tempfile, os
            root = Path(__file__).resolve().parent
            tmp = tempfile.NamedTemporaryFile(delete=False, suffix='.zip')
            try:
                with zipfile.ZipFile(tmp, 'w', zipfile.ZIP_DEFLATED) as zf:
                    for fp in root.rglob('*'):
                        if fp.is_file():
                            rel = fp.relative_to(root)
                            zf.writestr(rel.as_posix(), fp.read_bytes())
                tmp.close()
                with open(tmp.name, 'rb') as f:
                    data = f.read()
                self.send_response(200)
                self.send_header('Content-Type', 'application/zip')
                self.send_header('Content-Disposition', 'attachment; filename="reori-source.zip"')
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            finally:
                try: os.unlink(tmp.name)
                except: pass
        except Exception as e:
            self._send_json({'ok': False, 'error': str(e)})

    def _handle_admin_upload_source(self):
        """POST /api/admin/upload-source — 上传 ZIP 覆盖网站源码（仅管理员）"""
        user = _bbs_check(self.headers)
        if not user or user.get('role') != 'admin':
            self._send_json({'ok': False, 'error': 'forbidden'})
            return
        try:
            import io, zipfile
            length = int(self.headers.get('Content-Length', 0))
            raw = self.rfile.read(length)
            zf = zipfile.ZipFile(io.BytesIO(raw), 'r')
            root = Path(__file__).resolve().parent
            count = 0
            uploaded_names = []
            for name in zf.namelist():
                if name.endswith('/'):
                    continue
                # 保护 .data/ 目录不被上传覆盖
                if name.startswith('.data/') or name.startswith('\\.data\\'):
                    continue
                target = root / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(zf.read(name))
                count += 1
                uploaded_names.append(name)
            zf.close()
            needs_restart = 'app.py' in uploaded_names or 'server.py' in uploaded_names
            log(f'源码上传 by {user.get("username", "?")} ({count} 个文件)' + (' ，将重启' if needs_restart else ''))
            self._send_json({'ok': True, 'files': count, 'restart': needs_restart})
            if needs_restart:
                import threading, os, sys, time
                def _delayed_restart():
                    time.sleep(1)
                    _save_stats(force=True)
                    _save_daily(force=True)
                    try:
                        import subprocess
                        subprocess.Popen([sys.executable] + sys.argv)
                    except: pass
                    os._exit(0)
                threading.Thread(target=_delayed_restart, daemon=False).start()
        except Exception as e:
            self._send_json({'ok': False, 'error': str(e)})

        # ── 管理员：用户管理 ─────────────────────────────────

    MAINTENANCE_FILE = DATA_DIR / "bbs" / "maintenance.json"

    def _load_users(self):
        try:
            if BBS_USERS_FILE.exists():
                return json.loads(BBS_USERS_FILE.read_text(encoding='utf-8'))
        except Exception as e:
            log(f"读取用户列表失败: {e}")
        return []

    def _save_users(self, users):
        BBS_USERS_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = BBS_USERS_FILE.with_suffix(".tmp.json")
        tmp.write_text(json.dumps(users, ensure_ascii=False, indent=2), encoding='utf-8')
        tmp.replace(BBS_USERS_FILE)

    def _is_admin(self):
        user = _bbs_check(self.headers)
        return user and user.get('role') == 'admin'

    # -- 维护状态 --

    def _load_maintenance(self):
        try:
            if self.MAINTENANCE_FILE.exists():
                return json.loads(self.MAINTENANCE_FILE.read_text(encoding='utf-8'))
        except:
            pass
        return {"enabled": False, "started_at": 0, "auto_close_at": 0}

    def _save_maintenance(self, state):
        self.MAINTENANCE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.MAINTENANCE_FILE.with_suffix(".tmp.json")
        tmp.write_text(json.dumps(state, ensure_ascii=False), encoding='utf-8')
        tmp.replace(self.MAINTENANCE_FILE)

    def _check_maintenance_block(self, user) -> bool:
        m = self._load_maintenance()
        if not m.get('enabled'):
            return False
        auto_close = m.get('auto_close_at', 0)
        if auto_close and time.time() >= auto_close:
            m['enabled'] = False
            self._save_maintenance(m)
            return False
        # 维护模式禁止一切用户操作（包括管理员）
        self._send_json({'ok': False, 'error': 'maintenance',
                         'message': '论坛维护中，暂时无法操作'})
        return True

    def _handle_admin_maintenance(self):
        if self.command == 'GET':
            state = self._load_maintenance()
            if state.get('enabled') and state.get('auto_close_at', 0):
                if time.time() >= state['auto_close_at']:
                    state['enabled'] = False
                    self._save_maintenance(state)
            self._send_json({'ok': True, 'maintenance': state})
            return
        if not self._is_admin():
            self._send_json({'ok': False, 'error': 'forbidden'})
            return
        try:
            body = json.loads(self.rfile.read(int(self.headers.get('Content-Length', 0))))
            state = self._load_maintenance()
            state['enabled'] = bool(body.get('enabled', False))
            if state['enabled']:
                state['started_at'] = time.time()
            else:
                state['started_at'] = 0
                state['auto_close_at'] = 0
            close_min = body.get('auto_close_minutes')
            if close_min and state['enabled']:
                state['auto_close_at'] = time.time() + int(close_min) * 60
            self._save_maintenance(state)
            en_str = '开启' if state['enabled'] else '关闭'
            log(f"维护状态: {en_str}" + (f' (自动关闭于 {close_min} 分钟后)' if close_min else ''))
            self._send_json({'ok': True, 'maintenance': state})
        except Exception as e:
            self._send_json({'ok': False, 'error': str(e)})

    def _handle_admin_restart(self):
        if not self._is_admin():
            self._send_json({'ok': False, 'error': 'forbidden'}); return
        self._send_json({'ok': True, 'msg': '服务器即将重启'})
        import threading, os, sys, time
        def _restart():
            time.sleep(1)
            _save_stats(force=True)
            _save_daily(force=True)
            try:
                import subprocess
                subprocess.Popen([sys.executable] + sys.argv)
            except: pass
            os._exit(0)
        threading.Thread(target=_restart, daemon=False).start()

    # -- 用户 CRUD --

    def _handle_admin_users(self):
        if not self._is_admin():
            self._send_json({'ok': False, 'error': 'forbidden'})
            return
        try:
            users = self._load_users()
            self._send_json({'ok': True, 'users': users})
        except Exception as e:
            self._send_json({'ok': False, 'error': str(e)})

    def _handle_admin_user_create(self):
        if not self._is_admin():
            self._send_json({'ok': False, 'error': 'forbidden'})
            return
        try:
            body = json.loads(self.rfile.read(int(self.headers.get('Content-Length', 0))))
            username = body.get('username', '').strip()
            password = body.get('password', '').strip()
            if not username or not password:
                self._send_json({'ok': False, 'error': '用户名和密码不能为空'})
                return
            users = self._load_users()
            for u in users:
                if u.get('username') == username:
                    self._send_json({'ok': False, 'error': '用户名已存在'})
                    return
            # 使用自定义 ID 或自动生成
            import random, string
            uid = body.get('new_id', '').strip()
            if uid:
                if any(u.get('id') == uid for u in users):
                    self._send_json({'ok': False, 'error': 'ID 已被占用'})
                    return
            else:
                chars = string.ascii_letters + string.digits
                while True:
                    uid = ''.join(random.choices(chars, k=8))
                    if not any(u.get('id') == uid for u in users):
                        break
            new_user = {
                'id': uid, 'username': username, 'password': password,
                'role': body.get('role', 'user'),
                'storage_quota': body.get('storage_quota', STORAGE_DEFAULT_QUOTA),
            }
            tags = body.get('tags')
            if tags:
                new_user['tags'] = tags if isinstance(tags, list) else [tags]
            users.append(new_user)
            self._save_users(users)
            log(f'管理员创建用户: {uid} ({username})')
            self._send_json({'ok': True, 'id': uid, 'password': password})
        except Exception as e:
            log(f"创建用户失败: {e}")
            self._send_json({'ok': False, 'error': str(e)})

    def _handle_admin_user_update(self, uid):
        if not self._is_admin():
            self._send_json({'ok': False, 'error': 'forbidden'})
            return
        try:
            body = json.loads(self.rfile.read(int(self.headers.get('Content-Length', 0))))
            users = self._load_users()
            found = None
            for u in users:
                if u.get('id') == uid:
                    found = u
                    break
            if not found:
                self._send_json({'ok': False, 'error': '用户不存在'})
                return

            new_username = body.get('username', '').strip()
            if new_username and new_username != found.get('username'):
                for u in users:
                    if u.get('username') == new_username:
                        self._send_json({'ok': False, 'error': '用户名已存在'})
                        return
                found['username'] = new_username

            new_id = body.get('new_id', '').strip()
            if new_id and new_id != uid:
                m = self._load_maintenance()
                if not m.get('enabled'):
                    self._send_json({'ok': False, 'error': '修改 ID 需要先开启维护模式'})
                    return
                for u in users:
                    if u.get('id') == new_id:
                        self._send_json({'ok': False, 'error': '新 ID 已被占用'})
                        return
                old_id = found['id']
                found['id'] = new_id
                self._replace_user_id_globally(old_id, new_id)
                log(f'管理员改 ID: {old_id} -> {new_id}')

            if 'password' in body and body['password']:
                found['password'] = body['password']
            if 'role' in body and body['role']:
                found['role'] = body['role']
            if 'tags' in body:
                found['tags'] = body['tags'] if isinstance(body['tags'], list) else [body['tags']]
            if 'storage_quota' in body:
                try:
                    found['storage_quota'] = max(0, int(body['storage_quota']))
                except: pass
            self._save_users(users)
            log(f'管理员更新用户: {found["id"]}')
            self._send_json({'ok': True, 'id': found.get('id', uid)})
        except Exception as e:
            log(f"更新用户失败: {e}")
            self._send_json({'ok': False, 'error': str(e)})

    def _replace_user_id_globally(self, old_id, new_id):
        if not BBS_TOPICS_DIR.exists():
            return
        replaced_count = 0
        for fp in BBS_TOPICS_DIR.iterdir():
            if fp.suffix != '.json':
                continue
            try:
                topic = json.loads(fp.read_text(encoding='utf-8'))
                changed = False
                if topic.get('author_id') == old_id:
                    topic['author_id'] = new_id
                    changed = True
                for r in topic.get('replies', []):
                    if r.get('author_id') == old_id:
                        r['author_id'] = new_id
                        changed = True
                if changed:
                    fp.write_text(json.dumps(topic, ensure_ascii=False, indent=2), encoding='utf-8')
                    replaced_count += 1
            except:
                pass
        log(f'全局 ID 替换: {old_id} -> {new_id}, 涉及 {replaced_count} 个帖子')

    def _handle_admin_user_delete(self, uid):
        if not self._is_admin():
            self._send_json({'ok': False, 'error': 'forbidden'})
            return
        try:
            delete_data = False
            try:
                body = json.loads(self.rfile.read(int(self.headers.get('Content-Length', 0))))
                delete_data = body.get('delete_data', False)
            except: pass
            if delete_data:
                m = self._load_maintenance()
                if not m.get('enabled'):
                    self._send_json({'ok': False, 'error': '删除用户数据需要先开启维护模式'})
                    return
            users = self._load_users()
            before = len(users)
            removed = [u for u in users if u.get('id') == uid]
            users = [u for u in users if u.get('id') != uid]
            if len(users) == before:
                self._send_json({'ok': False, 'error': '用户不存在'})
                return
            self._save_users(users)
            uname = removed[0].get("username", "?") if removed else "?"
            # 删除用户数据
            deleted_topics = 0
            if delete_data and BBS_TOPICS_DIR.exists():
                for fp in list(BBS_TOPICS_DIR.iterdir()):
                    if fp.suffix != '.json':
                        continue
                    try:
                        topic = json.loads(fp.read_text(encoding='utf-8'))
                        if topic.get('author_id') == uid:
                            fp.unlink()
                            deleted_topics += 1
                        else:
                            replies = topic.get('replies', [])
                            new_replies = [r for r in replies if r.get('author_id') != uid]
                            if len(new_replies) < len(replies):
                                topic['replies'] = new_replies
                                fp.write_text(json.dumps(topic, ensure_ascii=False, indent=2), encoding='utf-8')
                    except: pass
            log(f'管理员删除用户: {uid} ({uname})' + (f' 连带删除 {deleted_topics} 个帖子' if delete_data else ''))
            self._send_json({'ok': True, 'deleted_topics': deleted_topics})
        except Exception as e:
            log(f"删除用户失败: {e}")
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

            imported = 0
            for name in namelist:
                if name.startswith(topic_prefix) and name.endswith('.json'):
                    rel = name[len(topic_prefix):]
                    data = zf.read(name)
                    (BBS_TOPICS_DIR / rel).write_text(data.decode('utf-8', errors='replace'), encoding='utf-8')
                    imported += 1

            zf.close()
            log(f'BBS 导入 ZIP by {user.get("user_id", "?")} (users={users_path in namelist}, topics_prefix={topic_prefix!r}, imported={imported})')
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
            resp = {
                'ok': True,
                'user_id': uid,
                'username': info['username'],
                'role': info['role'],
                'tags': info.get('tags', []),
                'last_login': info.get('last_login', 0),
                'topics': user_topics,
                'topic_count': len(user_topics),
            }
            # 管理员查看时附带隐藏标签
            req_user = _bbs_check(self.headers)
            if req_user and req_user.get('role') == 'admin':
                try:
                    all_u = json.loads(BBS_USERS_FILE.read_text(encoding='utf-8'))
                    for u in all_u:
                        if u.get('id') == uid:
                            all_tags = u.get('tags', [])
                            hidden = [t[1:] for t in all_tags if isinstance(t, str) and t.startswith('_')]
                            if hidden:
                                resp['hidden_tags'] = hidden
                            break
                except: pass
            self._send_json(resp)
        except Exception as e:
            self._send_json({'ok': False, 'error': str(e)})

    # ── 通知系统 ─────────────────────────────────────────

    def _handle_bbs_notifications(self):
        """GET /api/bbs/notifications — 获取当前用户通知（?count_only=1 仅返回未读数）"""
        user = _bbs_check(self.headers)
        if not user:
            self._send_json({'ok': False, 'error': 'unauthorized'})
            return
        uid = user.get('user_id', '')
        nd = _load_notifications()
        items = [n for n in nd["notifications"] if n.get("to_user_id") == uid]
        items.sort(key=lambda x: x.get('created_at', 0), reverse=True)
        unread = sum(1 for n in items if not n.get('read'))
        params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        if params.get('count_only', [None])[0]:
            self._send_json({'ok': True, 'unread': unread})
            return
        self._send_json({'ok': True, 'notifications': items[:100], 'unread': unread})

    def _handle_bbs_notifications_read(self):
        """POST /api/bbs/notifications/read — 标记通知已读"""
        user = _bbs_check(self.headers)
        if not user:
            self._send_json({'ok': False, 'error': 'unauthorized'})
            return
        try:
            body = json.loads(self.rfile.read(int(self.headers.get('Content-Length', 0))))
            ids = body.get('ids', [])
            all_flag = body.get('all', False)
        except:
            ids = []
            all_flag = False
        uid = user.get('user_id', '')
        nd = _load_notifications()
        changed = False
        for n in nd["notifications"]:
            if n.get("to_user_id") != uid:
                continue
            if all_flag or n.get('id') in ids:
                if not n.get('read'):
                    n['read'] = True
                    changed = True
        if changed:
            _save_notifications(nd)
        self._send_json({'ok': True})

    def _handle_bbs_notifications_delete(self):
        """POST /api/bbs/notifications/delete — 删除通知（单条或全部）"""
        user = _bbs_check(self.headers)
        if not user:
            self._send_json({'ok': False, 'error': 'unauthorized'})
            return
        try:
            body = json.loads(self.rfile.read(int(self.headers.get('Content-Length', 0))))
            nid = body.get('id', '')
            all_flag = body.get('all', False)
        except:
            nid = ''
            all_flag = False
        uid = user.get('user_id', '')
        nd = _load_notifications()
        before = len(nd["notifications"])
        if all_flag:
            nd["notifications"] = [n for n in nd["notifications"] if n.get("to_user_id") != uid]
        elif nid:
            nd["notifications"] = [n for n in nd["notifications"] if not (n.get("id") == nid and n.get("to_user_id") == uid)]
        if len(nd["notifications"]) < before:
            _save_notifications(nd)
        self._send_json({'ok': True})

    # ── 云文件存储 ──────────────────────────────────────────

    # ── 分享链接存储 ──
    BBS_SHARES_FILE = BBS_DIR / "shares.json"

    def _load_shares(self):
        try:
            if self.BBS_SHARES_FILE.exists():
                return json.loads(self.BBS_SHARES_FILE.read_text(encoding='utf-8'))
        except: pass
        return []

    def _save_shares(self, shares):
        self.BBS_SHARES_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.BBS_SHARES_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(shares, ensure_ascii=False), encoding='utf-8')
        tmp.replace(self.BBS_SHARES_FILE)

    def _share_code(self):
        import random, string
        chars = string.ascii_letters + string.digits
        shares = self._load_shares()
        existing = {s['code'] for s in shares}
        while True:
            code = ''.join(random.choice(chars) for _ in range(12))
            if code not in existing:
                return code

    def _get_file_meta(self, file_id):
        meta = _bbs_load_files_meta()
        for f in meta:
            if f['id'] == file_id:
                return f
        return None

    def _get_user_quota(self, uid):
        users = self._load_users()
        for u in users:
            if u.get('id') == uid:
                return u.get('storage_quota', STORAGE_DEFAULT_QUOTA)
        return STORAGE_DEFAULT_QUOTA

    def _get_username(self, uid):
        users = self._load_users()
        for u in users:
            if u.get('id') == uid:
                return u.get('username', uid)
        return uid

    # ── 文件 & 文件夹操作 ──

    def _handle_bbs_files_tree(self):
        """GET /api/bbs/files/tree — 返回用户的文件夹树（含文件数和大小）"""
        user = _bbs_check(self.headers)
        if not user:
            self._send_json({'ok': False, 'error': 'unauthorized'}); return
        uid = user.get('user_id', '')
        meta = [f for f in _bbs_load_files_meta() if f.get('user_id') == uid]
        # 先构建文件夹列表
        folders = []
        for f in meta:
            if f.get('type') == 'folder':
                folders.append({'id': f['id'], 'name': f['name'], 'parent_id': f.get('parent_id'),
                                'file_count': 0, 'total_size': 0})
        # 统计每个文件夹的直接文件数和大小，同时收集子文件夹ID用于递归
        folder_map = {f['id']: f for f in folders}
        child_map = {}
        for f in meta:
            pid = f.get('parent_id')
            if pid and pid in folder_map and f.get('type') != 'folder':
                folder_map[pid]['file_count'] += 1
                folder_map[pid]['total_size'] += f.get('size', 0)
        self._send_json({'ok': True, 'folders': folders})

    def _handle_bbs_files_list(self):
        """GET /api/bbs/files — 列出文件/文件夹（支持 folder_id 参数）"""
        user = _bbs_check(self.headers)
        if not user:
            self._send_json({'ok': False, 'error': 'unauthorized'}); return
        uid = user.get('user_id', '')
        params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        parent_id = params.get('folder_id', [None])[0]

        meta = _bbs_load_files_meta()
        files = [f for f in meta if f.get('user_id') == uid and f.get('parent_id') == parent_id]
        used = sum(f.get('size', 0) for f in meta if f.get('user_id') == uid and f.get('type') != 'folder')
        quota = self._get_user_quota(uid)

        safe = []
        for f in sorted(files, key=lambda x: (0 if x.get('type') == 'folder' else 1, x.get('name', '').lower())):
            entry = {'id': f['id'], 'name': f['name'], 'type': f.get('type', 'file'), 'size': f.get('size', 0)}
            if f.get('type') == 'file':
                entry['mime'] = f.get('mime', 'application/octet-stream')
                entry['uploaded_at'] = f.get('uploaded_at', 0)
            safe.append(entry)
        self._send_json({'ok': True, 'files': safe, 'quota': quota, 'used': used})

    def _handle_bbs_folder_create(self):
        """POST /api/bbs/files/folder — 创建文件夹"""
        user = _bbs_check(self.headers)
        if not user:
            self._send_json({'ok': False, 'error': 'unauthorized'}); return
        uid = user.get('user_id', '')
        try:
            body = json.loads(self.rfile.read(int(self.headers.get('Content-Length', 0))))
            name = (body.get('name') or '').strip()
            parent_id = body.get('parent_id') or None
            if not name:
                self._send_json({'ok': False, 'error': 'name_required'}); return
            if '/' in name or '\\' in name:
                self._send_json({'ok': False, 'error': 'invalid_name'}); return
            meta = _bbs_load_files_meta()
            folder_id = uuid.uuid4().hex[:16]
            meta.append({
                'id': folder_id, 'name': name, 'type': 'folder',
                'parent_id': parent_id, 'user_id': uid, 'created_at': time.time(),
            })
            _bbs_save_files_meta(meta)
            self._send_json({'ok': True, 'folder_id': folder_id, 'name': name})
        except Exception as e:
            self._send_json({'ok': False, 'error': str(e)})

    def _handle_bbs_file_upload(self):
        """POST /api/bbs/files/upload — 上传文件（multipart/form-data）"""
        user = _bbs_check(self.headers)
        if not user:
            self._send_json({'ok': False, 'error': 'unauthorized'}); return
        uid = user.get('user_id', '')
        try:
            ctype = self.headers.get('Content-Type', '')
            clen = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(clen)
            import re
            boundary = ''
            if 'boundary=' in ctype:
                boundary = ctype.split('boundary=', 1)[1].split(';')[0].strip()
                if boundary.startswith('"') and boundary.endswith('"'):
                    boundary = boundary[1:-1]
            if not boundary:
                self._send_json({'ok': False, 'error': 'invalid_content_type'}); return
            parts = body.split(('--' + boundary).encode())
            file_data = None; filename = 'untitled'; mime_type = 'application/octet-stream'; parent_id = None
            for part in parts:
                if b'Content-Disposition' not in part:
                    continue
                # 提取 field name
                fn_match = re.search(rb'name="([^"]*)"', part)
                if not fn_match:
                    continue
                field_name = fn_match.group(1).decode()
                header_end = part.find(b'\r\n\r\n')
                if header_end < 0:
                    continue
                field_value = part[header_end + 4:]
                if field_value.endswith(b'\r\n'):
                    field_value = field_value[:-2]
                if field_name == 'folder_id':
                    val = field_value.decode().strip()
                    if val: parent_id = val
                elif b'filename=' in part:
                    fname_match = re.search(rb'filename="([^"]*)"', part)
                    if fname_match:
                        filename = fname_match.group(1).decode('utf-8', errors='replace')
                    ct_match = re.search(rb'Content-Type:\s*(\S+)', part, re.IGNORECASE)
                    if ct_match:
                        mime_type = ct_match.group(1).decode()
                    file_data = field_value
            if file_data is None:
                self._send_json({'ok': False, 'error': 'no_file_found'}); return
            file_size = len(file_data)
            if file_size == 0:
                self._send_json({'ok': False, 'error': 'empty_file'}); return
            quota = self._get_user_quota(uid)
            meta = _bbs_load_files_meta()
            used = sum(f.get('size', 0) for f in meta if f.get('user_id') == uid and f.get('type') != 'folder')
            if used + file_size > quota:
                self._send_json({'ok': False, 'error': 'quota_exceeded',
                                 'used': used, 'quota': quota, 'file_size': file_size}); return
            file_id = uuid.uuid4().hex[:16]
            BBS_FILES_DIR.mkdir(parents=True, exist_ok=True)
            (BBS_FILES_DIR / file_id).write_bytes(file_data)
            meta.append({
                'id': file_id, 'name': filename, 'size': file_size, 'mime': mime_type,
                'type': 'file', 'parent_id': parent_id,
                'user_id': uid, 'uploaded_at': time.time(),
            })
            _bbs_save_files_meta(meta)
            log(f"文件上传: {filename} ({file_size}B) by {uid}")
            self._send_json({'ok': True, 'file_id': file_id, 'name': filename, 'size': file_size})
        except Exception as e:
            log(f"文件上传失败: {e}")
            self._send_json({'ok': False, 'error': str(e)})

    def _handle_bbs_file_download(self, file_id):
        """GET /api/bbs/files/<id>/download — 下载文件"""
        # 支持 ?token=xxx 参数（前端直链下载时用）
        params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        token_param = params.get('token', [None])[0]
        if token_param:
            self.headers['X-Auth-Token'] = token_param
        user = _bbs_check(self.headers)
        if not user:
            self._send_json({'ok': False, 'error': 'unauthorized'}); return
        uid = user.get('user_id', '')
        info = self._get_file_meta(file_id)
        if not info:
            self.send_error(404, "File not found"); return
        if info.get('user_id') != uid and user.get('role') != 'admin':
            self.send_error(403, "Forbidden"); return
        if info.get('type') == 'folder':
            try:
                # 递归收集文件夹内所有文件，打包为 ZIP
                meta = _bbs_load_files_meta()
                children_map = {}
                for f in meta:
                    pid = f.get('parent_id')
                    if pid not in children_map:
                        children_map[pid] = []
                    children_map[pid].append(f)
                buf = io.BytesIO()
                visited = set()
                with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
                    def _add_to_zip(folder_id, zip_path, depth=0):
                        if depth > 20 or folder_id in visited:
                            return
                        visited.add(folder_id)
                        for f in children_map.get(folder_id, []):
                            rel = zip_path + '/' + f['name']
                            if f.get('type') == 'folder':
                                _add_to_zip(f['id'], rel, depth + 1)
                            else:
                                fp = BBS_FILES_DIR / f['id']
                                if fp.exists():
                                    zf.writestr(rel.lstrip('/'), fp.read_bytes())
                    _add_to_zip(file_id, info['name'])
                data = buf.getvalue()
                self.send_response(200)
                self.send_header('Content-Type', 'application/zip')
                self.send_header('Content-Disposition', f'attachment; filename="download.zip"; filename*=UTF-8\'\'{urllib.parse.quote(info["name"] + ".zip")}')
                self.send_header('Content-Length', str(len(data)))
                self.send_header('Cache-Control', 'no-store')
                self.end_headers()
                self.wfile.write(data)
            except Exception as e:
                log(f"文件夹下载失败: {e}")
                self.send_error(500, f"下载失败: {e}")
            return
        file_path = BBS_FILES_DIR / file_id
        if not file_path.exists():
            self.send_error(404, "File not found"); return
        data = file_path.read_bytes()
        self.send_response(200)
        self.send_header('Content-Type', info.get('mime', 'application/octet-stream'))
        self.send_header('Content-Disposition', f'attachment; filename="download"; filename*=UTF-8\'\'{urllib.parse.quote(info["name"])}')
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(data)

    def _handle_bbs_file_delete(self, file_id):
        """POST /api/bbs/files/<id>/delete — 删除文件或文件夹（递归删除子项）"""
        user = _bbs_check(self.headers)
        if not user:
            self._send_json({'ok': False, 'error': 'unauthorized'}); return
        uid = user.get('user_id', '')
        meta = _bbs_load_files_meta()
        info = self._get_file_meta(file_id)
        if not info:
            self._send_json({'ok': False, 'error': 'not_found'}); return
        if info.get('user_id') != uid and user.get('role') != 'admin':
            self._send_json({'ok': False, 'error': 'forbidden'}); return
        # 收集所有要删除的 ID（递归）
        to_delete = {file_id}
        if info.get('type') == 'folder':
            stack = [file_id]
            while stack:
                pid = stack.pop()
                for f in meta:
                    if f.get('parent_id') == pid and f['id'] not in to_delete:
                        to_delete.add(f['id'])
                        if f.get('type') == 'folder':
                            stack.append(f['id'])
        # 删除文件实体 + 元数据
        for fid in to_delete:
            fp = BBS_FILES_DIR / fid
            if fp.exists():
                fp.unlink()
        meta = [f for f in meta if f['id'] not in to_delete]
        _bbs_save_files_meta(meta)
        # 同时删除相关分享链接
        shares = self._load_shares()
        shares = [s for s in shares if s.get('item_id') not in to_delete]
        self._save_shares(shares)
        log(f"删除: {info['name']} by {uid} ({len(to_delete)}项)")
        self._send_json({'ok': True})

    # ── 分享链接 ──

    def _handle_bbs_share_create(self):
        """POST /api/bbs/files/share — 创建分享链接"""
        user = _bbs_check(self.headers)
        if not user:
            self._send_json({'ok': False, 'error': 'unauthorized'}); return
        uid = user.get('user_id', '')
        try:
            body = json.loads(self.rfile.read(int(self.headers.get('Content-Length', 0))))
            item_id = (body.get('item_id') or '').strip()
            password = (body.get('password') or '').strip() or None
            expires_in = body.get('expires_in', 0)
            if not item_id:
                self._send_json({'ok': False, 'error': 'item_id_required'}); return
            info = self._get_file_meta(item_id)
            if not info:
                self._send_json({'ok': False, 'error': 'not_found'}); return
            if info.get('user_id') != uid and user.get('role') != 'admin':
                self._send_json({'ok': False, 'error': 'forbidden'}); return
            code = self._share_code()
            expires_at = (time.time() + expires_in) if expires_in > 0 else 0
            shares = self._load_shares()
            share_name = (body.get('name') or '').strip() or info['name']
            share = {
                'code': code, 'item_id': item_id, 'type': info.get('type', 'file'),
                'name': share_name, 'owner_id': uid, 'owner_name': user.get('username', uid),
                'created_at': time.time(), 'expires_at': expires_at,
            }
            if password:
                share['password'] = password
            default_pwd = (body.get('default_password') or '').strip()
            if default_pwd:
                share['default_password'] = default_pwd
            shares.append(share)
            self._save_shares(shares)
            log(f"创建分享: {code} → {info['name']} by {uid}")
            self._send_json({'ok': True, 'code': code, 'url': f'/Tool/files.html?share={code}'})
        except Exception as e:
            self._send_json({'ok': False, 'error': str(e)})

    def _send_share_password_page(self, code):
        import html as _html
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.end_headers()
        self.wfile.write(f'''<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>输入密码 — Origin 云文件</title>
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{font-family:-apple-system,sans-serif;display:flex;align-items:center;justify-content:center;min-height:100vh;background:#f5f5f7;padding:20px}}
.card{{background:#fff;border-radius:16px;padding:40px;max-width:380px;width:100%;box-shadow:0 4px 24px rgba(0,0,0,0.06);text-align:center}}
h2{{font-size:20px;margin-bottom:6px}}
p{{font-size:13px;color:#999;margin-bottom:20px}}
input{{width:100%;padding:12px 14px;border:1px solid rgba(0,0,0,0.12);border-radius:10px;font-size:15px;outline:none;margin-bottom:12px;box-sizing:border-box;background:rgba(255,255,255,0.8);color:#333}}
input:focus{{border-color:#399FFF}}
.btn{{width:100%;padding:12px;border:none;border-radius:10px;font-size:15px;font-weight:600;cursor:pointer;background:#399FFF;color:#fff}}
.btn:hover{{opacity:.85}}
.err{{color:#ff3b30;font-size:13px;margin-bottom:8px;display:none}}
</style></head><body><div class="card">
<svg width="36" height="36" viewBox="0 0 24 24" fill="none" stroke="#399FFF" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" style="margin-bottom:12px"><rect x="3" y="11" width="18" height="11" rx="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/></svg>
<h2>此分享需要密码</h2>
<p>请输入访问密码</p>
<div class="err" id="err"></div>
<input type="password" id="pwd" placeholder="输入密码" autocomplete="off">
<button class="btn" onclick="verifyPwd()">验证</button>
</div>
<script>
function verifyPwd(){{
  var p=document.getElementById("pwd").value.trim();
  if(!p)return;
  var x=new XMLHttpRequest();
  x.open("POST","/api/bbs/files/share/{_html.escape(code)}/verify",true);
  x.setRequestHeader("Content-Type","application/json");
  x.onload=function(){{
    try{{var r=JSON.parse(x.responseText)}}catch(e){{return}}
    if(r.ok){{window.location.href="?v="+encodeURIComponent(r.token||"")}}
    else{{document.getElementById("err").textContent="密码错误";document.getElementById("err").style.display=""}}
  }};
  x.send(JSON.stringify({{password:p}}));
}}
document.getElementById("pwd").addEventListener("keydown",function(e){{if(e.key==="Enter")verifyPwd()}});
</script></body></html>'''.encode('utf-8'))

    def _handle_bbs_share_list(self):
        """GET /api/bbs/files/share/list — 列出自己的分享链接"""
        user = _bbs_check(self.headers)
        if not user:
            self._send_json({'ok': False, 'error': 'unauthorized'}); return
        uid = user.get('user_id', '')
        shares = self._load_shares()
        mine = [s for s in shares if s.get('owner_id') == uid]
        safe = []
        for s in mine:
            safe.append({
                'code': s['code'], 'name': s['name'], 'type': s.get('type', 'file'),
                'has_password': 'password' in s,
                'has_default_password': 'default_password' in s,
                'created_at': s.get('created_at', 0),
                'expires_at': s.get('expires_at', 0),
            })
        self._send_json({'ok': True, 'shares': safe})

    def _handle_bbs_share_access(self, code):
        """GET /api/bbs/files/share/<code> — 访问分享页面"""
        shares = self._load_shares()
        share = None
        for s in shares:
            if s['code'] == code:
                share = s; break
        if not share:
            self._send_share_page('分享链接无效', '该分享链接不存在或已被删除', None); return
        if share.get('expires_at') and time.time() > share['expires_at']:
            self._send_share_page('分享已过期', '该分享链接已过期', None); return
        if share.get('password'):
            owner = _bbs_check(self.headers)
            is_owner = owner and owner.get('user_id') == share.get('owner_id')
            if not is_owner:
                vp = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).get('v', [None])[0]
                if vp and vp == share['password']:
                    pass
                else:
                    self._send_share_password_page(share['code']); return
        info = self._get_file_meta(share['item_id'])
        if not info:
            self._send_share_page('文件已删除', '该分享的文件已被删除', None); return
        # 支持 ?folder=xxx 直接进入子文件夹
        params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        deep_folder = params.get('folder', [None])[0]
        if deep_folder and info.get('type') == 'folder':
            # 验证 deep_folder 属于分享根
            meta = _bbs_load_files_meta()
            root = info['id']; valid = {root}; stack = [root]
            while stack:
                pid = stack.pop()
                for f in meta:
                    if f.get('parent_id') == pid and f['id'] not in valid:
                        valid.add(f['id'])
                        if f.get('type') == 'folder': stack.append(f['id'])
            if deep_folder not in valid:
                deep_folder = None
        self._send_share_page(info['name'], None, {'share': share, 'info': info, 'deep_folder': deep_folder})

    def _send_share_page(self, title, msg, data):
        import html as _html
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.end_headers()
        if msg:
            body = f'<div class="card"><h2>{_html.escape(title)}</h2><p style="color:#999">{_html.escape(msg)}</p></div>'
        else:
            info = data['info']; share = data['share']; code = share['code']; name = _html.escape(info['name'])
            owner = _html.escape(share.get('owner_name', '未知'))
            ctime = ''
            if share.get('created_at'):
                ctime = __import__('datetime').datetime.fromtimestamp(share['created_at']).strftime('%Y-%m-%d %H:%M')
            atext = '' if not share.get('expires_at') else f' · 过期: {__import__("datetime").datetime.fromtimestamp(share["expires_at"]).strftime("%m-%d %H:%M")}' if share["expires_at"] > 0 else ''
            meta_html = f'<div class="meta">发布者: {owner} · {ctime}{atext}</div>'
            if info.get('type') == 'folder':
                _FJS = '''<script>
var _sd=document.getElementById("share-page"),_code=_sd.dataset.code,_root=_sd.dataset.root,_stk=[],_cur=_root,_vt=(new URLSearchParams(window.location.search)).get("v")||"";
function _dl(fid){var u="/api/bbs/files/share/"+_code+"/download?item_id="+fid;if(_vt)u+="&v="+encodeURIComponent(_vt);return u}
function ld(fid,nm){
  _cur=fid;_upURL(fid);
  var el=document.getElementById("file-list"),lo=document.getElementById("fl-loading"),fn=document.getElementById("folder-name"),zb=document.getElementById("zip-dl-btn");
  if(nm)fn.textContent=nm;lo.style.display="";el.innerHTML="";
  var x=new XMLHttpRequest();x.open("GET","/api/bbs/files/share/__CODE__/list?folder_id="+fid,true);
  x.onload=function(){lo.style.display="none";try{var r=JSON.parse(x.responseText)}catch(e){return}
  if(!r.ok||!r.items)return;zb.href=_dl(fid);
  var h="";if(nm||_stk.length>0||fid!==_root){h+='<div class="fi folder" data-up="1"><span class="fn" style="color:var(--accent)">&larr; 返回</span></div>'}
  for(var i=0;i<r.items.length;i++){var f=r.items[i];
    var sv=_ic(f),sz=f.type==="folder"?"":_sz(f.size),tm=f.uploaded_at?_tm(f.uploaded_at):"";
    if(f.type==="folder"){h+='<div class="fi folder" data-fid="'+f.id+'" data-nm="'+_e(f.name).replace(/"/g,"&quot;")+'"><span class="fi-icon">'+sv+'</span><span class="fn">'+_e(f.name)+'</span><span class="fs">文件夹</span></div>'}
    else{h+='<div class="fi"><span class="fi-icon">'+sv+'</span><span class="fn">'+_e(f.name)+'</span><span class="fs">'+sz+'</span><span class="ft">'+tm+'</span><a class="dl-btn" href="'+_dl(f.id)+'">下载</a></div>'}}
  el.innerHTML=h;}
  x.send()}
document.getElementById("file-list").addEventListener("click",function(e){
  var row=e.target.closest(".fi.folder");if(!row)return;
  if(row.dataset.up){var p=_stk.pop();if(p)ld(p.id,p.name);return}
  if(row.dataset.fid){_stk.push({id:_cur,name:document.getElementById("folder-name").textContent});ld(row.dataset.fid,row.dataset.nm)}
});
function _e(s){var d=document.createElement("div");d.textContent=s;return d.innerHTML}
function _sz(b){if(b<=0)return"0 B";var u=["B","KB","MB","GB"],i=0,s=b;while(s>=1024&&i<3){s/=1024;i++}return(i===0?s:s.toFixed(1))+" "+u[i]}
function _tm(ts){if(!ts)return"";var d=new Date(ts*1e3);return("0"+d.getHours()).slice(-2)+":"+("0"+d.getMinutes()).slice(-2)}
function _ic(f){if(f.type==="folder")return'<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="#ff9500" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/></svg>';var e=(f.name||"").split(".").pop().toLowerCase();if(["jpg","jpeg","png","gif","webp","svg","bmp"].includes(e))return'<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="#34c759" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="18" height="18" rx="2"/><circle cx="8.5" cy="8.5" r="1.5"/><polyline points="21 15 16 10 5 21"/></svg>';if(["zip","rar","7z","gz","tar"].includes(e))return'<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="#ff9500" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M21 16V8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16z"/></svg>';if(["mp3","wav","ogg","flac","aac"].includes(e))return'<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="#af52de" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M9 18V5l12-2v13"/><circle cx="6" cy="18" r="3"/><circle cx="18" cy="16" r="3"/></svg>';if(["mp4","avi","mkv","mov","webm"].includes(e))return'<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="#ff2d55" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><polygon points="23 7 16 12 23 17 23 7"/><rect x="1" y="5" width="15" height="14" rx="2"/></svg>';if(["pdf"].includes(e))return'<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="#ff3b30" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="16" y1="13" x2="8" y2="13"/><line x1="16" y1="17" x2="8" y2="17"/></svg>';return'<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="#399FFF" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/></svg>'}
var _dp=document.getElementById("share-page").dataset.deep||"";
function _upURL(fid){var u=window.location.pathname;if(fid&&fid!==_root)u+="?folder="+fid;window.history.replaceState({folder:fid},"",u)}
// 恢复历史导航
window.addEventListener("popstate",function(e){if(e.state&&e.state.folder){var f=e.state.folder;if(f&&f!==_root){var nm="";var el2=document.querySelector('[data-fid="'+f+'"]');if(el2)nm=el2.dataset.nm;ld(f,nm)}else ld(_root)}});
// 深链接初始导航
if(_dp&&_dp!==_root){setTimeout(function(){_stk.push({id:_root,name:document.getElementById("folder-name").textContent});ld(_dp)},100)}
else{ld(_root)}
</script>'''
                _FJS = _FJS.replace('__CODE__', code)
                deep_init = data.get('deep_folder') or ''
                body = '<div id="share-page" data-code="' + code + '" data-root="' + info['id'] + '" data-deep="' + deep_init + '">' + '''
<div class="card">
  <div class="folder-icon"><svg width="40" height="40" viewBox="0 0 24 24" fill="none" stroke="#ff9500" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/></svg></div>
  <h2 id="folder-name">''' + name + '''</h2>
  ''' + meta_html + '''
  <div id="folder-actions" style="margin-bottom:12px">
    <a class="dl-btn primary" href="/api/bbs/files/share/''' + code + '''/download''' + ('?v=' + data['share'].get('password', '') if data and data['share'].get('password') else '') + '''" id="zip-dl-btn"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="vertical-align:-2px;margin-right:4px"><path d="M21 16V8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16z"/></svg> 下载全部 ZIP</a>
  </div>
  <div id="file-list" class="file-list"></div>
  <div id="fl-loading" style="color:#999;font-size:13px;padding:12px">加载中&hellip;</div>
</div></div>''' + _FJS
            else:
                ext = info['name'].rsplit('.',1)[-1].lower() if '.' in info['name'] else ''
                svg_icon = '<svg width="40" height="40" viewBox="0 0 24 24" fill="none" stroke="#399FFF" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/></svg>'
                if ext in ('jpg','jpeg','png','gif','webp','svg','bmp'): svg_icon = '<svg width="40" height="40" viewBox="0 0 24 24" fill="none" stroke="#34c759" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="18" height="18" rx="2"/><circle cx="8.5" cy="8.5" r="1.5"/><polyline points="21 15 16 10 5 21"/></svg>'
                elif ext in ('zip','rar','7z','gz','tar'): svg_icon = '<svg width="40" height="40" viewBox="0 0 24 24" fill="none" stroke="#ff9500" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M21 16V8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16z"/></svg>'
                elif ext in ('mp3','wav','ogg'): svg_icon = '<svg width="40" height="40" viewBox="0 0 24 24" fill="none" stroke="#af52de" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M9 18V5l12-2v13"/><circle cx="6" cy="18" r="3"/><circle cx="18" cy="16" r="3"/></svg>'
                elif ext in ('mp4','avi','mkv'): svg_icon = '<svg width="40" height="40" viewBox="0 0 24 24" fill="none" stroke="#ff2d55" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><polygon points="23 7 16 12 23 17 23 7"/><rect x="1" y="5" width="15" height="14" rx="2"/></svg>'
                elif ext in ('pdf'): svg_icon = '<svg width="40" height="40" viewBox="0 0 24 24" fill="none" stroke="#ff3b30" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="16" y1="13" x2="8" y2="13"/><line x1="16" y1="17" x2="8" y2="17"/></svg>'
                body = f'''<div class="card"><div class="file-icon">{svg_icon}</div>
  <h2>{name}</h2>
  {meta_html}
  <p style="color:#999;font-size:13px">{self._fmt_size_share(info.get('size',0))}</p>
  <a class="dl-btn primary" href="/api/bbs/files/share/{code}/download" style="margin-top:16px"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="vertical-align:-2px;margin-right:4px"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg> 下载文件</a>
</div>'''
        page_css = '''
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;min-height:100vh;margin:0;background:#f5f5f7;padding:20px;color:#1a1a1a}
.card{background:#fff;border-radius:16px;padding:36px;max-width:560px;width:100%;margin:0 auto;box-shadow:0 4px 24px rgba(0,0,0,0.06);text-align:center}
h2{font-size:20px;margin:12px 0 6px;word-break:break-all}
.meta{font-size:12px;color:#999;margin-bottom:14px;line-height:1.6}
.file-icon,.folder-icon{font-size:48px;line-height:1}
:root{--accent:#399FFF}
.dl-btn{display:inline-block;padding:9px 20px;border-radius:8px;font-size:13px;font-weight:600;cursor:pointer;text-decoration:none;background:rgba(0,0,0,0.05);color:#555;transition:all .15s;white-space:nowrap}
.dl-btn.primary{background:var(--accent);color:#fff}
.dl-btn.primary:hover{opacity:.85}
.dl-btn:hover{background:rgba(0,0,0,0.08)}
.file-list{margin-top:4px;text-align:left}
.fi{display:flex;align-items:center;gap:6px;padding:8px 10px;border-radius:8px;transition:background .15s;cursor:default}
.fi:hover{background:rgba(0,0,0,0.03)}
.fi.folder{cursor:pointer}
.fi.folder:hover{background:rgba(0,0,0,0.06)}
.fi .fi-icon{flex-shrink:0;width:20px;display:flex;align-items:center;justify-content:center}
.fi .fn{flex:1;font-size:13px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.fi .fs{font-size:11px;color:#999;flex-shrink:0;min-width:48px;text-align:right}
.fi .ft{font-size:11px;color:#bbb;flex-shrink:0;min-width:36px;text-align:right}
.fi .dl-btn{padding:4px 12px;font-size:12px}
'''
        self.wfile.write(f'<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0"><title>{_html.escape(title)} — Origin 云文件</title><style>{page_css}</style></head><body>{body}</body></html>'.encode('utf-8'))

    def _fmt_size_share(self, b):
        if b <= 0: return '0 B'
        u = ['B','KB','MB','GB']; i = 0; s = float(b)
        while s >= 1024 and i < len(u)-1: s /= 1024; i += 1
        return f'{s:.1f} {u[i]}' if i > 0 else f'{int(s)} B'

    def _handle_bbs_share_verify(self, code):
        """POST /api/bbs/files/share/<code>/verify — 验证分享密码"""
        try:
            body = json.loads(self.rfile.read(int(self.headers.get('Content-Length', 0))))
            password = body.get('password', '')
            shares = self._load_shares()
            share = None
            for s in shares:
                if s['code'] == code:
                    share = s; break
            if not share:
                self._send_json({'ok': False, 'error': 'not_found'}); return
            if share.get('expires_at') and time.time() > share['expires_at']:
                self._send_json({'ok': False, 'error': 'expired'}); return
            if 'password' not in share or password == share['password']:
                self._send_json({'ok': True, 'token': share.get('password', '')}); return
            self._send_json({'ok': False, 'error': 'wrong_password'}); return
            self._return_share_content(share)
        except Exception as e:
            self._send_json({'ok': False, 'error': str(e)})

    def _handle_bbs_share_list_folder(self, code):
        """GET /api/bbs/files/share/<code>/list?folder_id=xxx — 分享文件夹内子文件夹内容"""
        shares = self._load_shares()
        share = None
        for s in shares:
            if s['code'] == code: share = s; break
        if not share: self._send_json({'ok':False,'error':'not_found'}); return
        if share.get('expires_at') and time.time() > share['expires_at']: self._send_json({'ok':False,'error':'expired'}); return
        params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        folder_id = params.get('folder_id', [share['item_id']])[0]
        info = self._get_file_meta(folder_id)
        if not info: self._send_json({'ok':False,'error':'not_found'}); return
        # 验证 folder 属于分享根
        meta = _bbs_load_files_meta()
        root = share['item_id']
        valid = {root}; stack = [root]
        while stack:
            pid = stack.pop()
            for f in meta:
                if f.get('parent_id') == pid and f['id'] not in valid:
                    valid.add(f['id'])
                    if f.get('type') == 'folder': stack.append(f['id'])
        if folder_id not in valid:
            self._send_json({'ok':False,'error':'forbidden'}); return
        children = [f for f in meta if f.get('parent_id') == folder_id]
        # ?all=1 递归返回所有后代
        if params.get('all', [None])[0]:
            all_items = list(children)
            stack = [f['id'] for f in children if f.get('type') == 'folder']
            while stack:
                pid = stack.pop()
                for f in meta:
                    if f.get('parent_id') == pid:
                        all_items.append(f)
                        if f.get('type') == 'folder':
                            stack.append(f['id'])
            children = all_items
        items = []
        for f in sorted(children, key=lambda x: (0 if x.get('type')=='folder' else 1, x.get('name','').lower())):
            items.append({
                'id': f['id'], 'name': f['name'], 'type': f.get('type','file'),
                'size': f.get('size',0), 'mime': f.get('mime',''),
                'uploaded_at': f.get('uploaded_at',0), 'parent_id': f.get('parent_id'),
            })
        share_info = {}
        if params.get('all', [None])[0]:
            fpw = share.get('file_passwords', {})
            share_info = {
                'has_default_password': 'default_password' in share,
                'file_pwd_status': {k: ('__NOPASS__' if v == '__NOPASS__' else v) for k, v in fpw.items()},
                'root_id': share.get('item_id', ''),
            }
        self._send_json({'ok':True, 'items':items, 'folder_name': info.get('name',''), 'parent_id': info.get('parent_id'), 'share': share_info})

    def _handle_bbs_share_download(self, code):
        """GET /api/bbs/files/share/<code>/download — 通过分享链接下载文件/文件夹ZIP"""
        shares = self._load_shares()
        share = None
        for s in shares:
            if s['code'] == code:
                share = s; break
        if not share:
            self.send_error(404, "Not found"); return
        if share.get('expires_at') and time.time() > share['expires_at']:
            self.send_error(410, "Expired"); return
        params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        log(f"[DL] code={code}")
        # 单一密码检查：解析出最终密码值，与分享的各级密码比对
        NOPASS = '__NOPASS__'
        owner = _bbs_check(self.headers)
        is_owner = owner and owner.get('user_id') == share.get('owner_id')
        vp = params.get('v', [None])[0] or ''
        pwd_raw = (params.get('pwd', [None])[0] or '').strip()
        pwd_val = vp if (vp and not pwd_raw) else (pwd_raw if pwd_raw else '')
        item_id = params.get('item_id', [None])[0]
        target_id = item_id or share['item_id']
        password_ok = is_owner
        if not password_ok:
            fpw = share.get('file_passwords', {})
            # 单文件密码
            if target_id in fpw:
                password_ok = (fpw[target_id] == NOPASS) or (pwd_val == fpw[target_id])
            # 默认密码
            if not password_ok and share.get('default_password'):
                password_ok = (pwd_val == share['default_password'])
            # 分享密码（v 令牌直接比对）
            if not password_ok and share.get('password'):
                password_ok = (pwd_val == share['password'])
            log(f"[DL] password_ok={password_ok}")
        if not password_ok:
            self.send_error(403, "Password required"); return
        log(f"[DL] password ok")
        info = self._get_file_meta(target_id)
        if not info:
            self.send_error(404, "File not found"); return
        # 验证 item 确实属于分享
        if item_id:
            meta = _bbs_load_files_meta()
            owner_folder = share['item_id']
            # 从共享根开始 BFS，验证目标属于此分享
            visited = {owner_folder}
            stack = [owner_folder]
            found = target_id == owner_folder
            if not found:
                while stack:
                    pid = stack.pop()
                    for f in meta:
                        if f.get('parent_id') == pid and f['id'] not in visited:
                            visited.add(f['id'])
                            if f['id'] == target_id:
                                found = True
                                break
                            if f.get('type') == 'folder':
                                stack.append(f['id'])
                    if found: break
            if not found:
                self.send_error(403, "Forbidden"); return

        if info.get('type') == 'folder':
            # 打包为 ZIP
            try:
                meta = _bbs_load_files_meta()
                cmap = {}
                for f in meta:
                    pid = f.get('parent_id')
                    if pid not in cmap: cmap[pid] = []
                    cmap[pid].append(f)
                buf = io.BytesIO()
                visited_set = set()
                with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
                    def _add(pid, zp, depth=0):
                        if depth > 20 or pid in visited_set: return
                        visited_set.add(pid)
                        for f in cmap.get(pid, []):
                            r = zp + '/' + f['name']
                            if f.get('type') == 'folder': _add(f['id'], r, depth+1)
                            else:
                                fp = BBS_FILES_DIR / f['id']
                                if fp.exists(): zf.writestr(r.lstrip('/'), fp.read_bytes())
                    _add(target_id, info['name'])
                data = buf.getvalue()
                self.send_response(200)
                self.send_header('Content-Type', 'application/zip')
                self.send_header('Content-Disposition', f'attachment; filename="download.zip"; filename*=UTF-8\'\'{urllib.parse.quote(info["name"] + ".zip")}')
                self.send_header('Content-Length', str(len(data)))
                self.send_header('Cache-Control', 'no-store')
                self.end_headers()
                self.wfile.write(data)
            except Exception as e:
                self.send_error(500, f"ZIP 打包失败: {e}")
            return

        file_path = BBS_FILES_DIR / info['id']
        if not file_path.exists():
            self.send_error(404, "File not found"); return
        data = file_path.read_bytes()
        self.send_response(200)
        self.send_header('Content-Type', info.get('mime', 'application/octet-stream'))
        self.send_header('Content-Disposition', f'attachment; filename="download"; filename*=UTF-8\'\'{urllib.parse.quote(info["name"])}')
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(data)

    def _return_share_content(self, share):
        """返回分享的内容（文件信息或文件夹列表）"""
        info = self._get_file_meta(share['item_id'])
        if not info:
            self._send_json({'ok': False, 'error': 'item_deleted'}); return
        if info.get('type') == 'file':
            self._send_json({
                'ok': True, 'type': 'file',
                'name': info['name'], 'size': info.get('size', 0),
                'mime': info.get('mime', 'application/octet-stream'),
                'download_url': f'/api/bbs/files/share/{share["code"]}/download',
            })
        else:
            meta = _bbs_load_files_meta()
            children = [f for f in meta if f.get('parent_id') == share['item_id']]
            items = []
            for f in sorted(children, key=lambda x: (0 if x.get('type') == 'folder' else 1, x.get('name', '').lower())):
                items.append({
                    'id': f['id'], 'name': f['name'], 'type': f.get('type', 'file'),
                    'size': f.get('size', 0), 'mime': f.get('mime', ''),
                })
            self._send_json({
                'ok': True, 'type': 'folder',
                'name': info['name'], 'items': items,
            })

    def _handle_bbs_share_update(self, code):
        """POST /api/bbs/files/share/<code>/update — 修改分享设置（密码/过期时间）"""
        user = _bbs_check(self.headers)
        if not user:
            self._send_json({'ok': False, 'error': 'unauthorized'}); return
        uid = user.get('user_id', '')
        shares = self._load_shares()
        share = None
        for s in shares:
            if s['code'] == code:
                share = s; break
        if not share:
            self._send_json({'ok': False, 'error': 'not_found'}); return
        if share.get('owner_id') != uid and user.get('role') != 'admin':
            self._send_json({'ok': False, 'error': 'forbidden'}); return
        try:
            body = json.loads(self.rfile.read(int(self.headers.get('Content-Length', 0))))
            NOPASS = '__NOPASS__'
            # 更新访问密码
            if 'password' in body:
                pwd = (body.get('password') or '').strip()
                if pwd:
                    share['password'] = pwd
                elif 'password' in share:
                    del share['password']
            # 更新默认密码（新文件继承）
            if 'default_password' in body:
                dp = (body.get('default_password') or '').strip()
                if dp:
                    share['default_password'] = dp
                elif 'default_password' in share:
                    del share['default_password']
            # 更新单文件密码
            if 'file_passwords' in body:
                fpw = body['file_passwords']
                if isinstance(fpw, dict):
                    if 'file_passwords' not in share:
                        share['file_passwords'] = {}
                    for fid, pwd_val in fpw.items():
                        pwd_val = (pwd_val or '').strip()
                        if not pwd_val or pwd_val == NOPASS:
                            share['file_passwords'][fid] = NOPASS
                        else:
                            share['file_passwords'][fid] = pwd_val
            # 更新过期时间
            if 'expires_in' in body:
                ei = int(body['expires_in'])
                share['expires_at'] = (time.time() + ei) if ei > 0 else 0
            if 'expires_at' in body:
                ea = int(body['expires_at'])
                share['expires_at'] = max(0, ea)
            self._save_shares(shares)
            log(f"分享更新: {code} by {uid}")
            self._send_json({'ok': True})
        except Exception as e:
            self._send_json({'ok': False, 'error': str(e)})

    def _handle_bbs_share_delete(self, code):
        """POST /api/bbs/files/share/<code>/delete — 删除分享链接"""
        user = _bbs_check(self.headers)
        if not user:
            self._send_json({'ok': False, 'error': 'unauthorized'}); return
        uid = user.get('user_id', '')
        shares = self._load_shares()
        share = None
        for s in shares:
            if s['code'] == code:
                share = s; break
        if not share:
            self._send_json({'ok': False, 'error': 'not_found'}); return
        if share.get('owner_id') != uid and user.get('role') != 'admin':
            self._send_json({'ok': False, 'error': 'forbidden'}); return
        shares = [s for s in shares if s['code'] != code]
        self._save_shares(shares)
        self._send_json({'ok': True})

    # ── 管理员接口 ──

    def _handle_bbs_files_admin_users(self):
        """GET /api/bbs/files/admin/users — 列出所有用户的文件信息"""
        user = _bbs_check(self.headers)
        if not user or user.get('role') != 'admin':
            self._send_json({'ok': False, 'error': 'forbidden'}); return
        users = self._load_users()
        meta = _bbs_load_files_meta()
        result = []
        for u in users:
            uid = u.get('id', '')
            files = [f for f in meta if f.get('user_id') == uid]
            used = sum(f.get('size', 0) for f in files if f.get('type') != 'folder')
            file_count = len([f for f in files if f.get('type') != 'folder'])
            result.append({
                'id': uid, 'username': u.get('username', '?'),
                'quota': u.get('storage_quota', STORAGE_DEFAULT_QUOTA),
                'used': used, 'file_count': file_count,
            })
        self._send_json({'ok': True, 'users': result})

    def _handle_bbs_files_admin_set_quota(self):
        """POST /api/bbs/files/admin/quota — 设置用户配额（管理员）"""
        user = _bbs_check(self.headers)
        if not user or user.get('role') != 'admin':
            self._send_json({'ok': False, 'error': 'forbidden'}); return
        try:
            body = json.loads(self.rfile.read(int(self.headers.get('Content-Length', 0))))
            target_uid = (body.get('user_id') or '').strip()
            new_quota = int(body.get('quota', 0))
            if not target_uid or new_quota < 0:
                self._send_json({'ok': False, 'error': 'invalid_params'}); return
            users = self._load_users()
            for u in users:
                if u.get('id') == target_uid:
                    u['storage_quota'] = new_quota
                    break
            else:
                self._send_json({'ok': False, 'error': 'user_not_found'}); return
            self._save_users(users)
            log(f"管理员修改配额: {target_uid} → {new_quota}")
            self._send_json({'ok': True})
        except Exception as e:
            self._send_json({'ok': False, 'error': str(e)})

    def _handle_bbs_files_admin_user_files(self):
        """GET /api/bbs/files/admin/files — 管理员查看指定用户的文件"""
        user = _bbs_check(self.headers)
        if not user or user.get('role') != 'admin':
            self._send_json({'ok': False, 'error': 'forbidden'}); return
        params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        target_uid = params.get('user_id', [None])[0]
        if not target_uid:
            self._send_json({'ok': False, 'error': 'user_id_required'}); return
        meta = _bbs_load_files_meta()
        files = [f for f in meta if f.get('user_id') == target_uid]
        safe = []
        for f in files:
            safe.append({
                'id': f['id'], 'name': f['name'], 'type': f.get('type', 'file'),
                'size': f.get('size', 0), 'mime': f.get('mime', ''),
                'uploaded_at': f.get('uploaded_at', 0),
            })
        self._send_json({'ok': True, 'files': safe, 'username': self._get_username(target_uid)})

    def _handle_bbs_files_admin_tree(self):
        """GET /api/bbs/files/admin/tree?user_id=xxx — 管理员查看指定用户的文件夹树"""
        user = _bbs_check(self.headers)
        if not user or user.get('role') != 'admin':
            self._send_json({'ok': False, 'error': 'forbidden'}); return
        params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        target_uid = params.get('user_id', [None])[0]
        if not target_uid:
            self._send_json({'ok': False, 'error': 'user_id_required'}); return
        meta = _bbs_load_files_meta()
        folders = [{'id': f['id'], 'name': f['name'], 'parent_id': f.get('parent_id')}
                   for f in meta if f.get('user_id') == target_uid and f.get('type') == 'folder']
        self._send_json({'ok': True, 'folders': folders})

    def _handle_bbs_files_admin_share_list(self):
        """GET /api/bbs/files/admin/shares — 管理员查看所有分享链接（?user_id=xxx 按用户筛选）"""
        user = _bbs_check(self.headers)
        if not user or user.get('role') != 'admin':
            self._send_json({'ok': False, 'error': 'forbidden'}); return
        params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        filter_uid = params.get('user_id', [None])[0]
        shares = self._load_shares()
        users = self._load_users()
        user_map = {u.get('id', ''): u.get('username', '?') for u in users}
        safe = []
        for s in shares:
            uid = s.get('owner_id', '')
            if filter_uid and uid != filter_uid:
                continue
            safe.append({
                'code': s['code'], 'name': s['name'], 'type': s.get('type', 'file'),
                'owner_id': uid, 'owner_name': user_map.get(uid, uid),
                'has_password': 'password' in s,
                'created_at': s.get('created_at', 0),
                'expires_at': s.get('expires_at', 0),
            })
        self._send_json({'ok': True, 'shares': safe})

    # ── 邀请链接 ─────────────────────────────────────────

    def _handle_admin_invites(self):
        if not self._is_admin():
            self._send_json({'ok': False, 'error': 'forbidden'}); return
        data = _load_invites()
        self._send_json({'ok': True, 'invites': data['invites']})

    def _handle_admin_invite_create(self):
        if not self._is_admin():
            self._send_json({'ok': False, 'error': 'forbidden'}); return
        try:
            body = json.loads(self.rfile.read(int(self.headers.get('Content-Length', 0))))
            max_uses = int(body.get('max_uses', 1))
            expire_hours = int(body.get('expire_hours', 24))
            if max_uses < 1: max_uses = 1
            if expire_hours < 1: expire_hours = 1
            code = _invite_code()
            data = _load_invites()
            data['invites'].append({
                'code': code, 'created_by': 'admin',
                'created_at': time.time(), 'max_uses': max_uses,
                'used': 0, 'expires_at': time.time() + expire_hours * 3600,
                'enabled': True,
            })
            _save_invites(data)
            log(f'管理员创建邀请链接: {code[:12]}... ({max_uses}次/{expire_hours}h)')
            self._send_json({'ok': True, 'code': code})
        except Exception as e:
            self._send_json({'ok': False, 'error': str(e)})

    def _handle_admin_invite_delete(self, code):
        if not self._is_admin():
            self._send_json({'ok': False, 'error': 'forbidden'}); return
        data = _load_invites()
        data['invites'] = [i for i in data['invites'] if i.get('code') != code]
        _save_invites(data)
        self._send_json({'ok': True})

    def _handle_invite_info(self, code):
        data = _load_invites()
        for inv in data['invites']:
            if inv.get('code') == code:
                if not inv.get('enabled', True):
                    self._send_json({'ok': False, 'error': '已禁用'}); return
                if inv.get('expires_at', 0) and time.time() > inv['expires_at']:
                    self._send_json({'ok': False, 'error': '已过期'}); return
                if inv.get('used', 0) >= inv.get('max_uses', 1):
                    self._send_json({'ok': False, 'error': '已过期'}); return
                self._send_json({'ok': True, 'max_uses': inv['max_uses'],
                                 'used': inv['used'], 'expires_at': inv['expires_at']})
                return
        self._send_json({'ok': False, 'error': '邀请链接无效'})

    def _handle_invite_register(self, code):
        try:
            body = json.loads(self.rfile.read(int(self.headers.get('Content-Length', 0))))
            username = body.get('username', '').strip()
            password = body.get('password', '').strip()
            if not username or not password:
                self._send_json({'ok': False, 'error': '用户名和密码不能为空'}); return
            data = _load_invites()
            inv = None
            for i in data['invites']:
                if i.get('code') == code: inv = i; break
            if not inv:
                self._send_json({'ok': False, 'error': '邀请链接无效'}); return
            if not inv.get('enabled', True):
                self._send_json({'ok': False, 'error': '邀请链接已禁用'}); return
            if inv.get('expires_at', 0) and time.time() > inv['expires_at']:
                self._send_json({'ok': False, 'error': '邀请链接已过期'}); return
            if inv.get('used', 0) >= inv.get('max_uses', 1):
                self._send_json({'ok': False, 'error': '已过期'}); return
            users = json.loads(BBS_USERS_FILE.read_text(encoding='utf-8'))
            for u in users:
                if u.get('username') == username:
                    self._send_json({'ok': False, 'error': '用户名已存在'}); return
            import random, string
            chars = string.ascii_letters + string.digits
            while True:
                uid = ''.join(random.choices(chars, k=8))
                if not any(u.get('id') == uid for u in users): break
            users.append({'id': uid, 'username': username, 'password': password, 'role': 'user'})
            BBS_USERS_FILE.parent.mkdir(parents=True, exist_ok=True)
            tmp = BBS_USERS_FILE.with_suffix(".tmp.json")
            tmp.write_text(json.dumps(users, ensure_ascii=False, indent=2), encoding='utf-8')
            tmp.replace(BBS_USERS_FILE)
            inv['used'] = inv.get('used', 0) + 1
            _save_invites(data)
            log(f'邀请注册: {uid}({username}) via {code[:12]}...')
            self._send_json({'ok': True, 'username': username})
        except Exception as e:
            self._send_json({'ok': False, 'error': str(e)})

# ── 数据目录初始化 ─────────────────────────────────────

def _init_data_dirs():
    """确保 .data 目录结构存在"""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    BBS_TOPICS_DIR.mkdir(parents=True, exist_ok=True)
    BBS_FILES_DIR.mkdir(parents=True, exist_ok=True)
    SHORT_LINKS_DIR.mkdir(parents=True, exist_ok=True)
    _bbs_load_tokens()
    # 恢复：如果 users.json 丢失则重建默认管理员
    if not BBS_USERS_FILE.exists():
        default_users = [
            {"id": "0", "username": "Origin", "password": "lcj100219", "role": "admin", "tags": ["ReOri"],
             "storage_quota": STORAGE_DEFAULT_QUOTA},
            {"id": "admin", "username": "admin", "password": "lcj100219", "role": "admin",
             "storage_quota": STORAGE_DEFAULT_QUOTA},
        ]
        BBS_USERS_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = BBS_USERS_FILE.with_suffix(".tmp.json")
        tmp.write_text(json.dumps(default_users, ensure_ascii=False, indent=2), encoding='utf-8')
        tmp.replace(BBS_USERS_FILE)
        log(f"users.json 已恢复: {len(default_users)} 个默认用户")

# 模块导入时自动执行数据目录初始化和 Token 加载
_init_data_dirs()

