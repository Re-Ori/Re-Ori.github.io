#!/usr/bin/env python3
"""
AutoUpdate Web Server
=====================
纯 Python 标准库，零依赖。

访问网站时自动从 GitHub 拉取最新代码，5 分钟冷却。
在 main.js 末尾注入更新时间戳，F12 控制台可见。

用法:
    python server.py                  # 默认端口 9876
    python server.py --port 8080      # 自定义端口
"""

from __future__ import annotations

import os
import sys
import json
import time
import socket
import ssl
import hashlib
import shutil
import zipfile
import tempfile
import subprocess
import threading
import http.server
import urllib.parse
import urllib.request
import urllib.error
from pathlib import Path
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor

# ── 配置 ─────────────────────────────────────────────────

REPO_URL = "https://github.com/Re-Ori/Re-Ori.github.io"
ZIP_URL = f"{REPO_URL}/archive/refs/heads/main.zip"
EXTRACTED_DIR_PREFIX = "Re-Ori.github.io-main"

# 从 REPO_URL 提取 owner/name，供 GitHub API 使用
_REPO_PATH = REPO_URL.rstrip("/").rsplit("github.com/", 1)[-1]
REPO_OWNER, REPO_NAME = _REPO_PATH.split("/", 1)

PROJECT_ROOT = Path(__file__).resolve().parent
STATE_FILE = PROJECT_ROOT / ".update_state.json"
WHITELIST_FILE = PROJECT_ROOT / "whitelist.json"
MAIN_JS = PROJECT_ROOT / "js" / "main.js"

WORKERS = 8

# ── ACME HTTP-01 挑战（SSL 证书申请） ─────────────────────
# btPanel 等工具申请 SSL 证书时，会把挑战文件写入此目录。
# 服务器需要在多个可能的 web root 下查找并提供文件。
ACME_CHALLENGE_ROOTS: list[Path] = [
    PROJECT_ROOT,
]

# ── Giscus 代理目标 ────────────────────────────────────────
GISCUS_ORIGIN = "https://giscus.app"
GITHUB_API_ORIGIN = "https://api.github.com"

# ── P2P 信令存储（内存） ──────────────────────────────────
_p2p_signals: dict[str, list[dict]] = {}
_p2p_signals_lock = threading.RLock()
_p2p_rooms: dict[str, dict] = {}
# {
#   "room": {
#       "type": "websrc" | "server",
#       "password": "",            # 空字符串或无此字段表示无密码
#       "created_at": float,
#       "creator": str,
#       "peers": { "peer_id": {"name": str, "last_seen": float, "joined_at": float} }
#   }
# }

# ── P2P 中转数据存储 ──────────────────────────────────────
_p2p_relay_buffers: dict[str, list[dict]] = {}
_p2p_relay_lock = threading.RLock()
_p2p_relay_usage: dict[str, list[float]] = {}
RELAY_RATE_LIMIT = 5 * 1024 * 1024       # 5 MB 每 5 分钟
RELAY_RATE_WINDOW = 300                   # 300 秒 = 5 分钟
RELAY_MAX_BUFFER = 200
RELAY_FILE_MAX_SIZE = 5 * 1024 * 1024     # 单文件最大 5MB

# ── 日志 ─────────────────────────────────────────────────

def log(msg: str):
    ts = datetime.now().strftime("%H:%M:%S")
    try:
        print(f"[{ts}] {msg}")
    except UnicodeEncodeError:
        # Windows GBK 回退：只替换无法编码的字符（通常是 emoji）
        print(f"[{ts}] {msg.encode('gbk', 'replace').decode('gbk')}")

# ── 状态管理 ─────────────────────────────────────────────

def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}

def save_state(state: dict):
    STATE_FILE.write_text(
        json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8"
    )

# ── 白名单 ───────────────────────────────────────────────

def load_whitelist() -> list[str] | None:
    """
    加载访问白名单。

    返回 ``None`` 表示白名单文件不存在或无法解析——拒绝所有请求。
    返回 ``[]`` 表示白名单文件存在但为空——也拒绝所有请求。
    返回路径列表：
      - 以 ``/`` 结尾的条目为目录前缀（path.startswith(entry)）
      - 否则为精确匹配
    """
    if not WHITELIST_FILE.exists():
        return None
    try:
        data = json.loads(WHITELIST_FILE.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return [str(p).replace("\\", "/") for p in data]
        return []
    except Exception:
        log(f"⚠️ 白名单文件解析失败，将拒绝所有请求")
        return None


def is_path_allowed(request_path: str, whitelist: list[str] | None) -> bool:
    """
    检查请求路径是否在白名单内。

    - whitelist 为 ``None`` —— 白名单文件不存在，拒绝所有请求
    - whitelist 为 ``[]`` —— 白名单为空，也拒绝所有请求
    - 否则按条目匹配
    """
    if whitelist is None:
        return False   # 文件不存在，拒绝所有
    if not whitelist:
        return False   # 存在但为空，拒绝所有
    for entry in whitelist:
        if entry == "/":
            # "/" 仅精确匹配根路径，不作为前缀（否则会放行所有路径）
            if request_path == "/":
                return True
        elif entry.endswith("/"):
            # 以 "/" 结尾的条目作为目录前缀匹配
            if request_path.startswith(entry):
                return True
        else:
            # 否则精确匹配文件名
            if request_path == entry:
                return True
    return False


# ── 工具 ─────────────────────────────────────────────────

def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(65536)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()

def user_agent() -> str:
    return f"AutoUpdate/2.0 Python/{sys.version_info.major}.{sys.version_info.minor}"


def format_utc8(dt: datetime) -> str:
    """格式化为 ``2026.06.01 22:53:24 [UTC+8]`` 格式。"""
    return dt.strftime("%Y.%m.%d %H:%M:%S") + " [UTC+8]"


def fetch_github_repo_info() -> dict | None:
    """
    调用 GitHub Commits API 获取最新 commit 的 SHA 和时间。

    返回 ``{"sha": "e885e80", "updated_at": "2026.06.03 21:47:25 [UTC+8]"}``，
    失败返回 ``None``。
    """
    api_url = (
        f"https://api.github.com/repos/{REPO_OWNER}/{REPO_NAME}"
        f"/commits?sha=main&per_page=1"
    )
    req = urllib.request.Request(
        api_url,
        headers={
            "User-Agent": user_agent(),
            "Accept": "application/vnd.github.v3+json",
        },
    )
    try:
        with _ssl_urlopen(req, 15) as resp:
            data = json.loads(resp.read())
            if isinstance(data, list) and data:
                commit = data[0]
                sha_full = commit.get("sha", "") or ""
                sha_short = sha_full[:7] if sha_full else None
                commit_date = (
                    commit.get("commit", {})
                    .get("committer", {})
                    .get("date")
                )
                if commit_date:
                    dt = datetime.strptime(
                        commit_date.replace("Z", "").split("+")[0],
                        "%Y-%m-%dT%H:%M:%S",
                    )
                    dt_utc8 = dt + timedelta(hours=8)
                    return {
                        "sha": sha_short,
                        "updated_at": format_utc8(dt_utc8),
                    }
                if sha_short:
                    return {"sha": sha_short, "updated_at": None}
            return None
    except Exception:
        return None

# ── 下载 GitHub ZIP ─────────────────────────────────────

DOWNLOAD_TIMEOUT = 60   # 秒（GitHub CDN 可能较慢）
DOWNLOAD_RETRIES = 3    # 网络不稳定时重试次数
RETRY_BACKOFF = [1, 3, 8]  # 每次重试前等待秒数（索引 0=第一次重试前）


def _make_ssl_context() -> ssl.SSLContext:
    """创建兼容的 SSL 上下文，处理 Windows / CDN 边缘节点 TLS 问题。

    GitHub CDN（codeload.github.com）有时会在 TLS 握手阶段提前断开，
    标准 ``ssl.create_default_context()`` 对此十分严格。这里：
    1. 先用标准上下文尝试
    2. 若失败则降级为未验证上下文 + 宽松选项
    """
    ctx = ssl.create_default_context()
    _set_ctx_option(ctx, getattr(ssl, "OP_IGNORE_UNEXPECTED_EOF", 0))
    _set_ctx_option(ctx, getattr(ssl, "OP_LEGACY_SERVER_CONNECT", 0))
    return ctx


def _make_fallback_ssl_context() -> ssl.SSLContext:
    """降级上下文：不验证证书 + 宽松选项。"""
    ctx = ssl._create_unverified_context()
    _set_ctx_option(ctx, getattr(ssl, "OP_IGNORE_UNEXPECTED_EOF", 0))
    _set_ctx_option(ctx, getattr(ssl, "OP_LEGACY_SERVER_CONNECT", 0))
    return ctx


def _set_ctx_option(ctx: ssl.SSLContext, opt: int):
    """安全地设置 SSL Context option（忽略不存在的选项）。"""
    if opt:
        try:
            ctx.options |= opt
        except (ValueError, TypeError):
            pass


def _ssl_urlopen(req: urllib.request.Request, timeout: int):
    """带 SSL 兼容处理的 urlopen — 自动降级。

    先用标准 SSL 上下文连接，如果遇到证书/握手/CDN 断连错误则
    降级到未验证上下文重试一次（丢弃已消耗的响应体）。
    """
    try:
        return urllib.request.urlopen(req, timeout=timeout,
                                       context=_make_ssl_context())
    except Exception as e:
        err_str = str(e).lower()
        # 只有和网络/SSL/TLS 相关的错误才触发降级
        keywords = ("eof", "certificate", "handshake", "remote end",
                    "connection aborted", "connection reset",
                    "connection refused", "timed out",
                    "remote disconnected")
        if any(kw in err_str for kw in keywords):
            return urllib.request.urlopen(req, timeout=timeout,
                                           context=_make_fallback_ssl_context())
        raise


def _download_via_curl(url: str, target_path: Path, etag: str | None = None) -> tuple[str | None, str | None] | None:
    """使用 ``curl.exe`` 下载（兜底方案 — 绕过 Python SSL 栈）。

    curl 使用 Windows 自带的 Schannel SSL 库，与 GitHub CDN 的兼容性更好。
    返回新的 ETag，失败返回 None。
    """
    curl = shutil.which("curl") or shutil.which("curl.exe")
    if not curl:
        log("curl 不可用，跳过兜底下载")
        return None

    header_file = target_path.with_name(target_path.name + ".headers")
    CURL_TIMEOUT = 180  # 秒 — 实测 GitHub CDN 有时需要 110s
    cmd = [
        curl, "-sSL",
        "-k",                          # 跳过证书验证（WinSSL 证书存储问题）
        "--ssl-no-revoke",             # Windows 上避免 OCSP 吊销检查超时
        "-o", str(target_path),
        "-D", str(header_file),        # 把响应头写入单独文件
        "--connect-timeout", "30",
        "--max-time", str(CURL_TIMEOUT),
    ]
    if etag:
        cmd.extend(["-H", f"If-None-Match: {etag}"])
    cmd.append(url)

    try:
        log(f"尝试 curl 下载…（最长 {CURL_TIMEOUT}s）")
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=CURL_TIMEOUT + 15
        )
        if proc.returncode == 0:
            size = target_path.stat().st_size if target_path.exists() else 0
            if size > 0:
                # 从响应头提取 ETag 和 Last-Modified
                new_etag = etag
                last_modified = None
                if header_file.exists():
                    hdr_text = header_file.read_text(encoding="utf-8", errors="replace")
                    for line in hdr_text.splitlines():
                        lower = line.lower()
                        if lower.startswith("etag:"):
                            new_etag = line.split(":", 1)[1].strip()
                        elif lower.startswith("last-modified:"):
                            last_modified = line.split(":", 1)[1].strip()
                    header_file.unlink(missing_ok=True)
                log(f"curl 下载完成 ({size / 1024:.1f} KB)")
                return new_etag, last_modified
            else:
                log("curl 下载的文件为空")
                return None
        elif proc.returncode == 22:  # HTTP 4xx/5xx
            out = (proc.stdout + proc.stderr).lower()
            if "304" in out:
                log("服务端文件未变动（curl 返回 304）。")
                return None
            log(f"curl HTTP 错误（returncode={proc.returncode}）")
            return None
        else:
            log(f"curl 下载失败（exit={proc.returncode}）")
            return None
    except subprocess.TimeoutExpired:
        log(f"curl 下载超时（{CURL_TIMEOUT}s）")
        return None
    except Exception as e:
        log(f"curl 异常: {e}")
        return None
    finally:
        if header_file.exists():
            header_file.unlink(missing_ok=True)


def download_zip(target_path: Path) -> str | None:
    """
    下载仓库 ZIP 到本地。
    返回 ETag 字符串表示下载成功，返回 None 表示无更新或出错。
    """
    headers = {"User-Agent": user_agent()}

    state = load_state()
    etag = state.get("etag")
    if etag:
        headers["If-None-Match"] = etag

    log("正在从 GitHub 下载更新…")
    req = urllib.request.Request(ZIP_URL, headers=headers)

    last_err = None

    # ── 第 1 步：Python urllib（快速尝试，30s 超时） ──
    # Windows 上的 Python SSL 栈与 GitHub CDN 兼容性不好，
    # 尝试一次不行就立刻交给 curl，不浪费多次重试。
    try:
        log("尝试 Python urllib 下载…")
        with _ssl_urlopen(req, DOWNLOAD_TIMEOUT) as resp:
            if resp.status == 304:
                log("服务端文件未变动，无需更新。")
                return None
            content = resp.read()
            target_path.write_bytes(content)
            log(f"urllib 下载完成 ({len(content) / 1024:.1f} KB)")
            new_etag = resp.headers.get("ETag")
            if new_etag:
                state["etag"] = new_etag
            # 从下载响应头直接获取时间，不依赖 GitHub API
            last_modified = resp.headers.get("Last-Modified")
            if last_modified:
                try:
                    dt = datetime.strptime(
                        last_modified, "%a, %d %b %Y %H:%M:%S %Z"
                    )
                    state["github_updated_at"] = format_utc8(dt + timedelta(hours=8))
                except (ValueError, TypeError):
                    pass
            save_state(state)
            return new_etag
    except urllib.error.HTTPError as e:
        if e.code == 304:
            log("服务端文件未变动，无需更新。")
            return None
        log(f"urllib HTTP 错误: {e.code}")
        last_err = f"HTTP {e.code}"
    except urllib.error.URLError as e:
        msg = str(e.reason)
        log(f"urllib 网络错误: {msg}")
        last_err = msg
    except socket.timeout:
        log(f"urllib 超时（{DOWNLOAD_TIMEOUT}s）")
        last_err = "timeout"
    except Exception as e:
        ern = str(e)
        log(f"urllib 失败: {ern}")
        last_err = ern

    # ── 第 2 步：curl 兜底 ──
    log(f"urllib 失败（{last_err}），尝试 curl 兜底…")
    etag_fallback = load_state().get("etag")
    curl_result = _download_via_curl(ZIP_URL, target_path, etag_fallback)
    if curl_result:
        curl_etag, curl_last_modified = curl_result
        state = load_state()
        state["etag"] = curl_etag
        if curl_last_modified:
            try:
                dt = datetime.strptime(
                    curl_last_modified, "%a, %d %b %Y %H:%M:%S %Z"
                )
                state["github_updated_at"] = format_utc8(dt + timedelta(hours=8))
            except (ValueError, TypeError):
                pass
        save_state(state)
        return curl_etag

    log(f"下载最终失败: {last_err}")
    return None

# ── 解压 ─────────────────────────────────────────────────

def extract_zip(zip_path: Path, target_dir: Path) -> bool:
    log("正在解压…")
    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            for member in zf.namelist():
                parts = member.split("/", 1)
                if len(parts) < 2 or not parts[1]:
                    continue
                extracted_path = target_dir / parts[1]
                if member.endswith("/"):
                    extracted_path.mkdir(parents=True, exist_ok=True)
                else:
                    extracted_path.parent.mkdir(parents=True, exist_ok=True)
                    with zf.open(member) as src, open(extracted_path, "wb") as dst:
                        shutil.copyfileobj(src, dst)
        return True
    except zipfile.BadZipFile:
        log("错误: 下载的文件不是有效的 ZIP 压缩包。")
        return False
    except Exception as e:
        log(f"解压失败: {e}")
        return False

# ── 注入更新时间戳到 main.js 所用的标记常量 ──────────────
# （放在文件比对之前定义，因为比对逻辑也会引用）

TIMESTAMP_MARKER_START = "// ===== AutoUpdate Timestamp (do not remove) ====="
TIMESTAMP_MARKER_END   = "// ===== End AutoUpdate Timestamp ====="

# ── 文件比对与更新 ───────────────────────────────────────

# 不被远程仓库管理的本地文件（不参与比对、不被删除）
LOCAL_ONLY_FILES = frozenset({
    ".update_state.json",
})
# 被远程管理、但本地可能被注入额外内容的文件 — 计算哈希前先剥离受控区块
HASH_STRIP_MARKERS: dict[str, tuple[str, str]] = {
    "js/main.js": (TIMESTAMP_MARKER_START, TIMESTAMP_MARKER_END),
}


def _content_hash(path: Path, strip_start: str | None = None,
                  strip_end: str | None = None) -> str:
    """计算文件 SHA256，可选地剥离指定区块后再计算。"""
    if strip_start is None:
        return file_sha256(path)

    try:
        raw = path.read_bytes()
        text = raw.decode("utf-8")
        if strip_start in text:
            si = text.index(strip_start)
            ei = text.index(strip_end, si) + len(strip_end)
            text = text[:si].rstrip() + text[ei:]
        return hashlib.sha256(text.encode("utf-8")).hexdigest()
    except Exception:
        return file_sha256(path)


def compare_and_update(
    temp_dir: Path, project_root: Path
) -> tuple[list[str], list[str], list[str]]:
    updated: list[str] = []
    added: list[str] = []
    removed: list[str] = []

    temp_files: dict[Path, str] = {}
    local_files: dict[Path, str] = {}

    log("正在计算文件哈希…")

    for fpath in temp_dir.rglob("*"):
        if fpath.is_file():
            rel = fpath.relative_to(temp_dir)
            temp_files[rel] = file_sha256(fpath)

    for fpath in project_root.rglob("*"):
        if fpath.is_file():
            rel = fpath.relative_to(project_root)
            # 跳过本地自有文件
            if rel.name in LOCAL_ONLY_FILES:
                continue
            # 计算哈希时自动剥离本地注入的区块
            markers = HASH_STRIP_MARKERS.get(str(rel.as_posix()))
            ss, se = markers if markers else (None, None)
            local_files[rel] = _content_hash(fpath, ss, se)

    all_keys = set(temp_files.keys()) | set(local_files.keys())

    def classify(key: Path):
        in_temp = key in temp_files
        in_local = key in local_files
        if in_temp and in_local:
            if temp_files[key] != local_files[key]:
                return ("updated", key)
            return ("same", key)
        elif in_temp and not in_local:
            return ("added", key)
        else:
            return ("removed", key)

    with ThreadPoolExecutor(max_workers=WORKERS) as executor:
        results = list(executor.map(classify, all_keys))

    for action, key in results:
        if action == "updated":
            updated.append(str(key.as_posix()))
        elif action == "added":
            added.append(str(key.as_posix()))
        elif action == "removed":
            removed.append(str(key.as_posix()))

    return updated, added, removed


def apply_update(
    temp_dir: Path, project_root: Path,
    updated: list[str], added: list[str], removed: list[str],
):
    log("正在应用更新…")
    for rel_str in updated + added:
        rel = Path(rel_str)
        src = temp_dir / rel
        dst = project_root / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        log(f"  {'更新' if rel_str in updated else '新增'}  {rel_str}")

    for rel_str in removed:
        target = project_root / rel_str
        if target.exists():
            target.unlink()
            log(f"  删除  {rel_str}")

    # 清理空目录
    cleaned = 0
    for root, dirs, files in os.walk(project_root, topdown=False):
        if root == str(project_root):
            continue
        try:
            os.rmdir(root)
            cleaned += 1
        except OSError:
            pass
    if cleaned:
        log(f"  清理了 {cleaned} 个空目录")

# ── 本地文件变更检测 ─────────────────────────────────────

def get_local_updated_at() -> str | None:
    """
    扫描项目目录，取所有被跟踪文件（非 LOCAL_ONLY_FILES）的最新修改时间。
    返回格式化的 UTC+8 字符串，失败返回 None。
    """
    try:
        latest: float = 0.0
        for fpath in PROJECT_ROOT.rglob("*"):
            if fpath.is_file():
                rel = fpath.relative_to(PROJECT_ROOT)
                if rel.name in LOCAL_ONLY_FILES:
                    continue
                # 跳过 .update_state.json 和 git 相关
                if rel.name == ".update_state.json" or ".git" in rel.parts:
                    continue
                mtime = fpath.stat().st_mtime
                if mtime > latest:
                    latest = mtime
        if latest == 0.0:
            return None
        dt = datetime.fromtimestamp(latest, timezone.utc).replace(tzinfo=None) + timedelta(hours=8)
        return format_utc8(dt)
    except Exception:
        return None


# ── 注入更新时间戳到 main.js ─────────────────────────────

def inject_timestamp():
    """在 main.js 末尾注入/刷新更新时间戳（F12 控制台输出）。"""
    state = load_state()
    github_updated_at = state.get("github_updated_at", "未知")
    github_commit_sha = state.get("github_commit_sha")
    if github_updated_at != "未知" and github_commit_sha:
        github_display = f"{github_updated_at} [{github_commit_sha}]"
    else:
        github_display = github_updated_at
    last_checked_at = state.get("last_checked_at", "从未检查")
    local_updated_at = get_local_updated_at() or "未知"

    block = (
        f"\n\n{TIMESTAMP_MARKER_START}\n"
        f"// 此区块由 AutoUpdate Server 自动维护\n"
        f"(function() {{\n"
        f"  console.log(\n"
        f"    '%c📦 AutoUpdate',\n"
        f"    'color: #4CAF50; font-size: 13px; font-weight: bold;'\n"
        f"  );\n"
        f"  console.log(\n"
        f"    '  本地文件版本: %s',\n"
        f"    '{local_updated_at}'\n"
        f"  );\n"
        f"  console.log(\n"
        f"    '  GitHub 远程版本: %s',\n"
        f"    '{github_display}'\n"
        f"  );\n"
        f"  console.log(\n"
        f"    '  最新检查时间: %s',\n"
        f"    '{last_checked_at}'\n"
        f"  );\n"
        f"}})();\n"
        f"{TIMESTAMP_MARKER_END}\n"
    )

    try:
        if not MAIN_JS.exists():
            # 如果 main.js 还不存在，先创建基础框架
            MAIN_JS.parent.mkdir(parents=True, exist_ok=True)
            base = (
                "// AutoUpdate — timestamp placeholder\n"
                "(function() {\n"
                '  console.log("AutoUpdate 就绪");\n'
                "})();\n"
            )
            MAIN_JS.write_text(base + block, encoding="utf-8")
            log(f"🕐 已创建 js/main.js 并注入时间戳")
            return

        content = MAIN_JS.read_text(encoding="utf-8")

        if TIMESTAMP_MARKER_START in content:
            # 替换已有区块
            start = content.index(TIMESTAMP_MARKER_START)
            end = content.index(TIMESTAMP_MARKER_END) + len(TIMESTAMP_MARKER_END)
            new = content[:start].rstrip() + block
            MAIN_JS.write_text(new, encoding="utf-8")
        else:
            # 首次追加
            MAIN_JS.write_text(content.rstrip() + block, encoding="utf-8")

        log(f"🕐 时间戳信息已{'刷新' if TIMESTAMP_MARKER_START in content else '注入'}")
    except Exception as e:
        log(f"⚠️ 注入更新时间戳失败: {e}")

# ── 主更新流程 ───────────────────────────────────────────

REMOTE_EXCLUDE = {".update_state.json", "server.py", "updater.py", ".claude"}
_RESTART_NEEDED: bool = False  # run_update 检测到 server.py 自身有变更时置 True

def run_update() -> bool:
    """
    执行一次更新：下载 → 解压 → 比对 → 应用。
    返回 True 表示有文件变更，False 表示无更新。
    """
    state = load_state()

    with tempfile.TemporaryDirectory(prefix="autoupdate_") as tmp_str:
        tmp_dir = Path(tmp_str)
        zip_path = tmp_dir / "source.zip"

        has_update = download_zip(zip_path)
        if not has_update:
            return False

        extract_dir = tmp_dir / "extracted"
        extract_dir.mkdir(parents=True)
        if not extract_zip(zip_path, extract_dir):
            return False

        source_dir = extract_dir / EXTRACTED_DIR_PREFIX
        if not source_dir.exists():
            source_dir = extract_dir

        updated, added, removed = compare_and_update(source_dir, PROJECT_ROOT)

        log(f"\n差异报告:")
        log(f"  - 更新: {len(updated)} 个文件")
        log(f"  - 新增: {len(added)} 个文件")
        log(f"  - 删除: {len(removed)} 个文件")

        if not updated and not added and not removed:
            log("本地文件与远程一致。")
            return False

        apply_update(source_dir, PROJECT_ROOT, updated, added, removed)

        global _RESTART_NEEDED
        if "server.py" in updated or "server.py" in added:
            _RESTART_NEEDED = True

        state["last_updated"] = datetime.now().isoformat()
        save_state(state)

        log(f"\n✅ 更新完成！共处理 {len(updated) + len(added) + len(removed)} 个文件。")
        return True


# ── HTTP 服务器 ──────────────────────────────────────────

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
        stale = [pid for pid, info in list(peers_dict.items())
                 if now - info.get("last_seen", 0) > self.STALE_PEER_TIMEOUT]
        for pid in stale:
            peers_dict.pop(pid, None)
            self._store_signal(room, pid, '*', 'peer_leave', {'peer': pid})
        if not peers_dict:
            del _p2p_rooms[room]
            _p2p_signals.pop(room, None)

    def _handle_p2p_join(self):
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
        relay_remaining = self._get_relay_remaining(peer)
        self._send_json({'signals': signals, 'relay': relay_msgs, 'relay_remaining': relay_remaining})

    def _handle_p2p_room_info(self):
        """查询房间信息（类型、用户数、用户列表），无需加入房间。"""
        params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        room = params.get('room', [''])[0]
        if not room:
            self._send_json({'exists': False})
            return
        with _p2p_signals_lock:
            if room in _p2p_rooms:
                peers_dict = _p2p_rooms[room].get("peers", {})
                info = {
                    'exists': True,
                    'type': _p2p_rooms[room].get('type', 'websrc'),
                    'has_password': bool(_p2p_rooms[room].get('password', '')),
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

            # 速率检查
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
                # 限制缓冲区大小，防止内存泄漏
                if len(_p2p_relay_buffers[room]) > RELAY_MAX_BUFFER:
                    _p2p_relay_buffers[room] = _p2p_relay_buffers[room][-RELAY_MAX_BUFFER:]

            self._send_json({'ok': True})
        except Exception as e:
            log(f"Relay send error: {e}")
            self.send_error(400, "Bad request")

    def _get_relay_remaining(self, peer_id: str) -> int:
        """返回该 peer 在当前窗口内的剩余可用字节数。"""
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
                log("检查 GitHub 更新…")
                changed = run_update()

                # 无论是否有更新，都记录本次检查时间
                state = load_state()
                state["last_checked_at"] = format_utc8(
                    datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=8)
                )

                # 每次检查都从 GitHub API 获取最新 commit SHA 和时间，
                # 即使 ZIP 内容没变（304），用户也可能刚推送了新 commit
                gh_info = fetch_github_repo_info()
                if gh_info:
                    if gh_info["updated_at"]:
                        state["github_updated_at"] = gh_info["updated_at"]
                    if gh_info["sha"]:
                        state["github_commit_sha"] = gh_info["sha"]

                save_state(state)

                inject_timestamp()
                if changed:
                    log("更新完成，刷新浏览器即可生效")
                else:
                    log("当前已是最新版本")

                # server.py 自身在更新中被覆盖 → 重启进程
                if _RESTART_NEEDED:
                    log("\n🔄 server.py 已更新，正在重启…")
                    time.sleep(3)
                    try:
                        subprocess.Popen([sys.executable] + sys.argv)
                    except Exception as e:
                        log(f"重启失败: {e}")
                    os._exit(0)
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
    log(f"  源:   {REPO_URL}")
    log(f"  冷却: {args.interval}s（访问时触发）")
    log(f"{'='*50}")
    log("")

    # 启动时确保 main.js 有时间戳（尚无则首次注入）
    inject_timestamp()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log("\n👋 服务已停止")
        server.server_close()


if __name__ == "__main__":
    main()
