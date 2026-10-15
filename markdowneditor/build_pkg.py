# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-or-later
"""组装 + 构建脚本：从 MDEditor 源目录（SSOT）拉取工具运行件进包，再产 wheel。

源目录不做任何修改；本脚本只做"选择性复制 + 数量断言 + 构建"。
SSOT 解析：仓库根的上一级目录下的 MarkdownEditor/（可用环境变量 MDEDITOR_SSOT 覆盖）。
用法：python build_pkg.py [--no-build]
"""

import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))          # .../markdowneditor-ldxs/markdowneditor
REPO_ROOT = os.path.dirname(HERE)                           # .../markdowneditor-ldxs
SRC = os.path.join(HERE, "src", "markdowneditor_ldxs")
SSOT = os.environ.get(
    "MDEDITOR_SSOT",
    os.path.join(os.path.dirname(REPO_ROOT), "MarkdownEditor"))

# 工具运行件清单（name, kind, min_count）
FILES = [
    ("md_server.py", "file", 1),
    ("md_assets.py", "file", 1),
    ("_pickfile.py", "file", 1),
    ("markdown-editor.html", "file", 1),
    ("md-editor.ico", "file", 1),
    ("LICENSE", "file", 1),
]
DIRS = [
    ("katex", 1),
    ("pagedjs", 1),
]


def build():
    if not os.path.isdir(SSOT):
        sys.exit("[FATAL] SSOT not found: %s" % SSOT)
    os.makedirs(SRC, exist_ok=True)
    for name, kind, _ in FILES:
        s = os.path.join(SSOT, name)
        d = os.path.join(SRC, name)
        if kind == "file":
            if not os.path.isfile(s):
                sys.exit("[FATAL] missing SSOT file: %s" % s)
            shutil.copyfile(s, d)
            assert os.path.isfile(d) and os.path.getsize(d) > 0, name
    for name, _ in DIRS:
        s = os.path.join(SSOT, name)
        d = os.path.join(SRC, name)
        if not os.path.isdir(s):
            sys.exit("[FATAL] missing SSOT dir: %s" % s)
        if os.path.isdir(d):
            shutil.rmtree(d)
        shutil.copytree(s, d)
    # count assertions
    for name, kind, expect in FILES:
        p = os.path.join(SRC, name)
        n = 1 if os.path.isfile(p) else 0
        assert n == expect, "count(%s)=%d != %d" % (name, n, expect)
    for name, min_n in DIRS:
        n = sum(len(f) for _, _, f in os.walk(os.path.join(SRC, name)))
        assert n >= min_n and n > 0, "count(%s)=%d" % (name, n)
    total = sum(len(f) for _, _, f in os.walk(SRC))
    print("[OK] assembled: %d files in %s" % (total, SRC))
    if "--no-build" in sys.argv:
        return
    r = subprocess.run([sys.executable, "-m", "build", "--wheel"],
                       cwd=HERE)
    if r.returncode != 0:
        sys.exit("[FATAL] build failed")


if __name__ == "__main__":
    build()
