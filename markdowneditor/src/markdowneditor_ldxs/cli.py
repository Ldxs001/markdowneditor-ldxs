# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-or-later
"""PyPI 渠道启动胶水。

职责与 zip 渠道的 md_launch.pyw 一致：
1. 探测 127.0.0.1:PORT 是否已有实例（单实例）；
2. 没有则用当前解释器拉起 md_server.main()（分离进程、无控制台窗口）；
3. 浏览器打开编辑页（可带 ?file= 文档参数）。

与 md_launch 的差异：不管理内嵌 python/ 内核（pip 用户自带解释器）、
不做进程清理（服务端空闲看门狗已自愈）。工具本体零改动。
"""

import os
import subprocess
import sys
import time
import urllib.parse
import urllib.request
import webbrowser

from . import md_server

PORT = md_server.PORT
VERSION = md_server.MD_SERVER_VERSION


def _alive(timeout=1.5):
    try:
        urllib.request.urlopen(
            "http://127.0.0.1:%d/api/version" % PORT, timeout=timeout)
        return True
    except Exception:
        return False


def _open_browser(path):
    q = ("?file=" + urllib.parse.quote(path)) if path else ""
    webbrowser.open(
        "http://127.0.0.1:%d/%sv=%d" % (PORT, (q + "&") if q else "", VERSION))


def main(argv=None):
    argv = sys.argv[1:] if argv is None else list(argv)
    path = os.path.abspath(argv[0]) if argv else None
    if not _alive():
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        subprocess.Popen(
            [sys.executable, "-c",
             "import markdowneditor_ldxs.md_server as s; s.main()"],
            cwd=os.path.dirname(os.path.abspath(md_server.__file__)),
            creationflags=flags)
        for _ in range(50):
            if _alive(0.5):
                break
            time.sleep(0.1)
    _open_browser(path)


if __name__ == "__main__":
    main()
