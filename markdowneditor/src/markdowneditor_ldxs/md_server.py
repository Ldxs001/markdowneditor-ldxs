# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 wUwproject. Licensed under GNU AGPL v3 (or later).
"""
MD 编辑器本地后端 + 系统托盘（纯标准库，零第三方依赖）
- 标准库 http.server 提供页面 / 读文件 / 写文件路由
- 仅在 127.0.0.1 监听，单实例（由 md_launch.pyw 负责端口占用检测）
- 托盘用 ctypes 直接调用 Shell_NotifyIcon，无 pystray；失败则降级无托盘常驻
本文件与 markdown-editor.html 一同分发，配合内置 python/ 即可在任意 Windows 运行。
"""
import http.server
import hashlib
import io
import base64
import time
import subprocess
import tempfile
import socketserver
import threading
import os
import sys
import json
import re
import atexit
import urllib.parse
import webbrowser
import html as html_module

PORT = 18765
HERE = os.path.dirname(os.path.abspath(__file__))
EDITOR_HTML = os.path.join(HERE, "markdown-editor.html")
MD_SERVER_VERSION = 36  # 每次服务端路由/能力变更时 +1；md_launch 依此自愈换代


class ReuseTCPServer(socketserver.TCPServer):
    allow_reuse_address = True


class Handler(http.server.BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="text/plain; charset=utf-8", extra=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        if u.path in ("/", "/index.html"):
            try:
                with open(EDITOR_HTML, "r", encoding="utf-8") as f:
                    html = f.read()
            except Exception as e:  # pragma: no cover
                self._send(500, "read editor failed: %s" % e)
                return
            build = hashlib.md5(html.encode("utf-8")).hexdigest()[:10]
            html = html.replace("<body>", '<body data-build="%s">' % build, 1)
            self._send(200, html, "text/html; charset=utf-8",
                       {"Cache-Control": "no-store"})
            return
        if u.path == "/api/version":
            self._send(200, json.dumps({"ok": True, "version": MD_SERVER_VERSION},
                                       ensure_ascii=False))
            return
        if u.path == "/api/ping":
            # 页面心跳：仅用于"页面卸载后确认是否真关闭"的宽限判断。
            # 注意：闲置（切标签页/最小化）绝不触发退出。
            _LAST_PING[0] = time.time()
            _BYE_AT[0] = 0.0        # 收到新心跳 => 撤销待退出（刷新/重开场景）
            self._send(200, '{"ok":true}', "application/json; charset=utf-8")
            return
        if u.path == "/api/bye":
            # 页面卸载通知（pagehide -> sendBeacon）。刷新也会走到这里，
            # 因此只"登记"，由看门狗在宽限期后再决定是否退出。
            _BYE_AT[0] = time.time()
            self._send(200, '{"ok":true}', "application/json; charset=utf-8")
            return
        if u.path == "/api/browse-dir":
            try:
                d = _pick_dir_subprocess()
                self._send(200, json.dumps({"ok": True, "dir": d or ""},
                                           ensure_ascii=False))
            except Exception as e:
                self._send(500, json.dumps({"ok": False, "error": str(e)},
                                           ensure_ascii=False))
            return
        if u.path == "/api/open-file":
            # 浏览器模式专用：用原生"打开"框拿真实完整路径
            try:
                q = urllib.parse.parse_qs(u.query)
                initdir = q.get("dir", [""])[0]
                p = _pick_file_subprocess(initdir)
                if p:
                    _LAST_DOC_DIR[0] = os.path.dirname(p)
                self._send(200, json.dumps({"ok": True, "path": p},
                                           ensure_ascii=False))
            except Exception as e:
                self._send(500, json.dumps({"ok": False, "error": str(e)},
                                           ensure_ascii=False))
            return
        if u.path == "/api/common-dirs":
            try:
                out = _common_dirs()
                self._send(200, json.dumps({"ok": True, "dirs": out},
                                           ensure_ascii=False))
            except Exception as e:
                self._send(500, json.dumps({"ok": False, "error": str(e)},
                                           ensure_ascii=False))
            return
        if u.path == "/api/version":
            self._send(200, json.dumps({"ok": True, "version": MD_SERVER_VERSION},
                                       ensure_ascii=False))
            return
        if u.path == "/api/fonts":
            try:
                import winreg
                fonts = set()
                for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
                    try:
                        k = winreg.OpenKey(hive, "SOFTWARE\\Microsoft\\Windows NT\\CurrentVersion\\Fonts")
                    except OSError:
                        continue
                    i = 0
                    while True:
                        try:
                            name, _, _ = winreg.EnumValue(k, i)
                            i += 1
                        except OSError:
                            break
                        name = re.sub(r"\s*\((TrueType|OpenType|PostScript)\)$", "", name, flags=re.IGNORECASE)
                        for part in name.split("&"):
                            part = part.strip()
                            if part:
                                fonts.add(part)
                    try:
                        winreg.CloseKey(k)
                    except OSError:
                        pass
                self._send(200, json.dumps({"ok": True,
                    "fonts": sorted(fonts, key=lambda s: s.lower())}, ensure_ascii=False))
            except Exception as e:
                self._send(500, json.dumps({"ok": False, "error": str(e)},
                                           ensure_ascii=False))
            return
        if u.path.startswith("/static/"):
            rel = urllib.parse.unquote(u.path[len("/static/"):])
            root = os.path.normpath(HERE)
            full = os.path.normpath(os.path.join(HERE, rel))
            if not (full == root or full.startswith(root + os.sep)):
                self._send(403, "forbidden")
                return
            ctypes_map = {
                ".css": "text/css; charset=utf-8",
                ".js": "application/javascript; charset=utf-8",
                ".woff2": "font/woff2", ".woff": "font/woff", ".ttf": "font/ttf",
                ".html": "text/html; charset=utf-8", ".png": "image/png",
                ".svg": "image/svg+xml", ".ico": "image/x-icon",
            }
            ctype = ctypes_map.get(os.path.splitext(full)[1].lower(),
                                   "application/octet-stream")
            try:
                with open(full, "rb") as f:
                    data = f.read()
            except Exception:
                self._send(404, "not found")
                return
            self._send(200, data, ctype)
            return
        if u.path == "/api/file":
            q = urllib.parse.parse_qs(u.query)
            p = q.get("path", [""])[0]
            if not p:
                self._send(400, json.dumps({"ok": False, "error": "missing path"}, ensure_ascii=False))
                return
            try:
                _LAST_DOC_DIR[0] = os.path.dirname(p)
                with open(p, "r", encoding="utf-8", errors="replace") as f:
                    data = f.read()
            except Exception as e:
                self._send(500, json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False))
                return
            self._send(200, data, "text/plain; charset=utf-8")
            return
        if u.path == "/img":
            q = urllib.parse.parse_qs(u.query)
            p = q.get("path", [""])[0]
            if p and not os.path.isabs(p):
                cand = os.path.join(HERE, p)
                if os.path.exists(cand):
                    p = cand
            exts = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
                    ".webp": "image/webp", ".gif": "image/gif", ".svg": "image/svg+xml",
                    ".bmp": "image/bmp"}
            ext = os.path.splitext(p)[1].lower()
            try:
                st = os.stat(p)
                with open(p, "rb") as f:
                    data = f.read()
            except Exception:
                self._send(404, "image not found")
                return
            # 协商缓存：ETag=mtime-size，浏览器每次请求都回源校验，
            # 文件没变回 304（不重传），变了立刻拿到新图 —— F5 即见效
            etag = '"%d-%d"' % (int(st.st_mtime), st.st_size)
            if self.headers.get("If-None-Match") == etag:
                self.send_response(304)
                self.send_header("Cache-Control", "no-cache")
                self.send_header("ETag", etag)
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", exts.get(ext, "application/octet-stream"))
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-cache")
            self.send_header("ETag", etag)
            self.end_headers()
            self.wfile.write(data)
            return
        if u.path == "/docs-list":
            docs_dir = os.path.join(HERE, "docs")
            items = []
            try:
                for f in sorted(os.listdir(docs_dir)):
                    if f.lower().endswith(".md"):
                        items.append({"name": os.path.splitext(f)[0],
                                      "file": f})
            except Exception:
                pass
            self._send(200, json.dumps({"ok": True, "docs": items},
                                       ensure_ascii=False))
            return
        if u.path == "/api/log":
            q = urllib.parse.parse_qs(u.query)
            msg = q.get("msg", [""])[0][:500]
            try:
                rt = os.path.join(HERE, "runtime")
                os.makedirs(rt, exist_ok=True)
                with io.open(os.path.join(rt, "ui.log"), "a",
                             encoding="utf-8") as f:
                    f.write("%s %s\n" % (time.strftime("%H:%M:%S"), msg))
            except Exception:
                pass
            self._send(200, json.dumps({"ok": True}, ensure_ascii=False))
            return
        if u.path == "/api/open-external":
            q = urllib.parse.parse_qs(u.query)
            url = q.get("url", [""])[0]
            # 仅允许本机服务的 URL，防止被用作任意程序启动器
            if url.startswith("/"):
                url = "http://127.0.0.1:%d%s" % (PORT, url)
            if url.startswith("http://127.0.0.1:%d/" % PORT)                     or url.startswith("http://localhost:%d/" % PORT):
                threading.Thread(target=lambda: webbrowser.open(url),
                                 daemon=True).start()
                self._send(200, json.dumps({"ok": True},
                                           ensure_ascii=False))
            else:
                self._send(400, json.dumps({"ok": False,
                    "error": "仅允许打开本机服务地址"}, ensure_ascii=False))
            return
        if u.path == "/doc":
            q = urllib.parse.parse_qs(u.query)
            fname = q.get("file", [""])[0]
            docs_dir = os.path.normpath(os.path.join(HERE, "docs"))
            full = os.path.normpath(os.path.join(docs_dir, fname))
            if (fname and full.startswith(docs_dir + os.sep)
                    and os.path.isfile(full)):
                try:
                    with io.open(full, "r", encoding="utf-8") as f:
                        body = f.read()
                    _LAST_DOC_DIR[0] = os.path.dirname(full)
                    self._send(200, json.dumps(
                        {"ok": True, "path": full, "content": body},
                        ensure_ascii=False))
                except Exception as e:
                    self._send(500, json.dumps({"ok": False, "error": str(e)},
                                               ensure_ascii=False))
            else:
                self._send(404, json.dumps({"ok": False,
                    "error": "文档不存在"}, ensure_ascii=False))
            return
        if u.path == "/open":
            q = urllib.parse.parse_qs(u.query)
            f = q.get("file", [""])[0]
            v = MD_SERVER_VERSION
            if f:
                target = "/?file=" + urllib.parse.quote(f) + "&v=%d" % v
            else:
                target = "/?v=%d" % v
            # 先回响应、再异步开浏览器：避免启动器等待超时后重复打开
            self._send(200, "ok")
            _url = "http://127.0.0.1:%d%s" % (PORT, target)
            threading.Timer(0.25, lambda: webbrowser.open(_url)).start()
            return
        # 根级静态兜底：工具目录内的图片/常见静态文件（防穿越，白名单扩展名）
        root = os.path.normpath(HERE)
        rel = urllib.parse.unquote(u.path.lstrip("/"))
        full = os.path.normpath(os.path.join(HERE, rel))
        static_exts = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                       ".webp": "image/webp", ".gif": "image/gif", ".svg": "image/svg+xml",
                       ".ico": "image/x-icon", ".css": "text/css", ".js": "application/javascript"}
        if (full.startswith(root + os.sep) and rel
                and os.path.splitext(full)[1].lower() in static_exts
                and os.path.isfile(full)):
            try:
                with open(full, "rb") as f:
                    data = f.read()
                self._send(200, data, static_exts[os.path.splitext(full)[1].lower()])
                return
            except Exception:
                pass
        self._send(404, "not found")

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        if u.path == "/api/file":
            try:
                length = int(self.headers.get("Content-Length", 0))
                raw = self.rfile.read(length)
                obj = json.loads(raw.decode("utf-8"))
                p = obj.get("path", "")
                content = obj.get("content", "")
                if not p:
                    self._send(400, json.dumps({"ok": False, "error": "missing path"}, ensure_ascii=False))
                    return
                added = []
                if materialize_images:
                    content, added = materialize_images(p, content)
                with open(p, "w", encoding="utf-8") as f:
                    f.write(content)
                _LAST_DOC_DIR[0] = os.path.dirname(p)
                self._send(200, json.dumps({"ok": True,
                                            "images": added}, ensure_ascii=False))
            except Exception as e:
                self._send(500, json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False))
            return
        if u.path == "/api/save-as":
            try:
                length = int(self.headers.get("Content-Length", 0))
                obj = json.loads(self.rfile.read(length).decode("utf-8"))
                d = obj.get("dir", "").strip()
                name = (obj.get("name") or "未命名.md").strip()
                content = obj.get("content", "")
                if not d or not os.path.isdir(d):
                    self._send(400, json.dumps({"ok": False,
                        "error": "目录不存在"}, ensure_ascii=False))
                    return
                if not name.lower().endswith(".md"):
                    name += ".md"
                path = os.path.join(d, name)
                added = []
                if materialize_images:
                    content, added = materialize_images(path, content)
                else:
                    added = []
                with open(path, "w", encoding="utf-8") as f:
                    f.write(content)
                _LAST_DOC_DIR[0] = d
                self._send(200, json.dumps({"ok": True, "path": path,
                                            "images": added},
                                           ensure_ascii=False))
            except Exception as e:
                self._send(500, json.dumps({"ok": False, "error": str(e)},
                                           ensure_ascii=False))
            return
        if u.path == "/api/paste-image":
            try:
                length = int(self.headers.get("Content-Length", 0))
                obj = json.loads(self.rfile.read(length).decode("utf-8"))
                doc_path = obj.get("docPath") or ""
                data_url = obj.get("dataUrl") or ""
                if not doc_path:
                    self._send(400, json.dumps({"ok": False,
                        "error": "文档尚未保存"}, ensure_ascii=False))
                    return
                head, _, b64 = data_url.partition(",")
                ext = ".png"
                if "jpeg" in head: ext = ".jpg"
                elif "webp" in head: ext = ".webp"
                elif "gif" in head: ext = ".gif"
                stem = os.path.splitext(os.path.basename(doc_path))[0]
                assets = os.path.join(os.path.dirname(doc_path), stem + ".assets")
                os.makedirs(assets, exist_ok=True)
                fname = "%s_%s%s" % (time.strftime("%Y%m%d_%H%M%S"),
                                     str(int(time.time() * 1000))[-4:], ext)
                with open(os.path.join(assets, fname), "wb") as f:
                    f.write(base64.b64decode(b64))
                self._send(200, json.dumps({"ok": True,
                    "rel": "%s.assets/%s" % (stem, fname)}, ensure_ascii=False))
            except Exception as e:
                self._send(500, json.dumps({"ok": False, "error": str(e)},
                                           ensure_ascii=False))
            return
        if u.path == "/api/export-html":
            try:
                length = int(self.headers.get("Content-Length", 0))
                obj = json.loads(self.rfile.read(length).decode("utf-8"))
                html = build_standalone_html(obj.get("path") or None,
                                             obj.get("bodyHtml", ""),
                                             obj.get("tsCss", ""))
                name = "document.html"
                with open(os.path.join(HERE, name), "w", encoding="utf-8") as f:
                    f.write(html)
                self._send(200, json.dumps({"ok": True, "url": "/static/" + name},
                                           ensure_ascii=False))
            except Exception as e:
                self._send(500, json.dumps({"ok": False, "error": str(e)},
                                           ensure_ascii=False))
            return
        if u.path == "/api/export-paged":
            try:
                length = int(self.headers.get("Content-Length", 0))
                obj = json.loads(self.rfile.read(length).decode("utf-8"))
                md_path = obj.get("path") or None
                body_html = obj.get("bodyHtml", "")
                ts_css = obj.get("tsCss", "")
                page_cfg = obj.get("cfg", {}) or {}
                html = build_paged_html(md_path, body_html, ts_css, page_cfg)
                # /static/ 路由会把前缀剥掉后映射到工具目录（/static/x -> HERE/x），
                # 因此文件写在 HERE 下、URL 用 /static/<name> 即可正确访问
                name = "document.paged.html"
                with open(os.path.join(HERE, name), "w", encoding="utf-8") as f:
                    f.write(html)
                self._send(200, json.dumps({"ok": True, "url": "/static/" + name},
                                           ensure_ascii=False))
            except Exception as e:
                self._send(500, json.dumps({"ok": False, "error": str(e)},
                                           ensure_ascii=False))
            return
        if u.path == "/api/export":
            try:
                length = int(self.headers.get("Content-Length", 0))
                obj = json.loads(self.rfile.read(length).decode("utf-8"))
                name = obj.get("name", "")
                content = obj.get("content", "")
                if not re.match(r"^[A-Za-z0-9._-]+\.html$", name):
                    self._send(400, json.dumps({"ok": False, "error": "bad name"}, ensure_ascii=False))
                    return
                with open(os.path.join(HERE, name), "w", encoding="utf-8") as f:
                    f.write(content)
                self._send(200, json.dumps({"ok": True, "url": "/static/" + name}, ensure_ascii=False))
            except Exception as e:
                self._send(500, json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False))
            return
        self._send(404, "not found")

    def log_message(self, *a):  # 静默
        pass


class Server(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True)
        self.httpd = ReuseTCPServer(("127.0.0.1", PORT), Handler)

    def run(self):
        self.httpd.serve_forever()

    def stop(self):
        try:
            self.httpd.shutdown()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# 系统托盘（纯 ctypes，无 pystray）
# ---------------------------------------------------------------------------
def build_tray(server):
    import ctypes
    from ctypes import wintypes

    # 部分 ctypes 分发版未导出指针宽类型，这里显式定义（Win64）
    LRESULT = ctypes.c_longlong
    UINT_PTR = ctypes.c_ulonglong

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)

    WM_TRAY = 0x0400 + 1
    WM_COMMAND = 0x0111
    WM_DESTROY = 0x0002
    WM_RBUTTONUP = 0x0205
    WM_LBUTTONUP = 0x0202

    NIF_MESSAGE = 0x1
    NIF_ICON = 0x2
    NIF_TIP = 0x4
    NIM_ADD = 0x0
    NIM_DELETE = 0x1

    user32.EnumWindows.restype = wintypes.BOOL
    _ENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user32.EnumWindows.argtypes = [_ENUMPROC, wintypes.LPARAM]
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.IsIconic.argtypes = [wintypes.HWND]
    user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    user32.GetWindowTextLengthW.restype = ctypes.c_int
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetWindowTextW.restype = ctypes.c_int
    user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.PostMessageW.argtypes = [wintypes.HWND, ctypes.c_uint,
                                    wintypes.WPARAM, wintypes.LPARAM]

    TPM_RIGHTBUTTON = 0x0002
    TPM_RETURNCMD = 0x0100

    MF_STRING = 0x0000
    IDI_APPLICATION = 32512
    ID_TRAY = 1
    ID_NEW = 1001
    ID_EXIT = 1002

    WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT,
                                 wintypes.WPARAM, wintypes.LPARAM)

    class POINT(ctypes.Structure):
        _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

    class MSG(ctypes.Structure):
        _fields_ = [
            ("hwnd", wintypes.HWND), ("message", wintypes.UINT),
            ("wParam", wintypes.WPARAM), ("lParam", wintypes.LPARAM),
            ("time", wintypes.DWORD), ("pt", POINT),
        ]

    class WNDCLASSEXW(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.UINT), ("style", wintypes.UINT),
            ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int),
            ("cbWndExtra", ctypes.c_int), ("hInstance", wintypes.HINSTANCE),
            ("hIcon", wintypes.HICON), ("hCursor", wintypes.HANDLE),
            ("hbrBackground", wintypes.HANDLE), ("lpszMenuName", wintypes.LPCWSTR),
            ("lpszClassName", wintypes.LPCWSTR), ("hIconSm", wintypes.HICON),
        ]

    class NOTIFYICONDATAW(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD), ("hWnd", wintypes.HWND),
            ("uID", wintypes.UINT), ("uFlags", wintypes.UINT),
            ("uCallbackMessage", wintypes.UINT), ("hIcon", wintypes.HICON),
            ("szTip", wintypes.WCHAR * 128), ("dwState", wintypes.DWORD),
            ("dwStateMask", wintypes.DWORD), ("szInfo", wintypes.WCHAR * 256),
            ("uVersion", wintypes.UINT), ("szInfoTitle", wintypes.WCHAR * 64),
            ("dwInfoFlags", wintypes.DWORD), ("guidItem", ctypes.c_char * 16),
            ("hBalloonIcon", wintypes.HICON),
        ]

    user32.RegisterClassExW.argtypes = [ctypes.POINTER(WNDCLASSEXW)]
    user32.RegisterClassExW.restype = wintypes.ATOM
    user32.CreateWindowExW.argtypes = [
        wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID]
    user32.CreateWindowExW.restype = wintypes.HWND
    user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    user32.DefWindowProcW.restype = LRESULT
    user32.LoadIconW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR]
    user32.LoadIconW.restype = wintypes.HICON
    user32.DestroyWindow.argtypes = [wintypes.HWND]
    user32.DestroyWindow.restype = wintypes.BOOL
    user32.PostQuitMessage.argtypes = [ctypes.c_int]
    user32.PostQuitMessage.restype = None
    user32.GetCursorPos.argtypes = [ctypes.POINTER(POINT)]
    user32.GetCursorPos.restype = wintypes.BOOL
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    user32.SetForegroundWindow.restype = wintypes.BOOL
    user32.CreatePopupMenu.restype = wintypes.HMENU
    user32.AppendMenuW.argtypes = [wintypes.HMENU, wintypes.UINT, UINT_PTR, wintypes.LPCWSTR]
    user32.AppendMenuW.restype = wintypes.BOOL
    user32.TrackPopupMenu.argtypes = [wintypes.HMENU, wintypes.UINT, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.HWND, wintypes.LPCVOID]
    user32.TrackPopupMenu.restype = wintypes.BOOL
    user32.DestroyMenu.argtypes = [wintypes.HMENU]
    user32.DestroyMenu.restype = wintypes.BOOL
    user32.GetMessageW.argtypes = [ctypes.POINTER(MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
    user32.GetMessageW.restype = LRESULT
    user32.TranslateMessage.argtypes = [ctypes.POINTER(MSG)]
    user32.TranslateMessage.restype = wintypes.BOOL
    user32.DispatchMessageW.argtypes = [ctypes.POINTER(MSG)]
    user32.DispatchMessageW.restype = LRESULT
    shell32.Shell_NotifyIconW.argtypes = [wintypes.DWORD, ctypes.POINTER(NOTIFYICONDATAW)]
    shell32.Shell_NotifyIconW.restype = wintypes.BOOL
    kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
    kernel32.GetModuleHandleW.restype = wintypes.HINSTANCE

    hinst = kernel32.GetModuleHandleW(None)
    # 资源 ID 按 Windows 约定以 (LPWSTR)(UINT_PTR)id 形式传入
    # 托盘图标：优先用工具自带 md-editor.ico，取不到才退回系统默认
    hicon = None
    _ico = os.path.join(HERE, "md-editor.ico")
    try:
        if os.path.exists(_ico):
            user32.LoadImageW.restype = wintypes.HANDLE
            user32.LoadImageW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR,
                                          ctypes.c_uint, ctypes.c_int,
                                          ctypes.c_int, ctypes.c_uint]
            hicon = user32.LoadImageW(None, _ico, 1, 16, 16, 0x10)
    except Exception:
        hicon = None
    if not hicon:
        hicon = user32.LoadIconW(0, ctypes.c_wchar_p(IDI_APPLICATION))

    class Tray:
        def __init__(self):
            self.hwnd = None
            self._wndproc = None  # 防止回调被 GC

        def wndproc(self, hwnd, msg, wparam, lparam):
            if msg == WM_TRAY and lparam == WM_LBUTTONUP:
                self.toggle_editor()
                return 0
            if msg == WM_TRAY and lparam == WM_RBUTTONUP:
                self.show_menu()
                return 0
            if msg == WM_DESTROY:
                user32.PostQuitMessage(0)
                return 0
            return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

        def find_editor_windows(self):
            found = []
            _ENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND,
                                           wintypes.LPARAM)

            def cb(h, l):
                try:
                    if not user32.IsWindowVisible(h):
                        return True
                    n = user32.GetWindowTextLengthW(h)
                    if n > 0:
                        buf = ctypes.create_unicode_buffer(n + 1)
                        user32.GetWindowTextW(h, buf, n + 1)
                        if ("MDEditor" in buf.value
                                or "Markdown 编辑器" in buf.value):
                            found.append(h)
                except Exception:
                    pass
                return True

            try:
                user32.EnumWindows(_ENUMPROC(cb), 0)
            except Exception:
                pass
            return found

        def toggle_editor(self):
            wins = self.find_editor_windows()
            if not wins:
                webbrowser.open("http://127.0.0.1:%d/" % PORT)
                return
            h = wins[0]
            try:
                if user32.IsIconic(h):
                    user32.ShowWindow(h, 9)          # SW_RESTORE
                    user32.SetForegroundWindow(h)
                elif user32.GetForegroundWindow() == h:
                    user32.ShowWindow(h, 6)          # SW_MINIMIZE
                else:
                    user32.SetForegroundWindow(h)    # 前置已开窗口
            except Exception:
                pass

        def show_menu(self):
            hmenu = user32.CreatePopupMenu()
            user32.AppendMenuW(hmenu, MF_STRING, ID_NEW, "新建空白文档")
            user32.AppendMenuW(hmenu, MF_STRING, ID_EXIT, "退出")
            pt = POINT()
            user32.GetCursorPos(ctypes.byref(pt))
            user32.SetForegroundWindow(self.hwnd)
            cmd = user32.TrackPopupMenu(hmenu, TPM_RIGHTBUTTON | TPM_RETURNCMD,
                                       pt.x, pt.y, 0, self.hwnd, None)
            user32.DestroyMenu(hmenu)
            if cmd == ID_NEW:
                webbrowser.open("http://127.0.0.1:%d/" % PORT)
            elif cmd == ID_EXIT:
                self.quit()

        def quit(self):
            # 关闭编辑器窗口（“退出”= 整个应用都关掉）
            for h in self.find_editor_windows():
                try:
                    user32.PostMessageW(h, 0x0010, 0, 0)  # WM_CLOSE
                except Exception:
                    pass
            time.sleep(0.25)
            nid = NOTIFYICONDATAW()
            nid.cbSize = ctypes.sizeof(nid)
            nid.hWnd = self.hwnd
            nid.uID = ID_TRAY
            shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(nid))
            try:
                server.stop()
            except Exception:
                pass
            user32.PostQuitMessage(0)

        def run(self):
            self._wndproc = WNDPROC(self.wndproc)
            cls = WNDCLASSEXW()
            cls.cbSize = ctypes.sizeof(cls)
            cls.lpfnWndProc = self._wndproc
            cls.hInstance = hinst
            cls.lpszClassName = "MDViewerTrayWnd"
            atom = user32.RegisterClassExW(ctypes.byref(cls))
            if not atom:
                raise ctypes.WinError(ctypes.get_last_error())
            self.hwnd = user32.CreateWindowExW(
                0, "MDViewerTrayWnd", None, 0, 0, 0, 0, 0,
                None, None, hinst, None)
            if not self.hwnd:
                raise ctypes.WinError(ctypes.get_last_error())
            nid = NOTIFYICONDATAW()
            nid.cbSize = ctypes.sizeof(nid)
            nid.hWnd = self.hwnd
            nid.uID = ID_TRAY
            nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
            nid.uCallbackMessage = WM_TRAY
            nid.hIcon = hicon
            nid.szTip = "Markdown 编辑器"
            if not shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(nid)):
                raise ctypes.WinError(ctypes.get_last_error())
            msg = MSG()
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))

    Tray().run()




# ---------------------------------------------------------------------------
# 分页导出组装（py 全权负责；前端只提交渲染好的 body 与排版 CSS）
# ---------------------------------------------------------------------------
PAGED_PAGE_BOX = {"bc": "@bottom-center", "br": "@bottom-right", "bl": "@bottom-left",
                  "tc": "@top-center", "tr": "@top-right"}

PAGED_FILLER_TMPL = """
(function(){
  var tpl = __TPL__;
  var sel = __SEL__;
  var size = __SIZE__;
  var lastN = -1, stable = 0;
  var mo = new MutationObserver(function(){ lastN = -1; });
  mo.observe(document.body, {childList:true, subtree:true});
  var iv = setInterval(function(){
    var pgs = document.querySelectorAll('.pagedjs_page');
    var n = pgs.length;
    if(!n) return;
    if(n !== lastN){ lastN = n; stable = 0; return; }
    stable++;
    if(stable < 3) return;
    clearInterval(iv); mo.disconnect();
    for(var i=0;i<n;i++){
      var el = pgs[i].querySelector(sel);
      if(el){ el.textContent = tpl.replace('{p}', i+1).replace('{P}', n); el.style.fontSize = size + 'pt'; }
    }
  }, 300);
})();
"""


def _strip_tags(s):
    return re.sub(r"<[^>]+>", "", s)




def build_standalone_html(md_path, body_html, ts_css):
    """普通 .html 导出组装（py 全权：图片路径还原 + katex css + 排版样式）"""
    katex_css = ""
    kp = os.path.join(HERE, "katex", "katex.min.css")
    if os.path.exists(kp):
        katex_css = io.open(kp, encoding="utf-8").read().replace(
            "url(fonts/", "url(/static/katex/fonts/")
    def _proxy(m):
        p = urllib.parse.unquote(m.group(1)).replace("\\", "/")
        return 'src="file:///' + p + '"'
    body = re.sub(r'src="/img\?path=([^"]+)"', _proxy, body_html)
    if md_path:
        mdir = os.path.dirname(md_path).replace("\\", "/")
        body = re.sub(r'src="(?!https?:|file:|/|#)([^"]+)"',
                      lambda m: 'src="file:///%s/%s"' % (mdir, m.group(1)), body)
    m1 = re.search(r"<h1[^>]*>([\s\S]*?)</h1>", body)
    title = re.sub(r"<[^>]+>", "", m1.group(1)).strip() if m1 else "未命名文档"
    ts = ts_css.replace("#preview", ".markdown-body")
    html = ('<!DOCTYPE html>\n<html lang="zh-CN"><head><meta charset="UTF-8"><title>__TITLE__</title>'
        "<style>__KATEX__</style><style>div.md-math-block{overflow-x:auto;margin:.8em 0;}"
        "span.md-math-inline{white-space:nowrap;}__TS__</style>"
        '<style>body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC",'
        '"Microsoft YaHei",sans-serif;max-width:820px;margin:40px auto;padding:0 20px;'
        'line-height:1.7;color:#222;}'
        'h1,h2{border-bottom:1px solid #eee;padding-bottom:.3em;}'
        'code{background:#f4f4f4;padding:.15em .4em;border-radius:4px;font-family:Consolas,monospace;}'
        'pre{background:#f4f4f4;padding:14px;border-radius:8px;overflow:auto;}'
        'blockquote{border-left:3px solid #4a6cf7;background:#f5f7ff;padding:.4em 1em;margin:1em 0;}'
        'table{border-collapse:collapse;width:100%;}th,td{border:1px solid #ddd;padding:7px 12px;}'
        'img{max-width:100%;}a{color:#4a6cf7;}</style></head><body class="markdown-body">__BODY__</body></html>')
    html = html.replace("__TITLE__", html_module.escape(title))
    html = html.replace("__KATEX__", katex_css)
    html = html.replace("__TS__", ts)
    html = html.replace("__BODY__", body)
    return html

def build_paged_html(md_path, body_html, ts_css, page_cfg):
    paper = page_cfg.get("paper", "A4")
    pw, ph = page_cfg.get("pw", ""), page_cfg.get("ph", "")
    if paper == "custom" and pw and ph:
        size_str = "%smm %smm" % (pw, ph)
    else:
        size_str = paper
    mt = page_cfg.get("mt", 25)
    mr = page_cfg.get("mr", 18)
    mb = page_cfg.get("mb", 25)
    ml = page_cfg.get("ml", 18)
    pn = page_cfg.get("pn", {}) or {}
    toc_cfg = page_cfg.get("toc", {}) or {}

    # @page：纸张 + 边距 + 页码 margin box（占位空白，正文文本由填充脚本写入）
    page_css = "@page{size:%s;margin:%smm %smm %smm %smm;" % (size_str, mt, mr, mb, ml)
    if pn.get("on") == "on":
        box = PAGED_PAGE_BOX.get(pn.get("pos", "bc"), "@bottom-center")
        page_css += '%s{content:" ";font-size:%spt;}' % (box, pn.get("size", 9))
    page_css += "}"
    page_css += ".md-toc-wrap{break-after:page;}h1,h2,h3{break-after:avoid;}pre,blockquote{break-inside:avoid;}"

    # TOC + 锚点
    heads = []

    def _head(m):
        lvl = int(m.group(1))
        inner = m.group(2)
        heads.append((lvl, inner))
        return '<h%s id="md-h-%d">%s</h%s>' % (lvl, len(heads), inner, lvl)

    body = re.sub(r"<h([123])[^>]*>([\s\S]*?)</h\1>", _head, body_html)
    toc_html = ""
    if toc_cfg.get("on") == "on" and heads:
        items = "".join(
            '<div class="toc-l%d"><a href="#md-h-%d">%s</a></div>' % (lvl, i, txt)
            for i, (lvl, txt) in enumerate(heads, 1))
        solo = "break-before:page;break-after:page;" if toc_cfg.get("solo", True) else "break-after:page;"
        toc_html = ('<div class="md-toc-wrap" style="%s"><nav class="md-toc">'
                    '<div class="md-toc-title">目 录</div>%s</nav></div>') % (solo, items)
        if toc_cfg.get("pos", "top") == "afterh1":
            body = re.sub(r"(<h1[\s\S]*?</h1>)", lambda m: m.group(1) + toc_html, body, count=1)
        else:
            body = toc_html + body

    # 资源内嵌（全部本地，离线可用）
    katex_css = ""
    kp = os.path.join(HERE, "katex", "katex.min.css")
    if os.path.exists(kp):
        katex_css = io.open(kp, encoding="utf-8").read().replace(
            "url(fonts/", "url(/static/katex/fonts/")
    paged_js = ""
    pp = os.path.join(HERE, "pagedjs", "paged.polyfill.min.js")
    if os.path.exists(pp):
        paged_js = io.open(pp, encoding="utf-8").read().replace("</script", "<\\/script")

    # 页码填充脚本（Paged.js 分页完成、DOM 稳定后填入；模板/位置/字号由配置注入）
    sel = ".pagedjs_margin-%s .pagedjs_margin-content" % PAGED_PAGE_BOX.get(
        pn.get("pos", "bc"), "bottom-center").replace("@", "")
    filler_vals = {
        "__TPL__": json.dumps(pn.get("tpl", "第 {p} 页 / 共 {P} 页"), ensure_ascii=False),
        "__SEL__": json.dumps(sel),
        "__SIZE__": json.dumps(str(pn.get("size", 9))),
    }
    filler = PAGED_FILLER_TMPL
    for k, v in filler_vals.items():
        filler = filler.replace(k, v)
    filler = filler.replace("</script", "<\\/script")

    toc_css = (
        ".md-toc-title{font-size:1.7em;font-weight:700;text-align:center;margin:0 0 1.4em;}"
        ".md-toc a{text-decoration:none;color:inherit;}"
        ".md-toc .toc-l1{font-weight:600;margin:.9em 0 .25em;}"
        ".md-toc .toc-l2{padding-left:1.6em;}"
        ".md-toc .toc-l3{padding-left:3.2em;}"
        ".md-toc a::after{content:'  ' target-counter(attr(href url),page);}"
    )

    m1 = re.search(r"<h1[^>]*>([\s\S]*?)</h1>", body)
    title = _strip_tags(m1.group(1)) if m1 else "未命名文档"
    title = title.strip() or "未命名文档"

    html = (
        '<!DOCTYPE html>\n<html lang="zh-CN"><head><meta charset="UTF-8"><title>%s</title>'
        "<style>%s</style><style>%s%s%s</style>"
        '</head><body class="markdown-body"><div id="paged-content" class="markdown-body">%s</div>'
        "<script>%s</script>"
        "<script>%s</script></body></html>"
    ) % (
        html_module.escape(title),
        page_css,
        katex_css,
        toc_css,
        ts_css,
        body,
        paged_js,
        filler,
    )
    return html


if HERE not in sys.path:
    sys.path.insert(0, HERE)
try:
    from md_assets import materialize_images
except Exception:
    import traceback as _tb
    try:
        with open(os.path.join(HERE, "runtime", "md_assets.err"), "a",
                  encoding="utf-8") as _ef:
            _ef.write(_tb.format_exc() + "\n")
    except Exception:
        pass
    materialize_images = None


_SHELL_FOLDERS_KEY = ("Software\\Microsoft\\Windows\\CurrentVersion\\"
                      "Explorer\\User Shell Folders")


def _shell_folder(regval, fallback=""):
    """读取 HKCU User Shell Folders 中的真实路径。
    这样能正确处理 OneDrive 重定向（例如桌面实际在 D:\\...\\OneDrive\\Desktop），
    而不是想当然地拼 C:\\Users\\<name>\\Desktop。失败则用 fallback。"""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            _SHELL_FOLDERS_KEY) as k:
            v, _ = winreg.QueryValueEx(k, regval)
            v = os.path.expandvars(v)
            if v and os.path.isdir(v):
                return v
    except Exception:
        pass
    return fallback


def _common_dirs():
    home = os.path.expanduser("~")
    cands = {
        "desktop": _shell_folder("Desktop", os.path.join(home, "Desktop")),
        "docs": _shell_folder("Personal", os.path.join(home, "Documents")),
        "downloads": _shell_folder(
            "{374DE290-123F-4565-9164-39C4925E467B}",
            os.path.join(home, "Downloads")),
        "home": home,
    }
    return {k: v for k, v in cands.items() if v and os.path.isdir(v)}


_LAST_DOC_DIR = [""]
_LAST_PING = [time.time()]          # 最近一次页面心跳时间
_START_TIME = [time.time()]         # 服务启动时间（用于宽限期）
_BYE_AT = [0.0]                     # 收到"页面卸载"通知的时间（0=无待退出）   # 最近打开/保存的文档所在目录（供相对路径解析）


def _run_picker(script, tmpname):
    """在独立进程中弹选择框；结果经临时文件回传（空串=取消或不可用）。"""
    here = os.path.dirname(os.path.abspath(__file__))
    picker = os.path.join(here, script)
    if not os.path.exists(picker):
        return ""
    tmp = os.path.join(tempfile.gettempdir(), tmpname)
    try:
        if os.path.exists(tmp):
            os.remove(tmp)
    except Exception:
        pass
    exe_dir = os.path.dirname(sys.executable)
    py_exe = os.path.join(exe_dir, "python.exe")
    if not os.path.exists(py_exe):
        py_exe = sys.executable
    try:
        subprocess.run([py_exe, picker, tmp], timeout=900,
                       creationflags=0x08000000)
    except Exception:
        pass
    try:
        return io.open(tmp, encoding="utf-8").read().strip()
    except Exception:
        return ""


def _pick_dir_subprocess():
    """只使用现代文件夹选择框（IFileOpenDialog + FOS_PICKFOLDERS）。
    用户取消 -> 返回空串，不再回退（回退会二次弹老式框，用户已明确弃用）。
    现代框不可用时返回空串并写诊断日志。"""
    r = _run_picker("_pickdir_modern.py", "mdeditor_pickdir2.txt")
    if r == "@CANCEL@":
        _pickdir_log("modern: 用户取消")
        return ""
    if r:
        _pickdir_log("modern: 选中 %s" % r)
        return r
    _pickdir_log("modern: 不可用/无返回（未回退）")
    return ""


def _pickdir_log(msg):
    try:
        import time as _t
        with io.open(os.path.join(HERE, "runtime", "pickdir.log"), "a",
                     encoding="utf-8") as f:
            f.write("%s %s\n" % (_t.strftime("%H:%M:%S"), msg))
    except Exception:
        pass


def _idle_watchdog(grace=None, check=0.5):
    """只在【用户确实关闭了页面】时退出服务。

    判定：收到页面卸载通知(/api/bye，由 pagehide 触发)，且宽限期内
    没有恢复心跳 → 认定用户真的关掉了编辑器，服务退出。

    明确不做的事：**不做"空闲超时"判定**。
    切换标签页、最小化、长时间不操作都不等于关闭 —— 编辑器必须由
    用户明确关闭才停。刷新页面会重新发心跳，从而撤销待退出。
    可用环境变量 MD_EXIT_GRACE 覆盖宽限期（便于测试）。"""
    if grace is None:
        grace = float(os.environ.get("MD_EXIT_GRACE", "8"))
    _START_TIME[0] = time.time()
    if not _auto_stop_enabled():
        return
    while True:
        time.sleep(check)
        now = time.time()
        bye = _BYE_AT[0]
        if not bye:
            continue
        if now - bye < grace:
            continue
        # 宽限期内又收到过心跳 => 是刷新/重开，撤销退出
        if now - _LAST_PING[0] < grace:
            continue
        try:
            with io.open(os.path.join(HERE, "runtime", "lifecycle.log"),
                         "a", encoding="utf-8") as f:
                f.write("%s page closed -> stop service\n"
                        % time.strftime("%H:%M:%S"))
        except Exception:
            pass
        os._exit(0)


def _auto_stop_enabled():
    """runtime/settings.json 里的 auto_stop（默认开启）。"""
    try:
        import json as _json
        p = os.path.join(HERE, "runtime", "settings.json")
        d = _json.loads(io.open(p, encoding="utf-8").read())
        return bool(d.get("auto_stop", True))
    except Exception:
        return True


def main():
    srv = Server()
    srv.start()
    atexit.register(srv.stop)
    # 空闲自动退出：页面关闭后无人使用，服务自行结束（无需手动 md_stop）
    threading.Thread(target=_idle_watchdog, daemon=True).start()
    ev = threading.Event()
    try:
        ev.wait()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
