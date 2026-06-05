#!/usr/bin/env python3
"""
AutoUpdate Web Server — 启动引导器（Bootstrapper）
===================================================
只负责三件事：
1. 检查上次是否异常退出 → 回退 app.py 到前一个稳定版本
2. 设置崩溃标记（下次启动时检测）
3. 导入 app.py 并运行 HTTP 服务器

实际的服务器逻辑（Handler、代理、P2P、GitHub 同步等）在 app.py 中。
server.py 自身极少需要修改。

用法:
    python server.py                  # 默认端口 9876
    python server.py --port 8080      # 自定义端口
"""

from __future__ import annotations

import os
import sys
import json
import time
import shutil
import atexit
import traceback
from pathlib import Path
from datetime import datetime, timezone, timedelta

PROJECT_ROOT = Path(__file__).resolve().parent
CRASH_MARKER = PROJECT_ROOT / ".crash_marker.json"
STATE_FILE = PROJECT_ROOT / ".update_state.json"
VERSIONS_DIR = PROJECT_ROOT / ".versions"


# ── 工具 ───────────────────────────────────────────────────

def _log(msg: str):
    ts = datetime.now().strftime("%H:%M:%S")
    try:
        print(f"[{ts}] {msg}")
    except UnicodeEncodeError:
        print(f"[{ts}] {msg.encode('gbk', 'replace').decode('gbk')}")


def _format_utc8(dt: datetime) -> str:
    return dt.strftime("%Y.%m.%d %H:%M:%S") + " [UTC+8]"


def _load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _save_state(state: dict):
    STATE_FILE.write_text(
        json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8"
    )


# ── 崩溃标记管理 ──────────────────────────────────────────

def _write_crash_marker():
    """启动时写入崩溃标记，正常退出时清除。"""
    CRASH_MARKER.write_text(json.dumps({
        "pid": os.getpid(),
        "status": "running",
        "started_at": _format_utc8(
            datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=8)
        ),
    }), encoding="utf-8")


def _clear_crash_marker():
    try:
        if CRASH_MARKER.exists():
            CRASH_MARKER.unlink()
    except Exception:
        pass


def _mark_clean_shutdown():
    """将标记更新为"正常退出"状态（atexit 注册）。"""
    try:
        if CRASH_MARKER.exists():
            data = json.loads(CRASH_MARKER.read_text(encoding="utf-8"))
            data["status"] = "clean"
            CRASH_MARKER.write_text(json.dumps(data), encoding="utf-8")
    except Exception:
        pass


def _check_previous_crash() -> dict | None:
    """
    检查上次运行是否异常退出。
    返回崩溃信息 dict，正常则返回 None。
    """
    if not CRASH_MARKER.exists():
        return None
    try:
        data = json.loads(CRASH_MARKER.read_text(encoding="utf-8"))
        pid = data.get("pid")
        # 如果标记文件的 PID 和当前进程不同，说明是上一次运行的残留
        if data.get("status") == "running" and pid != os.getpid():
            return data
    except Exception:
        pass
    return None


# ── 版本回退 ──────────────────────────────────────────────

def _rollback_service(crash_data: dict) -> dict | None:
    """
    回退 app.py 到 .versions/ 中最近的一个版本。
    返回回退信息 dict（含错误原因、恢复时间等），失败返回 None。
    """
    state = _load_state()
    sv = state.get("service_version", {})
    queue = sv.get("version_queue", [])

    if not queue:
        _log("版本队列为空，无法回退")
        return None

    prev_id = queue[-1]
    prev_path = VERSIONS_DIR / f"service.v{prev_id}.py"

    if not prev_path.exists():
        _log(f"回退版本 v{prev_id} 文件不存在: {prev_path}")
        return None

    service_py = PROJECT_ROOT / "app.py"
    if not service_py.exists():
        _log("app.py 不存在，无法回退（将尝试全新启动）")
        return None

    try:
        shutil.copy2(prev_path, service_py)
    except Exception as e:
        _log(f"回退时复制文件失败: {e}")
        return None

    now_utc8 = _format_utc8(
        datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=8)
    )

    # 记录回退信息到状态文件（供 F12 控制台显示）
    rollback_info = {
        "error": crash_data.get("error", "程序异常退出"),
        "crashed_version": sv.get("current_version_id", "?"),
        "recovered_to_version": prev_id,
        "recovered_at": now_utc8,
    }
    _log(f"已自动回退至 app.py v{prev_id}（{now_utc8}）")

    # 更新版本状态
    sv["current_version_id"] = prev_id
    sv["crash_info"] = rollback_info
    state["service_version"] = sv
    _save_state(state)

    return rollback_info


# ── 主入口 ─────────────────────────────────────────────────

def main():
    # ═══ 阶段 1：崩溃检测与版本回退 ═══
    crash_data = _check_previous_crash()
    if crash_data:
        _log("检测到上次异常退出")
        error_hint = crash_data.get("error", "")
        if error_hint:
            _log(f"   错误: {error_hint}")
        _log("正在检查可回退的版本…")
        rollback_ok = _rollback_service(crash_data)
        if rollback_ok:
            _log(f"已恢复至 v{rollback_ok['recovered_to_version']}")
        else:
            _log("无可回退版本，将使用当前 app.py 启动")
    else:
        _log("上次运行正常退出")

    # ═══ 阶段 2：设置崩溃标记 ═══
    _clear_crash_marker()
    _write_crash_marker()
    atexit.register(_mark_clean_shutdown)

    # ═══ 阶段 3：导入并运行 app ═══
    sys.path.insert(0, str(PROJECT_ROOT))

    max_attempts = 2
    last_error = None

    for attempt in range(1, max_attempts + 1):
        try:
            _log(f"正在启动服务（尝试 {attempt}/{max_attempts}）…")
            import app
            import importlib
            if 'app' in sys.modules:
                importlib.reload(app)
            app.main()
            return
        except KeyboardInterrupt:
            _log("\n服务已停止（Ctrl+C）")
            return
        except SystemExit:
            return
        except Exception as e:
            last_error = e
            error_msg = f"{type(e).__name__}: {e}"
            tb = traceback.format_exc()
            _log(f"第 {attempt} 次启动失败: {error_msg}")
            _log(tb)

            if attempt == 1:
                _log("尝试回退到前一个版本并重试…")
                crash_data = {
                    "error": error_msg[:500],
                    "status": "running",
                    "pid": os.getpid(),
                }
                rollback_ok = _rollback_service(crash_data)
                if not rollback_ok:
                    _log("回退失败或无可回退版本")
                    break
                _log("回退完成，准备重试…")
                time.sleep(1)
                sys.modules.pop('app', None)
            else:
                _log("两次启动均失败，请检查 app.py 手动修复")
                break

    # 两次都失败了，记录到崩溃标记并退出
    if last_error:
        error_detail = f"{type(last_error).__name__}: {last_error}"
        tb_detail = traceback.format_exc()
        try:
            CRASH_MARKER.write_text(json.dumps({
                "pid": os.getpid(),
                "status": "running",
                "error": error_detail[:500],
                "traceback": tb_detail[:2000],
                "started_at": _format_utc8(
                    datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=8)
                ),
            }), encoding="utf-8")
        except Exception:
            pass

        _log("服务启动失败，退出代码 1")
        sys.exit(1)


if __name__ == "__main__":
    main()
