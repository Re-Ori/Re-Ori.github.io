#!/usr/bin/env python3
"""
开发测试服务器
=============
纯 Python 标准库，零依赖。

与 server.py 使用相同的 app.AutoUpdateHandler（Giscus 代理、白名单、P2P 信令等），
但 **禁用了 GitHub 自动更新**，本地文件修改不会被远程覆盖。

自动监控 app.py / server.py 变更并重启。

用法:
    python dev_server.py                  # 默认端口 9876
    python dev_server.py --port 8080      # 自定义端口
"""

from __future__ import annotations

import sys
import os
import time
import threading
from pathlib import Path

# 把项目根目录加入 sys.path，确保能导入 app.py
_project_root = Path(__file__).resolve().parent
sys.path.insert(0, str(_project_root))

import http.server
import app as svc  # 导入远程服务器的所有功能


# 监控 app.py 和 server.py 的修改时间，自动重启
_watched_files = [
    _project_root / "app.py",
    _project_root / "server.py",
]
_watched_mtimes = {
    p: p.stat().st_mtime if p.exists() else 0
    for p in _watched_files
}


def _watch_files():
    while True:
        time.sleep(2)
        try:
            for fpath in _watched_files:
                if fpath.exists() and fpath.stat().st_mtime != _watched_mtimes.get(fpath, 0):
                    svc.log(f"检测到 {fpath.name} 变更，正在重启…")
                    time.sleep(1)
                    os.execv(sys.executable, [sys.executable] + sys.argv)
        except Exception:
            pass


class DevHandler(svc.AutoUpdateHandler):
    """开发模式 Handler — 去掉自动更新，其余功能完全一致。"""

    @classmethod
    def _try_check_update(cls):
        """开发模式：不检查 GitHub 更新，保护本地修改。"""
        pass


def main():
    import argparse

    parser = argparse.ArgumentParser(
        description="开发测试服务器 — 无自动更新，适合本地调试"
    )
    parser.add_argument(
        "--host", default="0.0.0.0",
        help="监听地址 (默认: 0.0.0.0)"
    )
    parser.add_argument(
        "--port", type=int, default=9876,
        help="监听端口 (默认: 9876)"
    )
    args = parser.parse_args()

    server = http.server.HTTPServer(
        (args.host, args.port), DevHandler
    )
    addr = args.host if args.host != "0.0.0.0" else "localhost"

    # 启动文件监控线程
    threading.Thread(target=_watch_files, daemon=True).start()

    svc.log("")
    svc.log(f"{'='*50}")
    svc.log(f"  开发测试服务器已启动")
    svc.log(f"  地址: http://{addr}:{args.port}")
    svc.log(f"  目录: {svc.PROJECT_ROOT}")
    svc.log(f"  app.py / server.py 自动热重启已启用")
    svc.log(f"{'='*50}")
    svc.log("")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        svc.log("\n服务已停止")
        server.server_close()


if __name__ == "__main__":
    main()
