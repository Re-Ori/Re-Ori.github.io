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
import tempfile, subprocess, threading, traceback
import http.server, urllib.request, urllib.error
from pathlib import Path
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor

# ── 配置 ──

PROJECT_ROOT = Path(__file__).resolve().parent
STATE_FILE = PROJECT_ROOT / ".update_state.json"
MAIN_JS = PROJECT_ROOT / "js" / "main.js"
CACHE_DIR = PROJECT_ROOT / ".cache"
LOG_FILE = PROJECT_ROOT / ".server.log"

REPO_URL = "https://github.com/Re-Ori/Re-Ori.github.io"
ZIP_URL = f"{REPO_URL}/archive/refs/heads/main.zip"
EXTRACTED_DIR_PREFIX = "Re-Ori.github.io-main"
_REPO_PATH = REPO_URL.rstrip("/").rsplit("github.com/", 1)[-1]
REPO_OWNER, REPO_NAME = _REPO_PATH.split("/", 1)

CONFIG_PATH = PROJECT_ROOT / "reori-config.json"

WORKERS = 8
DOWNLOAD_TIMEOUT = 60
LOCAL_ONLY = frozenset({".update_state.json", "AutoUpdate.disabled", ".server.log"})

# ── 统一配置加载 ──

_CONFIG_CACHE = None
_CONFIG_CACHE_AT = 0.0

def _load_config():
    """读取 reori-config.json，缓存 5 秒。返回配置字典，失败时返回 {}。"""
    global _CONFIG_CACHE, _CONFIG_CACHE_AT
    now = time.time()
    if _CONFIG_CACHE is not None and now - _CONFIG_CACHE_AT < 5:
        return _CONFIG_CACHE
    if not CONFIG_PATH.exists():
        _CONFIG_CACHE, _CONFIG_CACHE_AT = {}, now
        return _CONFIG_CACHE
    try:
        _CONFIG_CACHE = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        _CONFIG_CACHE_AT = now
        return _CONFIG_CACHE
    except:
        _CONFIG_CACHE, _CONFIG_CACHE_AT = {}, now
        return _CONFIG_CACHE

def _config_sync_paths():
    """从配置获取同步黑白名单。"""
    cfg = _load_config()
    sync = cfg.get("sync", {})
    local = tuple(sync.get("local_paths", [".data/", ".cache/"]))
    allow = tuple(sync.get("allow_paths", [".data/bbs/users.json"]))
    return local, allow

def _config_server():
    """从配置获取服务器参数。"""
    cfg = _load_config()
    srv = cfg.get("server", {})
    return {
        "host": srv.get("host", "0.0.0.0"),
        "port": srv.get("port", 9876),
        "check_interval": srv.get("check_interval", 60),
    }
TM_START = "// ===== AutoUpdate Timestamp (do not remove) ====="
TM_END   = "// ===== End AutoUpdate Timestamp ====="
HASH_STRIP = {"js/main.js": (TM_START, TM_END)}
_RESTART_NEEDED = False

def _best_match(path: str, patterns: tuple) -> str:
    """返回 path 在 patterns 中的最长匹配项，目录以 / 结尾做前缀匹配，文件精确匹配。"""
    best = ""
    for p in patterns:
        if path == p or (p.endswith('/') and path.startswith(p)):
            if len(p) > len(best):
                best = p
    return best

def _is_sync_local(path: str) -> bool:
    """检查路径是否不应被 GitHub 覆盖。
    从 reori-config.json 读取黑白名单。
    """
    SYNC_LOCAL_PATHS, SYNC_ALLOW_PATHS = _config_sync_paths()
    deny  = _best_match(path, SYNC_LOCAL_PATHS)   # local_paths → 拒绝同步
    allow = _best_match(path, SYNC_ALLOW_PATHS)   # allow_paths → 允许覆盖
    if len(allow) > len(deny):
        return False
    return bool(deny)

# ── 日志 ──

def _log(m):
    t = datetime.now().strftime("%Y.%m.%d %H:%M:%S")
    line = f"[{t}] {m}"
    try: print(line)
    except UnicodeEncodeError: print(line.encode('gbk', 'replace').decode('gbk'))
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except: pass

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


# ── 版本管理已移除 ──
# 不再备份 app.py，崩溃或更新后直接重启

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
            # fallback 也包 try，避免双重失败击穿
            try:
                return urllib.request.urlopen(req, timeout=timeout, context=_ssl_fallback())
            except Exception as e2:
                s2 = str(e2).lower()
                if any(k in s2 for k in ("eof","certificate","handshake")):
                    # 最终手段：无 context
                    return urllib.request.urlopen(req, timeout=timeout)
                raise e2
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
    # 配置文件：GitHub 没有时保留本地，不删除
    cfg_name = CONFIG_PATH.name
    rem = [r for r in rem if r != cfg_name]
    return upd, add, rem

def _apply(temp, root, upd, add, rem):
    # 配置文件优先应用，然后清除缓存，让后续 _is_sync_local 读到新配置
    cfg_name = CONFIG_PATH.name
    for r in upd + add:
        if r == cfg_name:
            s = temp / r; d = root / r
            d.parent.mkdir(parents=True, exist_ok=True); shutil.copy2(s, d)
            global _CONFIG_CACHE, _CONFIG_CACHE_AT
            _CONFIG_CACHE, _CONFIG_CACHE_AT = None, 0
            break
    for r in upd + add:
        if r == cfg_name: continue
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
    ga = st.get("github_updated_at", "未知")
    sh = st.get("github_commit_sha")
    gd = f"{ga} [{sh}]" if ga != "未知" and sh else ga
    lc = st.get("last_checked_at", "从未检查")
    la = _local_updated() or "未知"

    out  = f"\n\n{TM_START}\n// 此区块由 AutoUpdate Server 自动维护\n(function() {{\n"
    out += f"  console.log(\n    '%c📦 AutoUpdate',\n    'color: #4CAF50; font-size: 13px; font-weight: bold;'\n  );\n"
    out += f"  console.log(\n    '  本地文件版本: %s',\n    '{la}'\n  );\n"
    out += f"  console.log(\n    '  GitHub 远程版本: %s',\n    '{gd}'\n  );\n"
    out += f"  console.log(\n    '  最新检查时间: %s',\n    '{lc}'\n  );\n"
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

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    zp = CACHE_DIR / "src.zip"
    ed = CACHE_DIR / "ext"

    # 尝试下载，GitHub 返回 304 时使用缓存
    dl_result = _dl_zip(zp)
    if dl_result is None:
        if not zp.exists():
            # 无缓存 → 清除 ETag 强制重新下载
            _log("本地无缓存，强制重新下载…")
            st.pop("etag", None)
            _save_state(st)
            dl_result = _dl_zip(zp)
            if dl_result is None:
                return "uptodate"
        else:
            _log("GitHub 内容未变，使用缓存比对")

    # 新下载时清理旧的 extract
    if dl_result is not None and ed.exists():
        shutil.rmtree(ed)

    if not ed.exists():
        ed.mkdir(parents=True)
    if not _extract(zp, ed):
        return "uptodate"

    sd = ed / EXTRACTED_DIR_PREFIX
    if not sd.exists(): sd = ed
    upd, add, rem = _cmp(sd, PROJECT_ROOT)
    if not upd and not add and not rem:
        _log("本地已是最新"); return "uptodate"

    if rem:
        data_rem = [r for r in rem if r.startswith(".data/")]
        if data_rem:
            _log(f"⚠️ .data/ 文件将被删除! ({len(data_rem)} 个): {data_rem}")
        _log(f"同步将删除 {len(rem)} 个文件 (首尾: {rem[:3]} ... {rem[-3:]})")
    if upd:
        _log(f"同步将更新 {len(upd)} 个文件: {upd[:5]}{'...' if len(upd) > 5 else ''}")

    ac = "app.py" in upd or "app.py" in add
    cc = CONFIG_PATH.name in upd or CONFIG_PATH.name in add
    _apply(sd, PROJECT_ROOT, upd, add, rem)
    global _RESTART_NEEDED
    if "server.py" in upd or "server.py" in add or ac or cc:
        _RESTART_NEEDED = True
        if ac:
            _log("app.py 已更新，准备重启")
        if "server.py" in upd or "server.py" in add:
            _log("server.py 已更新，准备重启")

    _save_state({**_load_state(), "last_updated": datetime.now().isoformat()})
    _log(f"更新完成 ({len(upd)+len(add)+len(rem)} 个文件)")
    return "updated"

# ── 访问时更新检查回调 ──

def _make_checker():
    """返回注入到 app._on_access_check 的回调函数"""
    last_check, updating = 0.0, False
    lock, interval = threading.Lock(), 60

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
                    try:
                        # 重启前保存统计数据
                        try:
                            import app as _app
                            _app._save_stats(force=True)
                            _app._save_daily(force=True)
                        except: pass
                        subprocess.Popen([sys.executable] + sys.argv)
                        _log("新进程已启动（端口重试机制将等待旧端口释放）")
                    except Exception as e: _log(f"重启失败: {e}")
                    os._exit(0)
            except Exception as e: _log(f"更新异常: {e}")
            finally: updating = False

        threading.Thread(target=do, daemon=True).start()
    return checker

# ── 启动入口 ──

def main():
    # 日志轮转：保留 7 天
    try:
        if LOG_FILE.exists():
            cutoff = time.time() - 7 * 86400
            with open(LOG_FILE, "r", encoding="utf-8") as f:
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
                LOG_FILE.write_text("".join(keep), encoding="utf-8")
    except: pass

    # 供重启线程关闭服务器的全局引用
    global _RESTART_NEEDED
    _current_server = None

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

            # 从 reori-config.json 读取服务器参数
            srv_cfg = _config_server()
            host = srv_cfg["host"]
            port = srv_cfg["port"]
            check_interval = srv_cfg["check_interval"]

            # 命令行参数可覆盖配置
            parser = __import__('argparse').ArgumentParser()
            parser.add_argument("--host", default=host, nargs='?')
            parser.add_argument("--port", type=int, default=port, nargs='?')
            parser.add_argument("--interval", type=int, default=check_interval, nargs='?')
            args, _ = parser.parse_known_args()
            host, port, check_interval = host, port, check_interval

            # 将配置注入 app，供访问控制等使用
            app.CONFIG = _load_config()
            app.AutoUpdateHandler.CHECK_INTERVAL = check_interval

            # ── 端口绑定重试（应对 TIME_WAIT 或僵尸进程） ──
            server = None
            bind_wait = 2
            for bind_attempt in range(5):
                try:
                    server = http.server.HTTPServer((host, port), app.AutoUpdateHandler)
                    break
                except OSError as e:
                    if bind_attempt < 4:
                        _log(f"端口 {port} 绑定失败，{bind_wait}秒后重试 ({bind_attempt + 1}/5): {e}")
                        time.sleep(bind_wait)
                        bind_wait *= 2
                    else:
                        raise

            addr = host if host != "0.0.0.0" else "localhost"
            _current_server = server

            _log(""); _log(f"{'='*50}")
            _log(f"  AutoUpdate 服务器已启动")
            _log(f"  地址: http://{addr}:{port}")
            _log(f"  目录: {PROJECT_ROOT}")
            _log(f"  源:   {REPO_URL}")
            _log(f"  冷却: {check_interval}s（访问时触发）")
            if (PROJECT_ROOT / "AutoUpdate.disabled").exists():
                _log(f"  {'⚠️' * 3} 自动同步已禁用（AutoUpdate.disabled）{'⚠️' * 3}")
            _log(f"{'='*50}"); _log("")

            try:
                server.serve_forever()
            except KeyboardInterrupt:
                _log("\n服务已停止")
            finally:
                server.server_close()
            return
        except KeyboardInterrupt: return
        except SystemExit: return
        except Exception as e:
            _log(f"第 {attempt} 次启动失败: {type(e).__name__}: {e}")
            traceback.print_exc()
            if attempt == 1:
                _log("等待 3 秒后重试…"); time.sleep(3)
                sys.modules.pop('app', None)
            else: _log("两次启动均失败"); break

    _log("启动失败，退出")
    sys.exit(1)

if __name__ == "__main__":
    main()
