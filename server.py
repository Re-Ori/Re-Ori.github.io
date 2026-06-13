#!/usr/bin/env python3
"""
AutoUpdate Web Server — 更新与启动
====================================
负责：GitHub 同步、版本管理、崩溃回退、注入时间戳、启动 HTTP 服务。
实际的 HTTP 请求处理由 app.py 负责。

用法:
    python server.py                  # 默认端口 9876
    python server.py --port 8080      # 自定义端口
"""

from __future__ import annotations
import os, sys, json, time, socket, ssl, hashlib, shutil, zipfile
import tempfile, subprocess, threading, atexit, traceback
import http.server, urllib.request, urllib.error
from pathlib import Path
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor

# ── 配置 ──

PROJECT_ROOT = Path(__file__).resolve().parent
STATE_FILE = PROJECT_ROOT / ".update_state.json"
MAIN_JS = PROJECT_ROOT / "js" / "main.js"
CRASH_MARKER = PROJECT_ROOT / ".crash_marker.json"
VERSIONS_DIR = PROJECT_ROOT / ".versions"

REPO_URL = "https://github.com/Re-Ori/Re-Ori.github.io"
ZIP_URL = f"{REPO_URL}/archive/refs/heads/main.zip"
EXTRACTED_DIR_PREFIX = "Re-Ori.github.io-main"
_REPO_PATH = REPO_URL.rstrip("/").rsplit("github.com/", 1)[-1]
REPO_OWNER, REPO_NAME = _REPO_PATH.split("/", 1)

WORKERS = 8
MAX_VERSIONS = 4
DOWNLOAD_TIMEOUT = 60
LOCAL_ONLY = frozenset({".update_state.json", ".crash_marker.json", "AutoUpdate.disabled"})
# 同步黑名单：路径前缀匹配，长度越长优先级越高（范围越小优先级越大）
# 匹配的文件不会被 GitHub 同步覆盖
SYNC_LOCAL_PATHS = (
    ".data/bbs/users.json",
    ".data/bbs/topics.json",
    ".data/bbs/topics/",
    ".data/bbs/tokens.json",
    ".data/short_link/short_links.json",
)
TM_START = "// ===== AutoUpdate Timestamp (do not remove) ====="
TM_END   = "// ===== End AutoUpdate Timestamp ====="
HASH_STRIP = {"js/main.js": (TM_START, TM_END)}
_RESTART_NEEDED = False

def _is_sync_local(path: str) -> bool:
    """检查路径是否在同步黑名单中（不应被 GitHub 覆盖），使用最长前缀匹配。
    目录路径以 / 结尾做前缀匹配，文件路径做精确匹配。"""
    best = ""
    for p in SYNC_LOCAL_PATHS:
        if path == p:
            if len(p) > len(best):
                best = p
        elif p.endswith('/') and path.startswith(p):
            if len(p) > len(best):
                best = p
    return bool(best)

# ── 日志 ──

def _log(m):
    t = datetime.now().strftime("%H:%M:%S")
    try: print(f"[{t}] {m}")
    except UnicodeEncodeError: print(f"[{t}] {m.encode('gbk', 'replace').decode('gbk')}")

def _fmt(dt):
    return dt.strftime("%Y.%m.%d %H:%M:%S") + " [UTC+8]"

def _ua():
    return f"AutoUpdate/2.0 Python/{sys.version_info.major}.{sys.version_info.minor}"

def _sha256(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        while True:
            c = f.read(65536)
            if not c: break
            h.update(c)
    return h.hexdigest()

def _load_state():
    if STATE_FILE.exists():
        try: return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except: return {}
    return {}

def _save_state(s):
    STATE_FILE.write_text(json.dumps(s, indent=2, ensure_ascii=False), encoding="utf-8")

# ── 崩溃标记 ──

def _write_cm():
    CRASH_MARKER.write_text(json.dumps({"pid": os.getpid(), "status": "running",
        "started_at": _fmt(datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=8)),
    }), encoding="utf-8")

def _clear_cm():
    try:
        if CRASH_MARKER.exists(): CRASH_MARKER.unlink()
    except: pass

def _mark_clean():
    try:
        if CRASH_MARKER.exists():
            d = json.loads(CRASH_MARKER.read_text(encoding="utf-8"))
            d["status"] = "clean"
            CRASH_MARKER.write_text(json.dumps(d), encoding="utf-8")
    except: pass

def _prev_crash():
    if not CRASH_MARKER.exists(): return None
    try:
        d = json.loads(CRASH_MARKER.read_text(encoding="utf-8"))
        if d.get("status") == "running" and d.get("pid") != os.getpid(): return d
    except: pass
    return None

# ── 版本状态 ──

def _sv_state(st=None):
    if st is None: st = _load_state()
    return st.setdefault("service_version", {
        "version_counter": 0, "version_queue": [], "current_version_id": 0, "crash_info": None,
    })

def _save_sv(sv, st=None):
    if st is None: st = _load_state()
    st["service_version"] = sv; _save_state(st)

def _backup_app():
    ap = PROJECT_ROOT / "app.py"
    if not ap.exists(): return None
    st, sv = _load_state(), _sv_state(_load_state())
    vc, ci = sv.get("version_counter", 0), sv.get("current_version_id", 0)
    bid = ci if ci > 0 and ci not in sv.get("version_queue", []) else vc + 1
    VERSIONS_DIR.mkdir(parents=True, exist_ok=True)
    bp = VERSIONS_DIR / f"service.v{bid}.py"
    if bp.exists() and _sha256(ap) == _sha256(bp):
        _log(f"  app v{bid} 已备份，跳过"); return None
    shutil.copy2(ap, bp)
    q = sv.get("version_queue", [])
    if bid not in q: q.append(bid)
    if bid > vc: vc = bid
    sv.update(version_counter=vc, version_queue=q, current_version_id=bid)
    _save_sv(sv, st); _old_clean()
    _log(f"已备份 app v{bid}"); return bid

def _old_clean():
    st, sv = _load_state(), _sv_state(_load_state())
    q = sv.get("version_queue", [])
    if len(q) <= MAX_VERSIONS: return
    for vid in q[:-MAX_VERSIONS]:
        p = VERSIONS_DIR / f"service.v{vid}.py"
        if p.exists(): p.unlink()
    sv["version_queue"] = q[-MAX_VERSIONS:]
    _save_sv(sv, st)

def _rollback():
    st, sv = _load_state(), _sv_state(_load_state())
    q = sv.get("version_queue", [])
    if not q: _log("版本队列为空"); return None
    pid, pp = q[-1], VERSIONS_DIR / f"service.v{pid}.py"
    if not pp.exists(): _log(f"回退版本 v{pid} 不存在"); return None

    # 队列中只剩一个版本，当前 app.py 就是这个版本，不用覆盖直接重启
    if len(q) > 1:
        ap = PROJECT_ROOT / "app.py"
        if not ap.exists(): return None
        try:
            shutil.copy2(pp, ap)
        except Exception as e:
            _log(f"回退失败: {e}"); return None

    now = _fmt(datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=8))
    info = {"error": "程序异常退出", "crashed_version": sv.get("current_version_id", "?"),
            "recovered_to_version": pid, "recovered_at": now}
    sv.update(current_version_id=pid, crash_info=info)
    _save_sv(sv, st)
    _log(f"已回退至 app.py v{pid} ({now})")
    return info

# ── SSL ──

def _ssl_ctx():
    ctx = ssl.create_default_context()
    _set_opt(ctx, getattr(ssl, "OP_IGNORE_UNEXPECTED_EOF", 0))
    _set_opt(ctx, getattr(ssl, "OP_LEGACY_SERVER_CONNECT", 0))
    return ctx

def _ssl_fallback():
    ctx = ssl._create_unverified_context()
    _set_opt(ctx, getattr(ssl, "OP_IGNORE_UNEXPECTED_EOF", 0))
    _set_opt(ctx, getattr(ssl, "OP_LEGACY_SERVER_CONNECT", 0))
    return ctx

def _set_opt(ctx, o):
    if o:
        try: ctx.options |= o
        except: pass

def _urlopen(req, timeout):
    try: return urllib.request.urlopen(req, timeout=timeout, context=_ssl_ctx())
    except Exception as e:
        es = str(e).lower()
        if any(k in es for k in ("eof","certificate","handshake","remote end",
            "connection aborted","connection reset","connection refused","timed out","remote disconnected")):
            return urllib.request.urlopen(req, timeout=timeout, context=_ssl_fallback())
        raise

# ── GitHub API ──

def _gh_info():
    url = f"https://api.github.com/repos/{REPO_OWNER}/{REPO_NAME}/commits?sha=main&per_page=1"
    try:
        with _urlopen(urllib.request.Request(url, headers={"User-Agent": _ua(),
            "Accept": "application/vnd.github.v3+json"}), 15) as r:
            d = json.loads(r.read())
            if isinstance(d, list) and d:
                c = d[0]; sh = (c.get("sha") or "")[:7] or None
                cd = c.get("commit", {}).get("committer", {}).get("date")
                if cd:
                    dt = datetime.strptime(cd.replace("Z","").split("+")[0], "%Y-%m-%dT%H:%M:%S") + timedelta(hours=8)
                    return {"sha": sh, "updated_at": _fmt(dt)}
                if sh: return {"sha": sh, "updated_at": None}
            return None
    except: return None

# ── 下载 ──

def _dl_zip(path):
    hdrs = {"User-Agent": _ua()}
    st, et = _load_state(), _load_state().get("etag")
    if et: hdrs["If-None-Match"] = et
    _log("下载更新…")
    try:
        with _urlopen(urllib.request.Request(ZIP_URL, headers=hdrs), DOWNLOAD_TIMEOUT) as r:
            if r.status == 304: return None
            path.write_bytes(r.read())
            ne, lm = r.headers.get("ETag"), r.headers.get("Last-Modified")
            if ne: st["etag"] = ne
            if lm:
                try: st["github_updated_at"] = _fmt(datetime.strptime(lm, "%a, %d %b %Y %H:%M:%S %Z") + timedelta(hours=8))
                except: pass
            _save_state(st); return ne
    except urllib.error.HTTPError as e:
        if e.code == 304: return None
        _log(f"HTTP {e.code}")
    except Exception as e: _log(f"下载失败: {e}")
    return None

def _extract(zp, td):
    try:
        with zipfile.ZipFile(zp) as z:
            for m in z.namelist():
                p = m.split("/", 1)
                if len(p) < 2 or not p[1]: continue
                ep = td / p[1]
                if m.endswith("/"): ep.mkdir(parents=True, exist_ok=True)
                else:
                    ep.parent.mkdir(parents=True, exist_ok=True)
                    with z.open(m) as s, open(ep, "wb") as d: shutil.copyfileobj(s, d)
        return True
    except: return False

# ── 比对 ──

def _chash(p, ss=None, se=None):
    if ss is None: return _sha256(p)
    try:
        t = p.read_text("utf-8")
        if ss in t:
            si, ei = t.index(ss), t.index(se, si) + len(se)
            t = t[:si].rstrip() + t[ei:]
        return hashlib.sha256(t.encode()).hexdigest()
    except: return _sha256(p)

def _cmp(temp, root):
    upd, add, rem = [], [], []
    tf, lf = {}, {}
    for fp in temp.rglob("*"):
        if fp.is_file():
            r = fp.relative_to(temp)
            rp = r.as_posix()
            if r.name in LOCAL_ONLY or ".versions" in r.parts or _is_sync_local(rp): continue
            tf[r] = _sha256(fp)
    for fp in root.rglob("*"):
        if fp.is_file():
            r = fp.relative_to(root)
            rp = r.as_posix()
            if r.name in LOCAL_ONLY or ".versions" in r.parts or _is_sync_local(rp): continue
            m = HASH_STRIP.get(rp)
            lf[r] = _chash(fp, m[0], m[1]) if m else _sha256(fp)
    allk = set(tf) | set(lf)
    def cls(k):
        it, il = k in tf, k in lf
        if it and il: return ("updated" if tf[k] != lf[k] else "same", k)
        return ("added" if it else "removed", k)
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        for act, k in ex.map(cls, allk):
            s = k.as_posix()
            if act == "updated": upd.append(s)
            elif act == "added": add.append(s)
            elif act == "removed": rem.append(s)
    return upd, add, rem

def _apply(temp, root, upd, add, rem):
    for r in upd + add:
        s = temp / r; d = root / r
        d.parent.mkdir(parents=True, exist_ok=True); shutil.copy2(s, d)
    for r in rem:
        t = root / r
        if t.exists(): t.unlink()
    for r, _, _ in os.walk(root, topdown=False):
        if r != str(root):
            try: os.rmdir(r)
            except: pass

# ── 时间戳 ──

def _local_updated():
    try:
        l = 0.0
        for fp in PROJECT_ROOT.rglob("*"):
            if fp.is_file():
                r = fp.relative_to(PROJECT_ROOT)
                if r.name in LOCAL_ONLY or ".versions" in r.parts or ".git" in r.parts: continue
                l = max(l, fp.stat().st_mtime)
        if l == 0: return None
        return _fmt(datetime.fromtimestamp(l, timezone.utc).replace(tzinfo=None) + timedelta(hours=8))
    except: return None

def _inject_ts():
    st = _load_state()
    sv = _sv_state(st)
    ga = st.get("github_updated_at", "未知")
    sh = st.get("github_commit_sha")
    gd = f"{ga} [{sh}]" if ga != "未知" and sh else ga
    lc = st.get("last_checked_at", "从未检查")
    la = _local_updated() or "未知"
    ci = sv.get("crash_info")

    out  = f"\n\n{TM_START}\n// 此区块由 AutoUpdate Server 自动维护\n(function() {{\n"
    out += f"  console.log(\n    '%c📦 AutoUpdate',\n    'color: #4CAF50; font-size: 13px; font-weight: bold;'\n  );\n"
    out += f"  console.log(\n    '  本地文件版本: %s',\n    '{la}'\n  );\n"
    out += f"  console.log(\n    '  GitHub 远程版本: %s',\n    '{gd}'\n  );\n"
    out += f"  console.log(\n    '  最新检查时间: %s',\n    '{lc}'\n  );\n"

    if ci:
        err = ci.get("error", "未知错误").replace("\\", "\\\\").replace("'", "\\'")
        rv, ra = ci.get("recovered_to_version", "?"), ci.get("recovered_at", "未知")
        out += f"  console.log(\n    '%c⚠️ 版本异常回退',\n    'color: #FF5722; font-size: 13px; font-weight: bold;'\n  );\n"
        out += f"  console.log(\n    '  错误原因: %s',\n    '{err}'\n  );\n"
        out += f"  console.log(\n    '  已自动恢复至 %s 的版本 (v{rv})',\n    '{ra}'\n  );\n"
        sv["crash_info"] = None; _save_sv(sv, st)

    out += f"}})();\n{TM_END}\n"

    try:
        if not MAIN_JS.exists():
            MAIN_JS.parent.mkdir(parents=True, exist_ok=True)
            MAIN_JS.write_text("// AutoUpdate\n(function(){console.log('AutoUpdate 就绪');})();\n" + out, encoding="utf-8")
            _log("已创建 js/main.js"); return
        c = MAIN_JS.read_text(encoding="utf-8")
        if TM_START in c:
            si, ei = c.index(TM_START), c.index(TM_END) + len(TM_END)
            MAIN_JS.write_text(c[:si].rstrip() + out, encoding="utf-8")
        else: MAIN_JS.write_text(c.rstrip() + out, encoding="utf-8")
    except Exception as e: _log(f"时间戳注入失败: {e}")

# ── 更新流程 ──

def _run_update():
    st = _load_state()
    # ── 防护：存在 AutoUpdate.disabled 文件时不拉取代码 ──
    if (PROJECT_ROOT / "AutoUpdate.disabled").exists():
        _log("⚠️ AutoUpdate.disabled 存在，自动同步已禁用")
        return "disabled"
    with tempfile.TemporaryDirectory(prefix="au_") as td:
        td, zp = Path(td), Path(td) / "src.zip"
        if not _dl_zip(zp): return "uptodate"
        ed = td / "ext"; ed.mkdir()
        if not _extract(zp, ed): return "uptodate"
        sd = ed / EXTRACTED_DIR_PREFIX
        if not sd.exists(): sd = ed
        upd, add, rem = _cmp(sd, PROJECT_ROOT)
        if not upd and not add and not rem:
            _log("本地已是最新"); return "uptodate"

        ac = "app.py" in upd or "app.py" in add
        if ac:
            _log("检测到 app.py 更新，备份当前版本…")
            _backup_app()

        _apply(sd, PROJECT_ROOT, upd, add, rem)
        global _RESTART_NEEDED
        if "server.py" in upd or "server.py" in add: _RESTART_NEEDED = True
        if ac:
            sv, vc = _sv_state(_load_state()), _sv_state(_load_state()).get("version_counter", 0) + 1
            sv["current_version_id"] = sv["version_counter"] = vc
            _save_sv(sv); _RESTART_NEEDED = True

        _save_state({**_load_state(), "last_updated": datetime.now().isoformat()})
        _log(f"更新完成 ({len(upd)+len(add)+len(rem)} 个文件)")
        return "updated"

# ── 访问时更新检查回调 ──

def _make_checker():
    """返回注入到 app._on_access_check 的回调函数"""
    last_check, updating = 0.0, False
    lock, interval = threading.Lock(), 300

    def checker(cls):
        nonlocal last_check, updating
        now = time.time()
        with lock:
            if now - last_check < interval or updating: return
            last_check, updating = now, True

        def do():
            nonlocal updating
            try:
                _log("检查 GitHub 更新…")
                status = _run_update()
                st = _load_state()
                st["last_checked_at"] = _fmt(datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=8))
                gi = _gh_info()
                if gi:
                    if gi.get("updated_at"): st["github_updated_at"] = gi["updated_at"]
                    if gi.get("sha"): st["github_commit_sha"] = gi["sha"]
                _save_state(st); _inject_ts()
                if status == "disabled":
                    _log("⛔ 自动同步已禁用（存在 AutoUpdate.disabled 文件）")
                elif status == "uptodate":
                    _log("当前已是最新版本")
                elif status == "updated":
                    _log("更新完成，刷新浏览器即可生效")

                if _RESTART_NEEDED:
                    _log("\n服务器代码已更新，正在重启…"); time.sleep(3)
                    try: subprocess.Popen([sys.executable] + sys.argv)
                    except Exception as e: _log(f"重启失败: {e}")
                    try: CRASH_MARKER.write_text('{"status":"clean"}', encoding="utf-8")
                    except: pass
                    os._exit(0)
            except Exception as e: _log(f"更新异常: {e}")
            finally: updating = False

        threading.Thread(target=do, daemon=True).start()
    return checker

# ── 启动入口 ──

def main():
    crash = _prev_crash()
    if crash:
        _log("检测到上次异常退出")
        if crash.get("error"): _log(f"  错误: {crash['error']}")
        info = _rollback()
        _log(f"已恢复至 v{info['recovered_to_version']}" if info else "无可回退版本，使用当前 app.py")
    else:
        _log("上次运行正常退出")

    _clear_cm(); _write_cm(); atexit.register(_mark_clean)

    # ├─ 首次启动：如果 app.py 不存在，先下载 ──
    if not (PROJECT_ROOT / "app.py").exists():
        _log("app.py 不存在，首次从 GitHub 下载…")
        result = _run_update()
        if result == "updated":
            _log("下载完成")
        elif result == "disabled":
            _log("下载被 AutoUpdate.disabled 阻止，请删除该文件后重试")
            sys.exit(1)
        else:
            _log("下载失败，请检查网络连接")
            sys.exit(1)

    sys.path.insert(0, str(PROJECT_ROOT))
    for attempt in range(1, 3):
        try:
            import app, importlib
            if 'app' in sys.modules: importlib.reload(app)
            app._on_access_check = _make_checker()

            _inject_ts()
            parser = __import__('argparse').ArgumentParser()
            parser.add_argument("--host", default="0.0.0.0", nargs='?')
            parser.add_argument("--port", type=int, default=9876, nargs='?')
            parser.add_argument("--interval", type=int, default=300, nargs='?')
            args, _ = parser.parse_known_args()

            app.AutoUpdateHandler.CHECK_INTERVAL = args.interval
            server = http.server.HTTPServer((args.host, args.port), app.AutoUpdateHandler)
            addr = args.host if args.host != "0.0.0.0" else "localhost"

            _log(""); _log(f"{'='*50}")
            _log(f"  AutoUpdate 服务器已启动")
            _log(f"  地址: http://{addr}:{args.port}")
            _log(f"  目录: {PROJECT_ROOT}")
            _log(f"  源:   {REPO_URL}")
            _log(f"  冷却: {args.interval}s（访问时触发）")
            if (PROJECT_ROOT / "AutoUpdate.disabled").exists():
                _log(f"  {'⚠️' * 3} 自动同步已禁用（AutoUpdate.disabled）{'⚠️' * 3}")
            _log(f"{'='*50}"); _log("")

            try: server.serve_forever()
            except KeyboardInterrupt: _log("\n服务已停止"); server.server_close()
            return
        except KeyboardInterrupt: return
        except SystemExit: return
        except Exception as e:
            _log(f"第 {attempt} 次启动失败: {type(e).__name__}: {e}")
            traceback.print_exc()
            if attempt == 1:
                _log("尝试回退并重试…"); _rollback(); time.sleep(1)
                sys.modules.pop('app', None)
            else: _log("两次启动均失败"); break

    _log("启动失败，退出")
    try: CRASH_MARKER.write_text(json.dumps({"pid": os.getpid(), "status": "running", "error": "启动失败"}), encoding="utf-8")
    except: pass
    sys.exit(1)

if __name__ == "__main__":
    main()
