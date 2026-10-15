# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 wUwproject. Licensed under GNU AGPL v3 (or later).
"""图片物化：把 Markdown 里的内嵌图片（base64 data URI / WorkBuddy blob 引用 /
本机绝对路径）写入 <文档名>.assets/，并把引用改写为相对路径，
使 .md 与 .assets 目录可整体拷贝迁移（"另存为后图片跟着走"）。

纯函数模块，无副作用，供 md_server.py 与 md_app.pyw 共用。
"""
import base64
import json
import os
import re

IMG_RE = re.compile(r'!\[([^\]]*)\]\(([^)]+)\)')
INVALID = re.compile(r'[\\/:*?"<>|\r\n\t]+')
MIME_EXT = (("jpeg", ".jpg"), ("jpg", ".jpg"), ("webp", ".webp"),
            ("gif", ".gif"), ("svg", ".svg"), ("bmp", ".bmp"))


def _ext_from_mime(s):
    s = (s or "").lower()
    for k, v in MIME_EXT:
        if k in s:
            return v
    return ".png"


def _safe_name(alt, idx, ext):
    n = INVALID.sub("", (alt or "")).strip()
    n = n[:40].strip()
    if not n:
        n = "image_%d" % idx
    return n + ext


def _unique(assets, name):
    base, ext = os.path.splitext(name)
    cand, i = name, 1
    while os.path.exists(os.path.join(assets, cand)):
        cand = "%s_%d%s" % (base, i, ext)
        i += 1
    return cand


def materialize_images(doc_path, content):
    """返回 (新内容, 已落盘文件名列表)。失败时原样返回，绝不破坏文档。"""
    if not doc_path or not content:
        return content, []
    stem = os.path.splitext(os.path.basename(doc_path))[0]
    base_dir = os.path.dirname(doc_path)
    assets = os.path.join(base_dir, stem + ".assets")
    saved = []
    counter = [0]

    def _handle(m):
        alt, url = m.group(1), m.group(2).strip()
        low = url.lower()
        # 网络图片 / 页内锚点 / 已是正确相对路径 → 保持原状
        if low.startswith(("http://", "https://", "#")):
            return m.group(0)
        if not (low.startswith("data:") or url.startswith("{")
                or os.path.isabs(url.replace("/", os.sep))
                or low.startswith("file:///")):
            return m.group(0)
        data, ext = None, ".png"
        if low.startswith("data:"):
            head, _, b64 = url.partition(",")
            ext = _ext_from_mime(head)
            try:
                data = base64.b64decode(b64)
            except Exception:
                return m.group(0)
        elif url.startswith("{"):
            try:
                o = json.loads(url)
            except Exception:
                return m.group(0)
            ext = _ext_from_mime(o.get("mime"))
            bp = o.get("blob_path") or ""
            if bp and os.path.isfile(bp):
                try:
                    with open(bp, "rb") as f:
                        data = f.read()
                except Exception:
                    return m.group(0)
            else:
                return m.group(0)
        else:
            p = url
            if low.startswith("file:///"):
                p = url[8:]
            p = p.replace("/", os.sep)
            if os.path.isfile(p):
                ext = os.path.splitext(p)[1].lower() or ".png"
                try:
                    with open(p, "rb") as f:
                        data = f.read()
                except Exception:
                    return m.group(0)
            else:
                return m.group(0)
        if data is None:
            return m.group(0)
        counter[0] += 1
        try:
            os.makedirs(assets, exist_ok=True)
            name = _unique(assets, _safe_name(alt, counter[0], ext))
            with open(os.path.join(assets, name), "wb") as f:
                f.write(data)
        except Exception:
            return m.group(0)
        saved.append(name)
        return "![%s](%s.assets/%s)" % (alt, stem, name)

    try:
        new = IMG_RE.sub(_handle, content)
    except Exception:
        return content, []
    return new, saved


def materialize_images_in_file(doc_path, content, writer):
    """写盘封装：先物化图片，再交给 writer(最终内容) 写文件。"""
    new, saved = materialize_images(doc_path, content)
    writer(new)
    return saved
