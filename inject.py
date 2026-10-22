"""把文字/按键「打」进某个终端里的 claude（模拟键盘输入）。单独进程运行，因为要临时挂到别人的控制台上。

用法：python inject.py <claude进程号> <JSON: {"text": "...", "keys": ["enter"|"esc"|"up"|"down"|"tab"|"shift+tab"|"1".."9"]}>
"""
import ctypes
import json
import sys
from ctypes import wintypes as w

VK = {"enter": (0x0D, "\r"), "esc": (0x1B, "\x1b"), "tab": (0x09, "\t"), "backspace": (0x08, "\b"),
      "up": (0x26, "\0"), "down": (0x28, "\0"), "left": (0x25, "\0"), "right": (0x27, "\0"),
      "shift+tab": (0x09, "\t", 0x0010)}  # 0x0010 = 按着 Shift；Claude 用它轮换权限模式


class KEY_EVENT_RECORD(ctypes.Structure):
    _fields_ = [("bKeyDown", w.BOOL), ("wRepeatCount", w.WORD), ("wVirtualKeyCode", w.WORD),
                ("wVirtualScanCode", w.WORD), ("UnicodeChar", w.WCHAR), ("dwControlKeyState", w.DWORD)]


class INPUT_RECORD(ctypes.Structure):
    class _U(ctypes.Union):
        _fields_ = [("KeyEvent", KEY_EVENT_RECORD), ("_pad", ctypes.c_byte * 16)]
    _fields_ = [("EventType", w.WORD), ("Event", _U)]


def _records(vk, ch, state=0):
    out = []
    for down in (True, False):
        r = INPUT_RECORD()
        r.EventType = 1  # KEY_EVENT
        r.Event.KeyEvent = KEY_EVENT_RECORD(down, 1, vk, 0, ch, state)
        out.append(r)
    return out


def write(pid, text="", keys=()):
    """挂到 pid 所在的控制台上写入按键。调用方自己不能有控制台（管理器用 pythonw 跑，正好没有）"""
    k = ctypes.windll.kernel32
    k.CreateFileW.restype = w.HANDLE
    k.GetStdHandle.restype = w.HANDLE
    k.SetStdHandle.argtypes = [w.DWORD, w.HANDLE]
    # 挂到别人的控制台会顺手改掉本进程的标准输入输出；先记下来，断开后原样放回，
    # 不然之后再开子进程会拿到失效的句柄（WinError 6 句柄无效）
    std = {n: k.GetStdHandle(n) for n in (-10, -11, -12)}
    k.FreeConsole()
    if not k.AttachConsole(pid):
        for n, h in std.items():
            k.SetStdHandle(n, h)
        raise RuntimeError(f"挂不上这个终端（错误 {k.GetLastError()}）")
    try:
        h = k.CreateFileW("CONIN$", 0xC0000000, 3, None, 3, 0, None)  # 读写、共享读写、打开已有
        recs = []
        for ch in text:
            recs += _records(0, ch)
        for key in keys:
            recs += _records(*VK[key]) if key in VK else _records(0, key)
        if recs:
            arr = (INPUT_RECORD * len(recs))(*recs)
            n = w.DWORD()
            ok = k.WriteConsoleInputW(h, arr, len(recs), ctypes.byref(n))
            if not ok:
                raise RuntimeError(f"写入失败（错误 {k.GetLastError()}）")
        k.CloseHandle(h)
    finally:
        k.FreeConsole()
        for n, h in std.items():
            k.SetStdHandle(n, h)


class COORD(ctypes.Structure):
    _fields_ = [("X", w.SHORT), ("Y", w.SHORT)]


class SMALL_RECT(ctypes.Structure):
    _fields_ = [("L", w.SHORT), ("T", w.SHORT), ("R", w.SHORT), ("B", w.SHORT)]


class CSBI(ctypes.Structure):
    _fields_ = [("size", COORD), ("cur", COORD), ("attr", w.WORD), ("win", SMALL_RECT), ("max", COORD)]


def read_screen(pid):
    """读终端窗口上现在显示的文字（选择框、提示这些不会进对话记录，只能这样看）"""
    k = ctypes.windll.kernel32
    k.CreateFileW.restype = w.HANDLE
    k.GetStdHandle.restype = w.HANDLE
    k.SetStdHandle.argtypes = [w.DWORD, w.HANDLE]
    std = {n: k.GetStdHandle(n) for n in (-10, -11, -12)}
    k.FreeConsole()
    try:
        if not k.AttachConsole(pid):
            raise RuntimeError(f"挂不上这个终端（错误 {k.GetLastError()}）")
        h = k.CreateFileW("CONOUT$", 0xC0000000, 3, None, 3, 0, None)
        info = CSBI()
        k.GetConsoleScreenBufferInfo(h, ctypes.byref(info))
        lines = []
        for y in range(info.win.T, info.win.B + 1):
            buf = ctypes.create_unicode_buffer(info.size.X + 1)
            n = w.DWORD()
            k.ReadConsoleOutputCharacterW(h, buf, info.size.X, COORD(0, y), ctypes.byref(n))
            lines.append(buf.value.rstrip())
        k.CloseHandle(h)
        return lines
    finally:
        k.FreeConsole()
        for n, h in std.items():
            k.SetStdHandle(n, h)


def main():
    spec = json.loads(sys.argv[2])
    try:
        write(int(sys.argv[1]), spec.get("text", ""), spec.get("keys", []))
    except RuntimeError as e:
        sys.exit(str(e))


if __name__ == "__main__":
    main()
