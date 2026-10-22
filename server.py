"""手机网页：同一个 Wi-Fi 下用手机看所有号的终端、看对话、回复。网址带密钥，没有密钥打不开"""
import base64
import json
import mimetypes
import re
import secrets
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse

import core
import push

PORT = 8765
PREVIEW_DIRS = set()  # 手机点开过网页文件的文件夹，只有这些文件夹里的文件能被打开
# 能发到手机上的文件类型：后缀 → (类型, 卡片上的分类)
SHARE_TYPES = {
    ".mp4": ("video/mp4", "video"), ".mov": ("video/quicktime", "video"), ".m4v": ("video/mp4", "video"),
    ".webm": ("video/webm", "video"), ".mp3": ("audio/mpeg", "audio"), ".wav": ("audio/wav", "audio"),
    ".m4a": ("audio/mp4", "audio"), ".pdf": ("application/pdf", "doc"),
    ".xlsx": ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "doc"),
    ".xls": ("application/vnd.ms-excel", "doc"), ".csv": ("text/csv; charset=utf-8", "doc"),
    ".docx": ("application/vnd.openxmlformats-officedocument.wordprocessingml.document", "doc"),
    ".pptx": ("application/vnd.openxmlformats-officedocument.presentationml.presentation", "doc"),
    ".txt": ("text/plain; charset=utf-8", "doc"), ".md": ("text/plain; charset=utf-8", "doc"),
    ".zip": ("application/zip", "file"), ".psd": ("application/octet-stream", "file"),
}
KEY_FILE = core.DATA_DIR / "mobile-key.txt"
PAGE = Path(__file__).parent / "mobile.html"


def key():
    if not KEY_FILE.exists():
        core.DATA_DIR.mkdir(parents=True, exist_ok=True)
        KEY_FILE.write_text(secrets.token_urlsafe(12), encoding="utf-8")
    return KEY_FILE.read_text(encoding="utf-8").strip()


def lan_ip():
    """本机在家里局域网的地址（跳过代理软件虚拟网卡的 198.18.x 这类地址）"""
    ips = socket.gethostbyname_ex(socket.gethostname())[2]
    lan = [ip for ip in ips if ip.startswith(("192.168.", "10.")) or
           (ip.startswith("172.") and 16 <= int(ip.split(".")[1]) <= 31)]
    return (lan or ips or ["127.0.0.1"])[0]


def url():
    return f"http://{lan_ip()}:{PORT}/?k={key()}"


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        # 图标要允许缓存，iPhone 才肯拿它当桌面图标；其他内容都不缓存
        self.send_header("Cache-Control", "public, max-age=86400" if ctype == "image/png" else "no-store")
        if getattr(self, "_set_cookie", False):
            # 扫码带着密钥进来过一次，就记在手机上；之后从主屏幕图标打开（网址里没密钥）也能进
            self.send_header("Set-Cookie", f"k={key()}; Max-Age=31536000; Path=/; HttpOnly; SameSite=Lax")
        self.end_headers()
        self.wfile.write(data)

    def _authed(self, q, path=""):
        given = (q.get("k") or [""])[0]
        if path.startswith("/app/"):
            given = path[5:].strip("/")
        if path.startswith("/pv/"):  # 网页文件预览：/pv/密钥/文件夹编码/文件
            given = path.split("/")[2]
        if secrets.compare_digest(given, key()):
            self._set_cookie = True
            return True
        cookies = dict(c.strip().split("=", 1) for c in (self.headers.get("Cookie") or "").split(";") if "=" in c)
        return secrets.compare_digest(cookies.get("k", ""), key())

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        # 这两个文件不含任何私密信息，不用密钥（后台推送脚本和桌面图标）
        if u.path == "/sw.js":
            return self._send(200, SW.encode("utf-8"), "text/javascript; charset=utf-8")
        # iPhone 会按不同名字、不同尺寸来找图标，都给它
        sizes = {"/icon.png": 180, "/apple-touch-icon.png": 180, "/apple-touch-icon-precomposed.png": 180,
                 "/apple-touch-icon-180x180.png": 180, "/apple-touch-icon-180x180-precomposed.png": 180,
                 "/favicon.ico": 64, "/app-icon-v3-180.png": 180, "/app-icon-v3-192.png": 192, "/app-icon-v3-512.png": 512}
        if u.path in sizes:
            return self._send(200, _icon_png(sizes[u.path]), "image/png")
        if not self._authed(q, u.path):
            return self._send(403, "<meta name=viewport content='width=device-width'><body style='font:17px -apple-system,sans-serif;padding:40px 24px;color:#333'>"
                                   "<h2>需要重新扫码</h2>打开电脑上的 Remote CC，点「手机」，用相机扫二维码进来一次就好。".encode("utf-8"),
                              "text/html; charset=utf-8")
        try:
            if u.path == "/" or u.path.startswith("/app/"):
                # 桌面图标直接嵌进页面：iPhone「添加到主屏幕」时另外去下载图标常失败，嵌进去就不用下载
                html = PAGE.read_text(encoding="utf-8").replace(
                    "__TOUCH_ICON__", "data:image/png;base64," + base64.b64encode(_icon_png(180)).decode()).replace(
                    "__VERSION__", _version())
                return self._send(200, html.encode("utf-8"), "text/html; charset=utf-8")
            if u.path == "/api/running":
                return self._send(200, core.all_running())
            if u.path == "/api/messages":
                # 手机带上次看到的文件大小来问；没变就只回状态，不重新读整段对话
                sid = q["session"][0]
                f = core.transcript_file(sid)
                size = f.stat().st_size if f else 0
                pid = int((q.get("pid") or ["0"])[0])
                st = next((r["status"] for r in core.list_running() if r["pid"] == pid), "closed") if pid else ""
                if str(size) == (q.get("since") or [""])[0]:
                    return self._send(200, {"size": size, "status": st, "pending": core.unseen_sent(sid)})
                msgs = core.read_messages(sid)
                return self._send(200, {"size": size, "status": st, "messages": msgs, "pending": core.unseen_sent(sid, msgs)})
            if u.path == "/manifest.json":
                # 「添加到主屏幕」用：打开时带上密钥
                return self._send(200, {"name": "Remote CC", "short_name": "Remote CC", "display": "standalone",
                                        "start_url": f"/app/{key()}", "scope": "/", "background_color": "#eef0f5",
                                        "theme_color": "#eef0f5", "id": "claude-accounts-mobile",
                                        # 只放 PNG；文件名带版本号，免得 iPhone 沿用以前「没取到」的结果
                                        "icons": [{"src": f"https://{self.headers.get('Host', '')}/app-icon-v3-{n}.png",
                                                   "sizes": f"{n}x{n}", "type": "image/png", "purpose": "any"} for n in (180, 192, 512)]},
                                  "application/manifest+json; charset=utf-8")
            if u.path.startswith("/pv/"):
                return self._preview_file(u.path)
            if u.path == "/api/dl":
                return self._send_file(q["path"][0], (q.get("save") or [""])[0] == "1")
            if u.path == "/api/finfo":
                # 文件卡片要显示的名字和大小
                p = Path(q["path"][0])
                ok = p.suffix.lower() in SHARE_TYPES and p.is_file()
                return self._send(200, {"ok": ok, "name": p.name, "size": p.stat().st_size if ok else 0,
                                        "kind": SHARE_TYPES.get(p.suffix.lower(), ("", ""))[1]})
            if u.path == "/api/pvurl":
                # 回复里提到的网页文件 → 手机能打开的地址；只认 .html，登记一下它所在的文件夹，里面的图片样式才能跟着加载
                p = Path(q["path"][0])
                if p.suffix.lower() not in (".html", ".htm") or not p.is_file():
                    return self._send(404, {"error": "找不到这个网页文件"})
                PREVIEW_DIRS.add(str(p.parent.resolve()).lower())
                return self._send(200, {"url": f"/pv/{key()}/{_enc(str(p.parent))}/{quote(p.name)}", "title": p.name})
            if u.path == "/api/screen":
                i = (q.get("i") or [""])[0]
                return self._send(200, core.screenshot(int(i) if i.isdigit() else None), "image/jpeg")
            if u.path == "/api/term_screen":
                return self._send(200, {"lines": core.read_screen(int(q["pid"][0]))})
            if u.path == "/api/term_state":
                return self._send(200, core.term_state(int(q["pid"][0])))
            if u.path == "/api/monitors":
                return self._send(200, {"count": len(core.monitors())})
            if u.path == "/api/sessions":
                kw = (q.get("q") or [""])[0].strip()
                return self._send(200, core.search_sessions(kw) if kw else core.list_sessions(80))
            if u.path == "/api/stats":
                # 用量账单：每个号每天用掉周额度的几成 + 每个项目每天用了多少 token
                names = {a["id"]: a["name"] for a in core.list_accounts()}
                return self._send(200, {"accounts": core.account_daily(), "names": names, "projects": core.token_stats()[:12]})
            if u.path == "/api/codex/accounts":
                default = core.codex_default()
                return self._send(200, [{**a, "usage": core.codex_usage(a["id"]) if a["logged"] and not a["relay"] else None,
                                         "email": core.codex_email(a), "is_default": a["id"] == default, "note": core.codex_note(a["id"])}
                                        for a in core.codex_accounts()])
            if u.path == "/api/codex":
                hidden = core.codex_hidden()
                return self._send(200, [s for s in core.codex_sessions(12) if s["id"] not in hidden][:8])
            if u.path == "/api/codex/messages":
                sid = q["session"][0]
                f = core._codex_file(sid)
                size = f.stat().st_size if f else 0
                st = next((s["status"] for s in core.codex_sessions(20) if s["id"] == sid), "idle")
                msgs = core.codex_messages(sid)
                pend = core.unseen_sent(sid, msgs)
                if str(size) == (q.get("since") or [""])[0]:
                    return self._send(200, {"size": size, "status": st, "pending": pend})
                return self._send(200, {"size": size, "status": st, "messages": msgs, "pending": pend})
            if u.path == "/api/progress":
                return self._send(200, core.task_progress(q["session"][0]))
            if u.path == "/api/changes":
                return self._send(200, core.file_changes(q["session"][0]))
            if u.path == "/api/snippets":
                return self._send(200, core.snippets())
            if u.path == "/api/quick":
                # 正在问的选择题（快捷回复的其他按钮在手机上按最新消息算）
                sid = (q.get("session") or [""])[0]
                return self._send(200, {"question": core.pending_question(sid) if sid else None})
            if u.path == "/api/version":
                return self._send(200, {"v": _version()})
            if u.path == "/api/push-key":
                return self._send(200, {"key": push.public_key()})
            if u.path == "/api/img":
                # 只能取上传文件夹里的图（名字里不许带路径），给手机显示缩略图
                name = Path(q["name"][0]).name
                f = core.UPLOAD_DIR / name
                if f.is_file():
                    return self._send(200, f.read_bytes(), "image/png" if name.endswith(".png") else "image/jpeg")
                return self._send(404, {"error": "没有这张图"})
            if u.path == "/api/file":
                # Claude 回复里提到 / 打开过的本机图片（只给图片，别的文件一律不给）
                f = core.image_file(q["path"][0])
                if not f:
                    return self._send(404, {"error": "不是图片或者找不到"})
                types = {".png": "image/png", ".gif": "image/gif", ".webp": "image/webp"}
                return self._send(200, f.read_bytes(), types.get(f.suffix.lower(), "image/jpeg"))
            if u.path == "/api/folders":
                return self._send(200, core.recent_folders())
            if u.path == "/api/acc/detail":
                return self._send(200, core.usage_detail(q["acc"][0]))
            if u.path == "/api/accounts":
                # 带上额度（走 2 分钟缓存，不会频繁去查）
                return self._send(200, [{"id": a["id"], "name": a["name"], "plan": a.get("plan", ""), "note": a.get("note", ""),
                                         "is_default": a["is_default"], "usage": core.get_usage(a["id"])}
                                        for a in core.list_accounts()])
        except Exception as e:
            return self._send(500, {"error": str(e)})
        self._send(404, {"error": "没有这个地址"})

    def _send_file(self, path, save):
        """把 Claude 做好的文件（视频、表格、文档…）给手机：只放行常见类型；视频要支持分段，才能边下边播、拖进度条"""
        p = Path(path)
        if p.suffix.lower() not in SHARE_TYPES or not p.is_file():
            return self._send(404, {"error": "不能给这个文件"})
        size = p.stat().st_size
        start, end = 0, size - 1
        rng = re.match(r"bytes=(\d*)-(\d*)", self.headers.get("Range", ""))
        if rng:
            if rng.group(1):
                start = int(rng.group(1))
                end = int(rng.group(2)) if rng.group(2) else end
            else:  # 「最后 N 个字节」
                start = max(0, size - int(rng.group(2)))
        end = min(end, size - 1)
        self.send_response(206 if rng else 200)
        self.send_header("Content-Type", SHARE_TYPES[p.suffix.lower()][0])
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        if rng:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("Content-Disposition", f"{'attachment' if save else 'inline'}; filename*=UTF-8''{quote(p.name)}")
        self.end_headers()
        with p.open("rb") as f:
            f.seek(start)
            left = end - start + 1
            while left > 0:
                chunk = f.read(min(1024 * 1024, left))
                if not chunk:
                    break
                try:
                    self.wfile.write(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    return  # 手机那边拖了进度条 / 关了，正常
                left -= len(chunk)

    def _preview_file(self, path):
        """打开回复里提到的网页文件，连同同一个文件夹里的图片、样式、脚本（只限手机点开过的那些文件夹）"""
        parts = path.split("/", 4)
        if len(parts) < 5:
            return self._send(404, {"error": "地址不对"})
        try:
            base = Path(base64.urlsafe_b64decode(parts[3] + "==").decode("utf-8")).resolve()
        except Exception:
            return self._send(404, {"error": "地址不对"})
        target = (base / unquote(parts[4])).resolve()
        if str(base).lower() not in PREVIEW_DIRS or base not in target.parents or not target.is_file():
            return self._send(404, {"error": "找不到这个文件"})
        ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript", "application/json"):
            ctype += "; charset=utf-8"
        return self._send(200, target.read_bytes(), ctype)

    def do_POST(self):
        u = urlparse(self.path)
        if not self._authed(parse_qs(u.query)):
            return self._send(403, {"error": "没有权限"})
        try:
            raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            if u.path == "/api/stt":
                # 按住说话的录音：存成临时文件 → 转文字 → 删掉
                ext = "webm" if "webm" in self.headers.get("Content-Type", "") else "m4a"
                f = Path(core.save_upload(raw, ext))
                try:
                    return self._send(200, {"text": core.transcribe(f)})
                finally:
                    f.unlink(missing_ok=True)
            if u.path == "/api/upload":
                # 手机传图片：原样存下来，把本机路径还给手机
                ext = {"image/png": "png", "image/heic": "heic", "image/webp": "webp"}.get(self.headers.get("Content-Type", ""), "jpg")
                return self._send(200, {"path": core.save_upload(raw, ext)})
            body = json.loads(raw or b"{}")
            if u.path == "/api/send":
                core.send_input(int(body["pid"]), body.get("text", ""), body.get("keys"))
                if body.get("text"):
                    # 记下来：Claude 忙的时候这句话要等它读到才进对话记录，这期间退出再进也要能看到
                    sid = next((r["session"] for r in core.list_running() + core.codex_terminals() if r["pid"] == int(body["pid"])), None)
                    if sid:
                        core.remember_sent(sid, body["text"])
                return self._send(200, {"ok": True})
            if u.path == "/api/term_set":
                # 切模型 / 思考程度 / 权限模式；切完把新状态带回去
                pid, what, val = int(body["pid"]), body["what"], body["value"]
                {"model": core.set_model, "effort": core.set_effort, "mode": core.set_mode}[what](pid, val)
                time.sleep(0.8)
                return self._send(200, core.term_state(pid))
            if u.path == "/api/preview":
                port = int(body["port"])
                return self._send(200, {"url": core.preview_url(port), "title": core.page_title(port)})
            if u.path == "/api/subscribe":
                push.subscribe(body)
                return self._send(200, {"ok": True})
            if u.path == "/api/default":
                core.set_default(body["acc"])  # Claude 号设为默认
                return self._send(200, {"ok": True})
            if u.path == "/api/codex/default":
                core.codex_set_default(body["acc"])  # Codex 号设为默认
                return self._send(200, {"ok": True})
            if u.path == "/api/codex/login":
                return self._send(200, core.codex_login_start(body["acc"]))
            if u.path == "/api/codex/hide":
                core.codex_hide(body["session"])
                return self._send(200, {"ok": True})
            if u.path == "/api/codex/stop":
                core.codex_stop(body["session"])
                return self._send(200, {"ok": True})
            if u.path == "/api/codex/send":
                core.codex_send(body["session"], body["text"], body.get("cwd"))
                core.remember_sent(body["session"], body["text"])
                return self._send(200, {"ok": True})
            if u.path == "/api/launch":
                # 选的是 Codex 就开 Codex 终端；带 session 就是接着那段历史对话
                if body["acc"].startswith("cx:"):  # Codex 的号，比如 cx:relay、cx:gpt1
                    core.launch_codex(body.get("cwd"), body["acc"][3:])
                else:
                    core.launch_cli(body["acc"], body.get("cwd"), body.get("session"))
                return self._send(200, {"ok": True})
            if u.path == "/api/snippets":
                core.save_snippets([s for s in body.get("items", []) if isinstance(s, str) and s.strip()][:30])
                return self._send(200, {"ok": True})
            # 长按账号卡片的菜单（acc 是 Claude 号的编号，Codex 号前面带 cx:）
            if u.path == "/api/acc/refresh":
                return self._send(200, core.refresh_usage(body["acc"]))
            if u.path == "/api/acc/evacuate":
                return self._send(200, core.evacuate(body["acc"]))
            if u.path == "/api/acc/web":
                core.acc_web(body["acc"])
                return self._send(200, {"ok": True})
            if u.path == "/api/acc/login":
                return self._send(200, core.acc_login(body["acc"]))
            if u.path == "/api/acc/remove":
                core.acc_remove(body["acc"])
                return self._send(200, {"ok": True})
            if u.path == "/api/acc/rename":
                core.acc_rename(body["acc"], body.get("name", "").strip(), body.get("note", "").strip())
                return self._send(200, {"ok": True})
            if u.path == "/api/close":
                core.close_terminal(int(body["pid"]))
                return self._send(200, {"ok": True})
            if u.path == "/api/switch":
                if body["acc"].startswith("cx:"):  # Codex 终端换号：关掉，用另一个号接着聊
                    core.switch_codex(int(body["pid"]), body["acc"][3:])
                    return self._send(200, {"in_place": False})
                return self._send(200, {"in_place": core.switch_running(int(body["pid"]), body["acc"])})
        except Exception as e:
            return self._send(500, {"error": str(e)})
        self._send(404, {"error": "没有这个地址"})


SW = """
self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', e => e.waitUntil(clients.claim()));
self.addEventListener('push', e => {
  const d = e.data ? e.data.json() : {title: 'Claude', body: ''};
  e.waitUntil(self.registration.showNotification(d.title, {body: d.body, tag: d.tag, data: {url: d.url}, icon: '/icon.png'}));
});
self.addEventListener('notificationclick', e => {
  e.notification.close();
  const url = e.notification.data && e.notification.data.url || '/';
  e.waitUntil(clients.matchAll({type: 'window', includeUncontrolled: true}).then(cs => {
    for (const c of cs) { if ('focus' in c) { c.navigate(url); return c.focus(); } }
    return clients.openWindow(url);
  }));
});
"""


def _enc(s):
    return base64.urlsafe_b64encode(s.encode("utf-8")).decode().rstrip("=")


def _version():
    """手机页面的版本号 = 页面文件的修改时间；手机发现变了就自己刷新"""
    return str(int(PAGE.stat().st_mtime))


def _icon_png(size=180):
    """手机主屏幕图标：铺满整块（iPhone 自己切圆角，透明的地方会变黑），白底 + 绿蓝同心双环"""
    import io
    from PIL import Image, ImageDraw
    n, s = 180, 4  # 按 180 的比例画 4 倍大，最后缩放到要的尺寸，边缘更平滑
    img = Image.new("RGB", (n * s, n * s), (255, 255, 255))
    d = ImageDraw.Draw(img)
    c, w = n * s / 2, 17 * s
    for r, color, end in ((56, (48, 209, 88), 150), (32, (10, 132, 255), 60)):
        box = (c - r * s, c - r * s, c + r * s, c + r * s)
        d.arc(box, 0, 360, fill=(234, 235, 239), width=w)
        d.arc(box, -90, end, fill=color, width=w)
        for ang in (-90, end):  # 圆头端点
            import math
            x, y = c + (r * s - w / 2) * math.cos(math.radians(ang)), c + (r * s - w / 2) * math.sin(math.radians(ang))
            d.ellipse((x - w / 2, y - w / 2, x + w / 2, y + w / 2), fill=color)
    buf = io.BytesIO()
    img.resize((size, size), Image.LANCZOS).save(buf, "PNG")
    return buf.getvalue()


def ts_https_url():
    """Tailscale 加密网址（开了 HTTPS 证书才有），比如 https://电脑名.xxx.ts.net/?k=密钥"""
    try:
        import subprocess
        out = subprocess.run([r"C:\Program Files\Tailscale\tailscale.exe", "status", "--json"], capture_output=True,
                             text=True, encoding="utf-8", creationflags=subprocess.CREATE_NO_WINDOW).stdout
        doms = json.loads(out).get("CertDomains") or []
        return f"https://{doms[0]}/app/{key()}" if doms else ""
    except Exception:
        return ""


def start():
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    push.start(key)
    return srv
