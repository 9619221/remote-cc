"""Remote CC - 桌面窗口 + 系统托盘"""
import ctypes
import json
import socket
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pystray
import webview
from PIL import Image, ImageDraw

import core
import server

REFRESH_SECONDS = 5 * 60
ALERT_PERCENT = 90
LABELS = {"five_hour": "5 小时", "seven_day": "每周"}

window = None
icon = None
usage = {}
alerted = set()   # (号, 额度类型, 重置时间)：同一个重置周期只提醒一次
full = set()      # 当前用满的 (号, 额度类型)，重置后提醒恢复
lock = threading.Lock()
quitting = False


def refresh():
    """拉一遍所有号的用量，更新托盘、发提醒、推给窗口"""
    accs = core.list_accounts()
    with ThreadPoolExecutor(8) as ex:
        result = dict(zip([a["id"] for a in accs], ex.map(core.get_usage, [a["id"] for a in accs])))
    with lock:
        usage.clear()
        usage.update(result)
        _check_alerts(accs)
    core.log_usage(result)  # 记一笔，用来算每个号每天用掉多少
    if icon:
        icon.title = _tooltip(accs)[:127]  # Windows 托盘提示最长 127 字
        icon.update_menu()
    if window:
        window.evaluate_js(f"applyUsage({json.dumps(result)})")
    return result


def _check_alerts(accs):
    for a in accs:
        u = usage.get(a["id"]) or {}
        for kind, label in LABELS.items():
            m = u.get(kind) or {}
            p = m.get("percent")
            if p is None:
                continue
            key = (a["id"], kind)
            if p >= 100:
                full.add(key)
            elif key in full:
                full.discard(key)
                _notify(f"Claude · {a['name']} 已恢复可用", f"{label}额度已重置，现在用了 {round(p)}%")
            if p >= ALERT_PERCENT and (a["id"], kind, m.get("resets_at")) not in alerted:
                alerted.add((a["id"], kind, m.get("resets_at")))
                _notify(f"Claude · {a['name']} {label}额度快满了", f"已用 {round(p)}%，{_reset_text(m.get('resets_at'))}")


def _reset_text(iso):
    if not iso:
        return ""
    from datetime import datetime
    d = datetime.fromisoformat(iso).astimezone()
    return f"{d.month}/{d.day} {d:%H:%M} 重置"


def _notify(title, msg):
    if icon:
        icon.notify(msg, title)


def _short(a):
    u = usage.get(a["id"]) or {}
    if u.get("error") or not u:
        return a["name"]
    f = lambda k: f"{round((u.get(k) or {}).get('percent') or 0)}%"
    return f"Claude · {a['name']}  5h {f('five_hour')} · 周 {f('seven_day')}"


def _tooltip(accs):
    return "\n".join(_short(a) for a in accs) or "Remote CC"


def loop():
    while not quitting:
        try:
            refresh()
        except Exception as e:
            print("刷新失败", e)
        # 平时 5 分钟查一次；有号快重置了，就在重置后 15 秒查，别让它重置完还显示 100%
        with lock:
            soon = core.next_reset_in(usage)
        wait = REFRESH_SECONDS if soon is None else int(min(REFRESH_SECONDS, soon + 15))
        for _ in range(max(15, wait)):
            if quitting:
                return
            threading.Event().wait(1)


# ---------- 托盘 ----------

ICON_FILE = Path(__file__).parent / "icon.ico"


def _icon_image(size=256):
    """白底 + 绿/蓝同心双环，和窗口左上角的标志一致"""
    s = size / 32
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((0, 0, size - 1, size - 1), round(8 * s), fill=(255, 255, 255))
    w = max(2, round(4 * s))
    for r, color, end in ((11, (48, 209, 88), 145), (6, (10, 132, 255), 60)):
        box = ((16 - r) * s, (16 - r) * s, (16 + r) * s, (16 + r) * s)
        d.arc(box, 0, 360, fill=(232, 233, 237), width=w)
        d.arc(box, -90, end, fill=color, width=w)
    return img


def _ensure_icon():
    if not ICON_FILE.exists():
        _icon_image().save(ICON_FILE, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])


def _set_window_icon():
    """pywebview 在 Windows 上不管标题栏图标，自己用系统接口设上"""
    u32 = ctypes.windll.user32
    vp = ctypes.c_void_p
    u32.FindWindowW.restype = vp
    u32.LoadImageW.restype = vp
    u32.LoadImageW.argtypes = [vp, ctypes.c_wchar_p, ctypes.c_uint, ctypes.c_int, ctypes.c_int, ctypes.c_uint]
    u32.SendMessageW.argtypes = [vp, ctypes.c_uint, vp, vp]
    for _ in range(50):
        hwnd = u32.FindWindowW(None, "Remote CC")
        if hwnd:
            break
        threading.Event().wait(0.1)
    for which, px in ((0, 16), (1, 32)):  # ICON_SMALL / ICON_BIG
        h = u32.LoadImageW(None, str(ICON_FILE), 1, px, px, 0x10)  # IMAGE_ICON, LR_LOADFROMFILE
        u32.SendMessageW(hwnd, 0x80, which, h)  # WM_SETICON


def _bind(fn, acc_id):
    # pystray 按参数个数决定怎么调用，所以包成无参函数
    return lambda: fn(acc_id)


def _menu():
    with lock:
        accs = core.list_accounts()
        items = []
        for a in accs:
            label = ("★ " if a["is_default"] else "") + _short(a)
            items.append(pystray.MenuItem(label, pystray.Menu(
                pystray.MenuItem("开终端", _bind(core.launch_cli, a["id"])),
                pystray.MenuItem("开网页", _bind(core.open_web, a["id"])),
                pystray.MenuItem("设为默认", _bind(_set_default, a["id"])),
            )))
    return items + [
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("打开主窗口", lambda: _show(), default=True),
        pystray.MenuItem("刷新用量", lambda: threading.Thread(target=refresh, daemon=True).start()),
        pystray.MenuItem("退出", lambda: _quit()),
    ]


def _set_default(acc_id):
    core.set_default(acc_id)
    icon.update_menu()
    if window:
        window.evaluate_js("load()")


def _show():
    window.show()
    window.restore()


def _quit():
    global quitting
    quitting = True
    icon.stop()
    window.destroy()


def _on_closing():
    # 点 ✕ 只缩到托盘，从托盘「退出」才真退出
    if quitting:
        return True
    window.hide()
    return False


# ---------- 给网页调用的接口 ----------

class Api:
    def list_accounts(self):
        return core.list_accounts()

    def cached_usage(self):
        with lock:
            return dict(usage)

    def usages(self):
        return refresh()

    def add_account(self, name, group, note):
        return core.add_account(name, group, note)

    def update_account(self, acc_id, name, group, note):
        core.update_account(acc_id, name=name, group=group, note=note)

    def remove_account(self, acc_id):
        core.remove_account(acc_id)
        icon.update_menu()

    def set_default(self, acc_id):
        core.set_default(acc_id)
        icon.update_menu()

    def launch_cli(self, acc_id, cwd=None):
        core.launch_cli(acc_id, cwd)

    def recent_folders(self):
        return core.recent_folders()

    def open_web(self, acc_id):
        core.open_web(acc_id)

    def open_web_usage(self, acc_id):
        core.open_web(acc_id, "https://claude.ai/settings/usage")

    def list_sessions(self):
        return core.list_sessions()

    def resume(self, acc_id, session_id, cwd):
        core.launch_cli(acc_id, cwd, session_id)

    def stats(self):
        names = {a["id"]: a["name"] for a in core.list_accounts()}
        return {"accounts": core.account_daily(), "names": names, "projects": core.token_stats()[:12]}

    def list_running(self):
        return core.all_running()

    def switch_running(self, pid, acc_id):
        return core.switch_running(pid, acc_id)

    def close_terminal(self, pid):
        core.close_terminal(pid)

    def codex_accounts(self):
        default = core.codex_default()
        return [{**a, "usage": core.codex_usage(a["id"]) if a["logged"] and not a["relay"] else None,
                 "email": core.codex_email(a), "is_default": a["id"] == default}
                for a in core.codex_accounts()]

    def codex_add(self, name):
        return core.add_codex_account(name)

    def codex_rename(self, acc_id, name):
        core.rename_codex_account(acc_id, name)

    def codex_remove(self, acc_id):
        core.remove_codex_account(acc_id)

    def codex_set_default(self, acc_id):
        core.codex_set_default(acc_id)

    def codex_web_usage(self, acc_id):
        core.open_codex_web(acc_id, "https://chatgpt.com/codex/settings/usage")

    def codex_login(self, acc_id):
        return core.codex_login_desktop(acc_id)

    def codex_web(self, acc_id):
        core.open_codex_web(acc_id)

    def launch_codex(self, acc_id, cwd=None):
        core.launch_codex(cwd, acc_id)

    def switch_codex(self, pid, acc_id):
        core.switch_codex(pid, acc_id)

    def firewall_ok(self):
        return core.firewall_ok()

    def firewall_allow(self):
        return core.firewall_allow(server.PORT)

    def mobile_info(self):
        """手机网址 + 二维码（SVG），给主窗口的「手机」弹窗用"""
        import io
        import qrcode
        import qrcode.image.svg
        u = server.url()
        tail = u.split(":" + str(server.PORT))[1]
        # 优先用 Tailscale 加密网址（手机通知要求加密网址），没开就退回 Tailscale 普通地址
        ts = server.ts_https_url() or next((f"http://{ip}:{server.PORT}{tail}" for ip in socket.gethostbyname_ex(socket.gethostname())[2]
                                            if ip.startswith("100.")), "")
        # 有 Tailscale 就让二维码走它：在家在外都能开，也不怕手机和电脑不在同一个网段
        buf = io.BytesIO()
        qrcode.make(ts or u, image_factory=qrcode.image.svg.SvgPathImage, box_size=8, border=1).save(buf)
        return {"url": u, "qr": buf.getvalue().decode("utf-8"), "tailscale": ts}


def _on_started():
    _set_window_icon()
    icon.run_detached()
    server.start()
    threading.Thread(target=loop, daemon=True).start()


if __name__ == "__main__":
    # 让任务栏把它当成独立程序，不跟 Python 归到一组、用 Python 图标
    ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("claude.accounts.manager")
    _ensure_icon()
    icon = pystray.Icon("remote-cc", _icon_image(64), "Remote CC", pystray.Menu(_menu))
    html = (Path(__file__).parent / "ui.html").read_text(encoding="utf-8")
    # 开机自启时带 --tray：不弹窗口，直接缩在托盘里
    window = webview.create_window("Remote CC", html=html, js_api=Api(), hidden="--tray" in sys.argv,
                                   width=880, height=760, min_size=(560, 460), background_color="#eef0f5")
    window.events.closing += _on_closing
    webview.start(_on_started)
