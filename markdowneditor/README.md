# markdowneditor-ldxs

本地 Markdown 编辑器（MDEditor）的 PyPI 分发渠道。单页编辑 + 实时预览：
KaTeX 公式、图片三种写法、分块增量渲染；排版面板（字体/字号/对齐/公式/表格样式）；
分页导出（Paged.js，纸张/页边距/页码/自动目录）；本地 HTTP 后端（文件读写、
图片代理、字体枚举），仅监听 127.0.0.1。

## 安装

```cmd
pip install markdowneditor-ldxs
```

## 使用

```cmd
mdeditor                  REM 启动服务并打开浏览器
mdeditor path\to\doc.md   REM 直接打开指定文档
python -m markdowneditor_ldxs doc.md
```

- 单实例：已有服务在跑时直接复用，通过 `/open?file=` 打开新文档。
- 服务端空闲自动退出（页面关闭后无人使用即结束）。
- 服务端设置写入包目录下 `runtime/settings.json`；系统级 Python 安装建议
  使用虚拟环境或 `--user`，避免写入受限。

## 许可

GNU AGPL-3.0-or-later（Copyright (C) 2026 wUwproject），完整文本见 `LICENSE`。
随附第三方组件（KaTeX、Paged.js）按其原始许可证分发。
