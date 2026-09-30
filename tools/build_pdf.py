#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
把 text/ 目录下的 Markdown 编译成一册 PDF。

用法:
    python tools/build_pdf.py                 # 输出到 pdf/求子三要.pdf
    python tools/build_pdf.py -o out.pdf      # 指定输出
    python tools/build_pdf.py --no-pagenum    # 不渲染页码（用命令行打印，兼容性更好）

依赖: 仅需 Python 标准库 + 本机 Chrome / Edge。
"""

import argparse
import base64
import io
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import textwrap
import time
import urllib.request
import zipfile

# ---------------------------------------------------------------- 基础路径

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEXT_DIR = os.path.join(ROOT, "text")
DEFAULT_OUT = os.path.join(ROOT, "pdf", "求子三要.pdf")

BOOK_TITLE = "求子三要"
BOOK_SUBTITLE = "附：礼念观世音菩萨求子疏 · 保身广嗣要义 · 求子开示辑录"
BOOK_AUTHOR = "印光大师　开示"
BOOK_EDITOR = "白话编译与整理： 开源共创"
BOOK_NOTE = "此版不设版权，欢迎随意转载翻印"


# ------------------------------------------------------- Markdown -> HTML

def inline(s):
    """行内标记：转义后处理 链接 / 加粗 / 行内代码。"""
    s = s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    # 先保护行内代码
    codes = []

    def stash(m):
        codes.append(m.group(1))
        return "\x00%d\x00" % (len(codes) - 1)

    s = re.sub(r"`([^`]+)`", stash, s)

    s = re.sub(r"&lt;br\s*/?&gt;", "<br />", s)

    # 链接
    s = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r'<a href="\2">\1</a>', s)
    # 加粗
    s = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", s)
    # 斜体
    s = re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)", r"<em>\1</em>", s)

    s = re.sub(r"\x00(\d+)\x00", lambda m: "<code>%s</code>" % codes[int(m.group(1))], s)
    return s


def is_cjk(ch):
    if not ch:
        return False
    o = ord(ch)
    return 0x2E80 <= o <= 0x9FFF or 0x3000 <= o <= 0x303F or 0xFF00 <= o <= 0xFFEF


def join_lines(parts):
    """把被物理折行的若干行拼回一段。

    parts 为 [(文本, 该行是否以硬换行结尾)]；hard 标记的是**该行本身**，
    拼接时看前一行的标志决定是否插 <br />。
    中英文相邻才补空格，中文之间直接相连。
    """
    out = ""
    for idx, (text, hard) in enumerate(parts):
        if idx == 0:
            out = text
            continue
        prev = out[-1:] if out else ""
        nxt = text[:1]
        if parts[idx - 1][1]:
            out += "<br />" + text
        elif is_cjk(prev) and is_cjk(nxt):
            out += text
        else:
            out += " " + text
    return out


def split_row(line):
    line = line.strip()
    if line.startswith("|"):
        line = line[1:]
    if line.endswith("|"):
        line = line[:-1]
    return [c.strip() for c in line.split("|")]


def md_to_html(md, slug):
    """极简 Markdown -> HTML。只支持本项目用到的语法。"""
    lines = md.split("\n")
    out = []
    i = 0
    n = len(lines)
    heading_seq = 0

    while i < n:
        line = lines[i]
        stripped = line.strip()

        # 空行
        if not stripped:
            i += 1
            continue

        # 分隔线
        if re.fullmatch(r"-{3,}|\*{3,}|_{3,}", stripped):
            out.append('<hr class="thin" />')
            i += 1
            continue

        # 表格（兼容两种写法：带分隔行的标准写法，以及首行即内容的简写）
        if stripped.startswith("|"):
            raw = []
            j = i
            while j < n and lines[j].strip().startswith("|"):
                raw.append(lines[j].strip())
                j += 1
            has_sep = len(raw) >= 2 and re.fullmatch(r"\|[\s:\-|]+\|", raw[1])
            if has_sep:
                head = split_row(raw[0])
                body = [split_row(x) for x in raw[2:]]
            else:
                head = None
                body = [split_row(x) for x in raw]
            out.append("<table>")
            if head is not None:
                out.append("<thead><tr>" + "".join(
                    "<th>%s</th>" % inline(c) for c in head) + "</tr></thead>")
            out.append("<tbody>")
            for r in body:
                cells = []
                for idx, c in enumerate(r):
                    if head is None and idx == 0:
                        cells.append('<td class="k">%s</td>' % inline(c.strip("*")))
                    elif c.startswith("**") and c.endswith("**"):
                        cells.append('<td class="k">%s</td>' % inline(c.strip("*")))
                    elif c in ("✅", "✓"):
                        cells.append('<td class="ok">✓</td>')
                    else:
                        cells.append("<td>%s</td>" % inline(c))
                out.append("<tr>" + "".join(cells) + "</tr>")
            out.append("</tbody></table>")
            i = j
            continue

        # 标题
        m = re.match(r"^(#{1,4})\s+(.*)$", stripped)
        if m:
            level = len(m.group(1))
            text = inline(m.group(2).strip())
            heading_seq += 1
            anchor = "%s-h%d" % (slug, heading_seq)
            cls = ""
            if level == 1:
                cls = ' class="chapter"'
            elif level == 2:
                cls = ' class="sec"'
            out.append('<h%d id="%s"%s>%s</h%d>' % (level, anchor, cls, text, level))
            i += 1
            continue

        # 引用块
        if stripped.startswith(">"):
            buf = []
            while i < n and lines[i].strip().startswith(">"):
                buf.append(re.sub(r"^\s*>\s?", "", lines[i]))
                i += 1
            inner = md_to_html("\n".join(buf), slug + "q")
            out.append("<blockquote>%s</blockquote>" % inner)
            continue

        # 列表
        if re.match(r"^[-*]\s+", stripped) or re.match(r"^\d+\.\s+", stripped):
            ordered = bool(re.match(r"^\d+\.\s+", stripped))
            items = []
            while i < n:
                cur = lines[i].strip()
                if ordered and re.match(r"^\d+\.\s+", cur):
                    items.append(re.sub(r"^\d+\.\s+", "", cur))
                    i += 1
                elif (not ordered) and re.match(r"^[-*]\s+", cur):
                    items.append(re.sub(r"^[-*]\s+", "", cur))
                    i += 1
                elif cur.startswith("  ") and items:
                    items[-1] += " " + cur.strip()
                    i += 1
                elif not cur:
                    break
                else:
                    break
            tag = "ol" if ordered else "ul"
            cls = ' class="checklist"' if any(x.startswith("[ ]") or x.startswith("[x]") for x in items) else ""
            lis = []
            for it in items:
                if it.startswith("[ ] "):
                    lis.append('<li class="chk">%s</li>' % inline(it[4:]))
                elif it.startswith("[x] "):
                    lis.append('<li class="chk done">%s</li>' % inline(it[4:]))
                else:
                    lis.append("<li>%s</li>" % inline(it))
            out.append("<%s%s>%s</%s>" % (tag, cls, "".join(lis), tag))
            continue

        # 普通段落（吸收后续非空行）
        buf = []
        while i < n:
            cur = lines[i]
            cur_r = cur.rstrip()
            if buf and (not cur_r
                        or cur_r.startswith("#") or cur_r.startswith(">") or cur_r.startswith("|")
                        or re.match(r"^[-*]\s+", cur_r) or re.match(r"^\d+\.\s+", cur_r)
                        or re.fullmatch(r"-{3,}", cur_r)):
                break
            hard = (len(cur) - len(cur_r) >= 2) or cur_r.endswith("\\")
            buf.append((cur_r.rstrip("\\"), hard))
            i += 1
            if not cur_r:
                break
        out.append("<p>%s</p>" % inline(join_lines(buf)))
        continue

    return "\n".join(out)


def build_toc(parts):
    """从各篇章的 h1/h2 抽目录。"""
    items = []
    for slug, html in parts:
        for m in re.finditer(r'<h([12]) id="([^"]+)"[^>]*>(.*?)</h\1>', html, re.S):
            level = int(m.group(1))
            anchor = m.group(2)
            text = re.sub(r"<[^>]+>", "", m.group(3))
            items.append((level, anchor, text))
    return items


# ------------------------------------------------------------------ 样式

CSS = """
@page {
  size: A4;
  margin: 20mm 18mm 18mm 18mm;
}

* { box-sizing: border-box; }

html { -webkit-print-color-adjust: exact; print-color-adjust: exact; }

body {
  font-family: "Source Han Serif SC", "Noto Serif CJK SC", "Songti SC",
               "STSong", "SimSun", "宋体", serif;
  font-size: 10.8pt;
  line-height: 1.9;
  color: #2b2724;
  margin: 0;
  text-align: justify;
  hyphens: auto;
}

/* ---------- 封面 ---------- */
.cover {
  page-break-after: always;
  height: 235mm;
  display: flex;
  flex-direction: column;
  justify-content: center;
  text-align: center;
  border: 0.6mm solid #b79b6a;
  padding: 12mm;
}
.cover .frame { border: 0.25mm solid #d8c6a4; padding: 18mm 10mm; height: 100%;
  display: flex; flex-direction: column; justify-content: center; }
.cover h1 {
  font-family: "Source Han Sans SC", "Noto Sans CJK SC", "Microsoft YaHei", "STZhongsong", serif;
  font-size: 34pt; font-weight: 700; letter-spacing: 6pt; margin: 0 0 6mm 0;
  color: #6b4f2a; text-indent: 6pt;
}
.cover .sub { font-size: 10.5pt; color: #8a6d45; line-height: 2.0; margin: 0 0 14mm 0; }
.cover .rule { width: 40mm; height: 0.3mm; background: #b79b6a; margin: 8mm auto; }
.cover .author { font-size: 12pt; color: #4a423b; margin-bottom: 4mm; }
.cover .editor { font-size: 9.5pt; color: #8a8079; line-height: 1.9; }
.cover .lotus { font-size: 22pt; color: #c9ab77; margin-bottom: 6mm; }
.cover .note {
  margin-top: 16mm; font-size: 9pt; color: #9a8f83; border-top: 0.25mm dashed #d8c6a4;
  padding-top: 6mm; line-height: 1.9;
}

/* ---------- 目录 ---------- */
.toc { page-break-after: always; }
.toc h2.toc-title {
  font-size: 15pt; letter-spacing: 4pt; text-align: center; color: #6b4f2a;
  margin: 4mm 0 10mm 0; font-weight: 600;
}
.toc ul { list-style: none; padding: 0; margin: 0; }
.toc li.l1 { margin: 3.6mm 0 1mm 0; font-size: 11pt; font-weight: 600; color: #3d342c; }
.toc li.l2 { margin: 0.6mm 0 0.6mm 8mm; font-size: 9.6pt; font-weight: 400; color: #6f655b; }
.toc a { color: inherit; text-decoration: none; }

/* ---------- 正文 ---------- */
h1.chapter {
  page-break-before: always;
  font-family: "Source Han Sans SC", "Noto Sans CJK SC", "Microsoft YaHei", sans-serif;
  font-size: 17pt; font-weight: 700; letter-spacing: 2pt;
  color: #6b4f2a; text-align: center;
  margin: 6mm 0 9mm 0; padding-bottom: 4mm;
  border-bottom: 0.5mm solid #d9c4a0;
}
h1.chapter:first-of-type { page-break-before: avoid; }
.bookstart h1.chapter:first-child { page-break-before: always; }

h2.sec {
  font-family: "Source Han Sans SC", "Noto Sans CJK SC", "Microsoft YaHei", sans-serif;
  font-size: 12.6pt; font-weight: 600; color: #7a5c33;
  margin: 7mm 0 3mm 0; padding-left: 3mm;
  border-left: 1.2mm solid #cbab78;
}
h3 {
  font-size: 11.2pt; font-weight: 600; color: #6b5b46;
  margin: 5mm 0 2mm 0;
}
h4 { font-size: 10.4pt; font-weight: 600; color: #6b5b46; margin: 4mm 0 1.5mm 0; }

p { margin: 0 0 3mm 0; text-indent: 2em; }
p:has(strong:first-child), h2 + p { text-indent: 0; }

strong { color: #7a4f2a; font-weight: 600; }
em { font-style: normal; color: #7a5c33; }
code { font-family: inherit; }

a { color: #8a6d45; text-decoration: none; border-bottom: 0.15mm dotted #b79b6a; }

blockquote {
  margin: 4mm 0; padding: 4mm 6mm;
  background: #fbf7f0; border-left: 1.2mm solid #cbab78;
  color: #4f463c; font-size: 10.2pt; line-height: 1.85;
}
blockquote p { margin: 0 0 2mm 0; text-indent: 0; }
blockquote p:last-child { margin-bottom: 0; }
blockquote blockquote { background: #f6f0e6; border-left-style: dotted; margin: 2mm 0; }

hr.thin { border: none; border-top: 0.25mm solid #e0d3bc; margin: 7mm 0; }

ul, ol { margin: 2mm 0 4mm 0; padding-left: 8mm; }
li { margin: 1.2mm 0; text-align: justify; }
ul.checklist { list-style: none; padding-left: 3mm; }
li.chk { text-indent: 0; }
li.chk::before { content: "□"; color: #b79b6a; margin-right: 2.5mm; font-size: 11pt; }
li.chk.done::before { content: "■"; color: #cbab78; }

table {
  width: 100%; border-collapse: collapse; margin: 4mm 0; font-size: 9.6pt;
  page-break-inside: avoid;
}
th, td {
  border: 0.2mm solid #ddd0b8; padding: 2.4mm 3mm; text-align: left; vertical-align: top;
  line-height: 1.7; text-indent: 0;
}
th { background: #f4ece0; color: #6b4f2a; font-weight: 600; }
table:not(:has(thead)) td:first-child { width: 1%; white-space: nowrap; }
table:not(:has(thead)) td:last-child { font-weight: 600; color: #6b4f2a; }
td.k { background: #faf6ef; color: #6b4f2a; font-weight: 600; white-space: nowrap; }
td.ok { text-align: center; color: #7a5c33; }

.closing {
  page-break-before: always; text-align: center; margin-top: 40mm; color: #8a7d6d;
}
.closing .big { font-size: 13pt; color: #6b4f2a; letter-spacing: 2pt; margin-bottom: 8mm; }
.closing .small { font-size: 9pt; line-height: 2.0; }
"""


def render_html(parts, toc):
    body_parts = []
    for idx, (slug, html) in enumerate(parts):
        cls = "bookstart" if idx == 0 else ""
        body_parts.append('<section class="%s">%s</section>' % (cls, html))

    toc_html = ['<div class="toc"><h2 class="toc-title">目 录</h2><ul>']
    for level, anchor, text in toc:
        toc_html.append('<li class="l%d"><a href="#%s">%s</a></li>' % (level, anchor, text))
    toc_html.append("</ul></div>")

    cover = """
<div class="cover">
  <div class="frame">
    <div class="lotus">❁</div>
    <h1>{title}</h1>
    <div class="sub">{subtitle}</div>
    <div class="rule"></div>
    <div class="author">{author}</div>
    <div class="editor">{editor}</div>
    <div class="note">{note}</div>
  </div>
</div>
""".format(title=BOOK_TITLE, subtitle=BOOK_SUBTITLE, author=BOOK_AUTHOR,
           editor=BOOK_EDITOR, note=BOOK_NOTE)

    closing = """
<div class="closing">
  <div class="big">南无大慈大悲救苦救难广大灵感观世音菩萨</div>
  <div class="small">
    本册依《印光法师文钞》整理，原文未改，白话编译仅供参考。<br/>
    此版不设版权，欢迎自由转载、翻印、改编、流通。<br/>
    如发现错漏，请至项目仓库指正，让这一册越来越准确。
  </div>
</div>
"""

    return """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8" />
<title>{title}</title><style>{css}</style></head>
<body>{cover}{toc}{body}{closing}</body></html>""".format(
        title=BOOK_TITLE, css=CSS, cover=cover,
        toc="".join(toc_html), body="\n".join(body_parts), closing=closing)


# ------------------------------------------------------- Chrome 查找 / 打印

def find_browser():
    cands = []
    for env in ("CHROME_PATH",):
        if os.environ.get(env):
            cands.append(os.environ[env])
    home = os.path.expanduser("~")
    cands += [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        os.path.join(home, r"AppData\Local\Google\Chrome\Application\chrome.exe"),
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        "/usr/bin/google-chrome", "/usr/bin/google-chrome-stable",
        "/usr/bin/chromium", "/usr/bin/chromium-browser",
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    ]
    found = shutil.which("google-chrome") or shutil.which("chromium") or shutil.which("chrome")
    if found:
        cands.insert(0, found)
    for c in cands:
        if c and os.path.isfile(c):
            return c
    return None


# --- 极简 WebSocket 客户端（仅够跑 CDP） ---

class WS:
    def __init__(self, url, timeout=30):
        m = re.match(r"ws://([^:/]+):(\d+)(/.*)$", url)
        if not m:
            raise RuntimeError("bad ws url: %s" % url)
        host, port, path = m.group(1), int(m.group(2)), m.group(3)
        self.sock = socket.create_connection((host, port), timeout=timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        req = ("GET %s HTTP/1.1\r\nHost: %s:%d\r\nUpgrade: websocket\r\n"
               "Connection: Upgrade\r\nSec-WebSocket-Key: %s\r\n"
               "Sec-WebSocket-Version: 13\r\n\r\n" % (path, host, port, key))
        self.sock.sendall(req.encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise RuntimeError("websocket handshake failed")
            buf += chunk
        self.buf = buf.split(b"\r\n\r\n", 1)[1]
        self.mid = 0

    def _read(self, n):
        while len(self.buf) < n:
            chunk = self.sock.recv(1 << 20)
            if not chunk:
                raise RuntimeError("socket closed")
            self.buf += chunk
        data, self.buf = self.buf[:n], self.buf[n:]
        return data

    def send(self, payload):
        data = json.dumps(payload).encode("utf-8")
        header = bytes([0x81])
        ln = len(data)
        if ln < 126:
            header += bytes([0x80 | ln])
        elif ln < (1 << 16):
            header += bytes([0x80 | 126]) + ln.to_bytes(2, "big")
        else:
            header += bytes([0x80 | 127]) + ln.to_bytes(8, "big")
        mask = os.urandom(4)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
        self.sock.sendall(header + mask + masked)

    def recv(self):
        while True:
            b0, b1 = self._read(2)
            ln = b1 & 0x7F
            if ln == 126:
                ln = int.from_bytes(self._read(2), "big")
            elif ln == 127:
                ln = int.from_bytes(self._read(8), "big")
            payload = self._read(ln)
            if (b0 & 0x0F) == 0x1:  # text frame
                return json.loads(payload.decode("utf-8"))

    def call(self, method, params=None, timeout=120):
        self.mid += 1
        mid = self.mid
        self.send({"id": mid, "method": method, "params": params or {}})
        deadline = time.time() + timeout
        while time.time() < deadline:
            msg = self.recv()
            if msg.get("id") == mid:
                if "error" in msg:
                    raise RuntimeError("%s: %s" % (method, msg["error"]))
                return msg.get("result", {})
        raise RuntimeError("timeout waiting for %s" % method)


def print_with_cdp(browser, html_path, out_path, page_numbers=True):
    """通过 DevTools 协议打印，可自定义页眉页脚（页码）。"""
    profile = tempfile.mkdtemp(prefix="cdp-profile-")
    port = 9333
    proc = subprocess.Popen([
        browser, "--headless=new", "--disable-gpu", "--no-first-run",
        "--no-default-browser-check", "--disable-extensions",
        "--remote-debugging-port=%d" % port,
        "--user-data-dir=%s" % profile, "about:blank",
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        target = None
        deadline = time.time() + 40
        while time.time() < deadline:
            try:
                with urllib.request.urlopen("http://127.0.0.1:%d/json/list" % port, timeout=2) as r:
                    for t in json.load(r):
                        if t.get("type") == "page":
                            target = t
                            break
                if target:
                    break
            except Exception:
                pass
            time.sleep(0.4)
        if not target:
            raise RuntimeError("无法连接 Chrome 调试端口")

        ws = WS(target["webSocketDebuggerUrl"])
        ws.call("Page.enable")
        loaded = {"done": False}
        ws.call("Page.navigate", {"url": "file:///" + html_path.replace("\\", "/")})
        deadline = time.time() + 40
        while time.time() < deadline:
            try:
                r = ws.call("Runtime.evaluate", {"expression": "document.readyState"}, timeout=5)
                if r.get("result", {}).get("value") == "complete":
                    loaded["done"] = True
                    break
            except Exception:
                pass
            time.sleep(0.4)
        if not loaded["done"]:
            raise RuntimeError("页面加载超时")
        time.sleep(1.2)

        params = {
            "landscape": False,
            "printBackground": True,
            "preferCSSPageSize": True,
            "marginTop": 0.78, "marginBottom": 0.86,
            "marginLeft": 0.62, "marginRight": 0.62,
            "paperWidth": 8.27, "paperHeight": 11.69,
        }
        if page_numbers:
            params["displayHeaderFooter"] = True
            params["headerTemplate"] = "<div></div>"
            params["footerTemplate"] = (
                '<div style="width:100%;font-size:7.6pt;color:#9d8c78;'
                'padding:0 14mm;display:flex;justify-content:space-between;'
                'font-family:\'Source Han Serif SC\',SimSun,serif;">'
                '<span>求子三要 · 印光大师开示</span>'
                '<span><span class="pageNumber"></span></span></div>'
            )
        else:
            params["displayHeaderFooter"] = False

        result = ws.call("Page.printToPDF", params, timeout=180)
        data = base64.b64decode(result["data"])
        with open(out_path, "wb") as f:
            f.write(data)
        ws.sock.close()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()
        shutil.rmtree(profile, ignore_errors=True)


def print_with_cli(browser, html_path, out_path):
    """回退方案：命令行 headless 打印（无页码）。"""
    cmd = [browser, "--headless=new", "--disable-gpu", "--no-pdf-header-footer",
           "--print-to-pdf=%s" % out_path, "file:///" + html_path.replace("\\", "/")]
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                   timeout=180)


# ------------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-o", "--out", default=DEFAULT_OUT)
    ap.add_argument("--no-pagenum", action="store_true")
    ap.add_argument("--keep-html", action="store_true")
    args = ap.parse_args()

    files = sorted(f for f in os.listdir(TEXT_DIR) if f.endswith(".md"))
    if not files:
        print("text/ 下没有 .md 文件", file=sys.stderr)
        return 1

    parts = []
    for f in files:
        slug = re.sub(r"[^a-zA-Z0-9]", "", os.path.splitext(f)[0]) or "p"
        with open(os.path.join(TEXT_DIR, f), encoding="utf-8") as fh:
            parts.append((slug, md_to_html(fh.read(), slug)))
        print("  · %s" % f)

    toc = build_toc(parts)
    html = render_html(parts, toc)

    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    tmpdir = tempfile.mkdtemp(prefix="qiuzi-")
    html_path = os.path.join(tmpdir, "book.html")
    with open(html_path, "w", encoding="utf-8") as fh:
        fh.write(html)

    browser = find_browser()
    if not browser:
        print("未找到 Chrome / Edge，仅生成 HTML：%s" % html_path, file=sys.stderr)
        return 2
    print("浏览器：%s" % browser)

    try:
        print_with_cdp(browser, html_path, args.out, page_numbers=not args.no_pagenum)
        print("已生成（含页码）：%s" % args.out)
    except Exception as e:
        print("CDP 打印失败（%s），改用命令行打印…" % e, file=sys.stderr)
        print_with_cli(browser, html_path, args.out)
        print("已生成（无页码）：%s" % args.out)

    if args.keep_html:
        keep = os.path.join(os.path.dirname(os.path.abspath(args.out)), "求子三要.html")
        shutil.copy(html_path, keep)
        print("HTML 副本：%s" % keep)
    shutil.rmtree(tmpdir, ignore_errors=True)

    size = os.path.getsize(args.out)
    print("大小：%.1f KB" % (size / 1024.0))
    return 0


if __name__ == "__main__":
    sys.exit(main())
