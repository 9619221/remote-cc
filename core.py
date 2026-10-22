"""Remote CC - 核心逻辑（账号存储、启动终端、打开网页）"""
import ctypes
import json
import os
import re
import shutil
import subprocess
import threading
import time
import urllib.request
import uuid
import winreg
from pathlib import Path

HOME = Path.home()
MAIN_CONFIG = HOME / ".claude"
DATA_DIR = HOME / ".claude-accounts"
STORE = DATA_DIR / "accounts.json"

# 主号里这些东西以符号链接共享给其他号：全局指令、设置、技能、命令、插件、记忆/会话
SHARED = ["CLAUDE.md", "settings.json", "statusline.ps1", "skills", "commands", "agents", "plugins", "projects"]

CHROME_CANDIDATES = [
    Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Google/Chrome/Application/chrome.exe",
    Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "Google/Chrome/Application/chrome.exe",
    Path(os.environ.get("LOCALAPPDATA", "")) / "Google/Chrome/Application/chrome.exe",
]


def _load():
    if STORE.exists():
        return json.loads(STORE.read_text(encoding="utf-8"))
    return {"accounts": [{"id": "main", "name": "主号", "group": "", "note": ""}]}


def _save(data):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STORE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(STORE)


def config_dir(acc_id):
    return MAIN_CONFIG if acc_id == "main" else DATA_DIR / acc_id / "config"


def chrome_dir(acc_id):
    return DATA_DIR / acc_id / "chrome"


def _identity(acc_id):
    """从该号的 Claude Code 资料里读登录邮箱和套餐，没登录返回空"""
    cfg = config_dir(acc_id)
    profile = HOME / ".claude.json" if acc_id == "main" else cfg / ".claude.json"
    info = {"email": "", "org": "", "plan": ""}
    try:
        o = json.loads(profile.read_text(encoding="utf-8")).get("oauthAccount") or {}
        info["email"] = o.get("emailAddress", "")
        info["org"] = o.get("organizationName", "")
    except (OSError, ValueError):
        pass
    try:
        c = json.loads((cfg / ".credentials.json").read_text(encoding="utf-8"))["claudeAiOauth"]
        info["plan"] = c.get("subscriptionType", "")
    except (OSError, ValueError, KeyError):
        pass
    return info


def list_accounts():
    default = get_default()
    return [{**a, **_identity(a["id"]), "is_default": a["id"] == default} for a in _load()["accounts"]]


def get_default():
    """默认号 = 用户级环境变量 CLAUDE_CONFIG_DIR 指向的号；没设就是主号"""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as k:
            val = winreg.QueryValueEx(k, "CLAUDE_CONFIG_DIR")[0]
    except OSError:
        return "main"
    for a in _load()["accounts"]:
        if a["id"] != "main" and Path(val) == config_dir(a["id"]):
            return a["id"]
    return "main"


def set_default(acc_id):
    """设成默认后，任何地方新开的 claude 都用这个号（已开着的终端/VS Code 要重开）"""
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_SET_VALUE) as k:
        if acc_id == "main":
            try:
                winreg.DeleteValue(k, "CLAUDE_CONFIG_DIR")
            except FileNotFoundError:
                pass
        else:
            winreg.SetValueEx(k, "CLAUDE_CONFIG_DIR", 0, winreg.REG_SZ, str(config_dir(acc_id)))
    # 通知资源管理器等程序环境变量变了，之后新开的程序才能读到
    ctypes.windll.user32.SendMessageTimeoutW(0xFFFF, 0x001A, 0, "Environment", 0x0002, 5000, None)


def add_account(name, group="", note=""):
    acc_id = re.sub(r"[^a-z0-9]", "", name.lower())[:16] or "acc"
    acc_id = f"{acc_id}-{uuid.uuid4().hex[:6]}"
    cfg = config_dir(acc_id)
    cfg.mkdir(parents=True, exist_ok=True)
    for item in SHARED:
        src, dst = MAIN_CONFIG / item, cfg / item
        if src.exists() and not dst.exists():
            os.symlink(src, dst, target_is_directory=src.is_dir())
    data = _load()
    data["accounts"].append({"id": acc_id, "name": name, "group": group, "note": note})
    _save(data)
    return acc_id


def update_account(acc_id, **fields):
    data = _load()
    for a in data["accounts"]:
        if a["id"] == acc_id:
            a.update({k: v for k, v in fields.items() if k in ("name", "group", "note")})
    _save(data)


def remove_account(acc_id):
    if acc_id == "main":
        raise ValueError("主号不能删除")
    if get_default() == acc_id:
        set_default("main")
    data = _load()
    data["accounts"] = [a for a in data["accounts"] if a["id"] != acc_id]
    _save(data)
    # 先拆掉共享链接，避免 rmtree 顺着链接删到主号的东西
    cfg = config_dir(acc_id)
    for item in SHARED:
        p = cfg / item
        if p.is_symlink():
            p.rmdir() if p.is_dir() else p.unlink()
    shutil.rmtree(DATA_DIR / acc_id, ignore_errors=True)


def _name(acc_id):
    return next((a["name"] for a in _load()["accounts"] if a["id"] == acc_id), acc_id)


WRAPPER = Path(__file__).parent / "wrapper.ps1"
RUN_DIR = DATA_DIR / "run"


def _launch_args(acc_id, resume=None):
    """守护脚本需要的参数：用哪个号的资料、哪个浏览器、窗口标题、接哪段对话"""
    return {
        "config_dir": "" if acc_id == "main" else str(config_dir(acc_id)),
        # 登录授权页等链接改在这个号自己的 Chrome 里打开，免得在平时浏览器里授权成别的号
        "browser": str(_browser_cmd(acc_id)),
        "title": f"Claude · {_name(acc_id)}",
        "resume": resume or "",
    }


def launch_cli(acc_id, cwd=None, resume=None):
    """开一个 Windows 终端窗口，用这个号跑 claude；给了 resume 就接着那段对话"""
    cwd = cwd if cwd and Path(cwd).is_dir() else str(HOME)
    a = _launch_args(acc_id, resume)
    # 去掉从上层 Claude 会话继承来的标记和 Key，免得新窗口被当成子会话或被 Key 盖住登录
    env = {k: v for k, v in os.environ.items()
           if not (k.startswith("CLAUDE") or k == "ANTHROPIC_API_KEY" or k == "NO_COLOR")}
    subprocess.Popen(
        ["wt.exe", "-w", "new", "new-tab", "-d", cwd,
         "powershell", "-NoExit", "-NoLogo", "-ExecutionPolicy", "Bypass", "-File", str(WRAPPER),
         # 空参数经过 wt 会被吞掉，所以空值传 none，守护脚本里再当成空
         "-ConfigDir", a["config_dir"] or "none", "-Browser", a["browser"], "-Title", a["title"], "-Resume", a["resume"] or "none"],
        env=env, creationflags=subprocess.DETACHED_PROCESS,
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


class _PE(ctypes.Structure):
    _fields_ = [("dwSize", ctypes.c_ulong), ("cntUsage", ctypes.c_ulong), ("th32ProcessID", ctypes.c_ulong),
                ("th32DefaultHeapID", ctypes.c_void_p), ("th32ModuleID", ctypes.c_ulong), ("cntThreads", ctypes.c_ulong),
                ("th32ParentProcessID", ctypes.c_ulong), ("pcPriClassBase", ctypes.c_long), ("dwFlags", ctypes.c_ulong),
                ("szExeFile", ctypes.c_wchar * 260)]


def _procs():
    """本机所有进程：进程号 -> (父进程号, 程序名)"""
    k = ctypes.windll.kernel32
    k.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
    snap = k.CreateToolhelp32Snapshot(2, 0)
    e = _PE()
    e.dwSize = ctypes.sizeof(_PE)
    out = {}
    ok = k.Process32FirstW(ctypes.c_void_p(snap), ctypes.byref(e))
    while ok:
        out[e.th32ProcessID] = (e.th32ParentProcessID, e.szExeFile.lower())
        ok = k.Process32NextW(ctypes.c_void_p(snap), ctypes.byref(e))
    k.CloseHandle(ctypes.c_void_p(snap))
    return out


def _session_title(session_id):
    for f in (MAIN_CONFIG / "projects").glob(f"*/{session_id}.jsonl"):
        try:
            with f.open(encoding="utf-8", errors="replace") as fh:
                for i, line in enumerate(fh):
                    if i > 80:
                        break
                    try:
                        d = json.loads(line)
                    except ValueError:
                        continue
                    if d.get("type") == "user" and not d.get("isMeta"):
                        t = _first_prompt((d.get("message") or {}).get("content"))
                        if t:
                            return t[:200]
        except OSError:
            pass
    return ""


_resumed = {}  # 守护窗口（shell 进程号）→ 换号后接着的那段对话编号
_resume_cmd = {}


def _resume_arg(shell_pid):
    """新开窗口接着聊时，窗口的启动命令里写着 -Resume <对话编号>，读出来（问一次记住）"""
    if shell_pid not in _resume_cmd:
        out = subprocess.run(["powershell", "-NoProfile", "-Command",
                              f"(Get-CimInstance Win32_Process -Filter 'ProcessId={int(shell_pid)}').CommandLine"],
                             capture_output=True, text=True, encoding="utf-8", errors="replace", stdin=subprocess.DEVNULL,
                             creationflags=subprocess.CREATE_NO_WINDOW).stdout
        m = re.search(r"-(?:-r|R)esume\s+([0-9a-f-]{36})", out)
        _resume_cmd[shell_pid] = m.group(1) if m else None
        _resume_cmd[("cmd", shell_pid)] = out  # 整条命令也留着，看是哪个号开的
    return _resume_cmd[shell_pid]


def list_running():
    """正在开着的 Claude 终端（每个号各自的运行记录里找，再核对进程还在）"""
    procs = _procs()
    out = []
    for a in _load()["accounts"]:
        for f in (config_dir(a["id"]) / "sessions").glob("*.json"):
            try:
                d = json.loads(f.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            pid = d.get("pid")
            if d.get("kind") != "interactive" or procs.get(pid, (0, ""))[1] != "claude.exe":
                continue
            ppid = procs[pid][0]
            sid = d.get("sessionId")
            # 刚换号 / 刚接着聊时，头几秒报的是个临时的新编号，还没有对话记录；这时用换号时记下的那段对话
            if not sid or not transcript_file(sid):
                sid = _resumed.get(ppid) or _resume_arg(ppid) or sid
            out.append({
                "pid": pid, "acc": a["id"], "acc_name": "Claude · " + a["name"], "session": sid,
                "cwd": d.get("cwd"), "status": d.get("status", ""), "title": _session_title(sid),
                # 父进程是管理器开的守护脚本 -> 能在原窗口换号
                "in_place": (RUN_DIR / f"{ppid}.wrap").exists() and ppid in procs,
            })
    # 管理器开的窗口里有 Claude 在跑、但还没登记的（停在「信任这个文件夹吗」这类启动提示上，点掉之前不会登记）
    seen = {r["pid"] for r in out}
    for pid, (ppid, exe) in procs.items():
        if exe != "claude.exe" or pid in seen or not (RUN_DIR / f"{ppid}.wrap").exists():
            continue
        acc = _wrapper_acc(ppid)
        sid = _resumed.get(ppid) or _resume_arg(ppid)
        out.append({"pid": pid, "acc": acc["id"], "acc_name": "Claude · " + acc["name"], "session": sid, "cwd": "",
                    "status": "waiting", "title": (_session_title(sid) if sid else "") or "（启动中，等你确认）",
                    "in_place": True, "starting": True})
    for r in out:
        f = transcript_file(r["session"]) if r["session"] else None
        r["active"] = f.stat().st_mtime if f and f.exists() else time.time()
    return sorted(out, key=lambda r: r["pid"])


def _wrapper_acc(shell_pid):
    """守护窗口是用哪个号开的：启动命令里 -ConfigDir 指向那个号的资料文件夹（none = 主号）"""
    _resume_arg(shell_pid)  # 顺便把命令行读进缓存
    cmd = _resume_cmd.get(("cmd", shell_pid), "")
    m = re.search(r"-ConfigDir\s+\"?([^\"\s]+)", cmd)
    accs = _load()["accounts"]
    if m and m.group(1) != "none":
        hit = next((a for a in accs if a["id"] != "main" and Path(m.group(1)) == config_dir(a["id"])), None)
        if hit:
            return hit
    return next(a for a in accs if a["id"] == "main")


def _copy_trust(from_acc, to_acc, cwd):
    """换号时，原来的号信任过这个文件夹，就帮新号也标上信任，免得一换号就弹「信任这个文件夹吗」"""
    def prof(acc):
        return HOME / ".claude.json" if acc == "main" else config_dir(acc) / ".claude.json"
    # Claude 记文件夹用正斜杠（C:/Users/你的用户名），也有反斜杠的老记录：比较时统一写法
    norm = lambda s: s.replace("\\", "/").rstrip("/").lower()
    try:
        src = json.loads(prof(from_acc).read_text(encoding="utf-8"))
        if not any(norm(k) == norm(cwd) and (v or {}).get("hasTrustDialogAccepted") for k, v in (src.get("projects") or {}).items()):
            return
        p = prof(to_acc)
        dst = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
        projects = dst.setdefault("projects", {})
        key = next((k for k in projects if norm(k) == norm(cwd)), cwd.replace("\\", "/"))
        proj = projects.setdefault(key, {})
        if not proj.get("hasTrustDialogAccepted"):
            proj["hasTrustDialogAccepted"] = True
            p.write_text(json.dumps(dst, ensure_ascii=False, indent=2), encoding="utf-8")
    except (OSError, ValueError):
        pass


def _summarize_tool(b):
    """工具调用压成一行，比如「Bash: git status」"""
    inp = b.get("input") or {}
    detail = inp.get("command") or inp.get("file_path") or inp.get("pattern") or inp.get("description") or inp.get("url") or ""
    return f"{b.get('name', '工具')}: {str(detail)[:120]}"


IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".webp")


def image_file(path):
    """手机要看的本机图片：只放行图片格式、确实存在、不太大的文件"""
    p = Path(path)
    if p.suffix.lower() in IMAGE_EXTS and p.is_file() and p.stat().st_size < 30 * 1024 * 1024:
        return p
    return None


_transcripts = {}


def transcript_file(session_id):
    """对话记录文件的位置（找过一次就记住，不用每次都翻所有项目文件夹）"""
    f = _transcripts.get(session_id)
    if not f or not f.exists():
        f = next((MAIN_CONFIG / "projects").glob(f"*/{session_id}.jsonl"), None)
        _transcripts[session_id] = f
    return f


def read_messages(session_id, limit=60):
    """读一段对话最近的内容，给手机页看：用户说的、Claude 回的、调用了哪些工具"""
    f = transcript_file(session_id)
    if not f:
        return []
    out = []
    with f.open("rb") as raw:
        # 长对话的记录文件能有几十 MB；手机只看最近几十条，只读最后 3 MB 就够
        raw.seek(max(0, f.stat().st_size - 3 * 1024 * 1024))
        if raw.tell():
            raw.readline()  # 跳过被截断的半行
        lines = raw.read().decode("utf-8", errors="replace").splitlines()
    for line in lines:
        try:
            d = json.loads(line)
        except ValueError:
            continue
        t, msg = d.get("type"), d.get("message") or {}
        # Claude 干活时你插进来的话，不记成普通消息，而是一条「排队消息」；也要显示出来，不然退出再进就没了
        a = d.get("attachment") if t == "attachment" else None
        if isinstance(a, dict) and a.get("type") == "queued_command" and (a.get("origin") or {}).get("kind") == "human":
            if a.get("prompt"):
                out.append({"role": "user", "kind": "text", "text": str(a["prompt"])[:4000]})
            continue
        if t not in ("user", "assistant") or d.get("isMeta"):
            continue
        content = msg.get("content")
        blocks = [{"type": "text", "text": content}] if isinstance(content, str) else (content or [])
        for b in blocks:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "text" and b.get("text", "").strip():
                text = b["text"].strip()
                # 你用「! 命令」自己跑的命令：命令显示成你发的话，执行结果收进「执行了 N 个操作」
                if t == "user" and text.startswith("<bash-input>"):
                    cmd = re.sub(r"</?bash-input>", "", text).strip()
                    out.append({"role": "user", "kind": "text", "text": ("! " + cmd)[:4000]})
                    continue
                if t == "user" and text.startswith("<bash-stdout>"):
                    res = re.sub(r"</?bash-(?:stdout|stderr)>", " ", text).strip()
                    out.append({"role": "assistant", "kind": "tool", "text": "命令结果：" + (res[:200] or "（没有输出）")})
                    continue
                if t == "user" and text.startswith("<"):
                    continue  # 命令回显、系统插入的内容
                out.append({"role": t, "kind": "text", "text": text[:4000]})
            elif b.get("type") == "tool_use":
                out.append({"role": "assistant", "kind": "tool", "text": _summarize_tool(b)})
                # Claude 打开看过的图片，手机上也显示出来
                fp = (b.get("input") or {}).get("file_path", "")
                if b.get("name") == "Read" and fp.lower().endswith(IMAGE_EXTS):
                    out.append({"role": "assistant", "kind": "image", "text": fp})
    return out[-limit:]


def _tail_lines(f, size=3 * 1024 * 1024):
    with f.open("rb") as raw:
        raw.seek(max(0, f.stat().st_size - size))
        if raw.tell():
            raw.readline()
        return raw.read().decode("utf-8", errors="replace").splitlines()


def pending_question(session_id):
    """Claude 正在问选择题（还没回答）时，返回问题和选项文字"""
    f = transcript_file(session_id)
    if not f:
        return None
    ask, answered = None, set()
    for line in _tail_lines(f, 512 * 1024):
        if "AskUserQuestion" not in line and '"tool_result"' not in line:
            continue
        try:
            d = json.loads(line)
        except ValueError:
            continue
        for b in (d.get("message") or {}).get("content") or []:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "tool_use" and b.get("name") == "AskUserQuestion":
                ask = b
            elif b.get("type") == "tool_result":
                answered.add(b.get("tool_use_id"))
    if not ask or ask.get("id") in answered:
        return None
    qs = (ask.get("input") or {}).get("questions") or []
    return {"id": ask.get("id"), "questions": [
        {"q": q.get("question", ""), "multi": bool(q.get("multiSelect")),
         "options": [o.get("label", "") for o in q.get("options") or []]} for q in qs]}


def monitors():
    """各块屏幕的位置（物理像素），从左到右排"""
    rects = []
    CB = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_long * 4), ctypes.c_void_p)
    ctypes.windll.user32.EnumDisplayMonitors(None, None, CB(lambda h, dc, r, d: rects.append(tuple(r.contents)) or 1), None)
    return sorted(rects)


def screenshot(index=None, max_w=2600):
    """截电脑屏幕给手机看：index 为空截全部屏幕，否则截第几块（从 0 数）；压成 JPEG"""
    import io
    from PIL import ImageGrab
    mons = monitors()
    if index is not None and 0 <= index < len(mons):
        img = ImageGrab.grab(bbox=mons[index], all_screens=True)
    else:
        img = ImageGrab.grab(all_screens=True)
    if img.width > max_w:
        img = img.resize((max_w, round(img.height * max_w / img.width)))
    buf = io.BytesIO()
    img.convert("RGB").save(buf, "JPEG", quality=78)
    return buf.getvalue()


_changes = {}


def file_changes(session_id):
    """这段对话里 Claude 改过的文件：每个文件的每一处改动（删了什么、加了什么），按文件分组"""
    f = transcript_file(session_id)
    if not f:
        return []
    size = f.stat().st_size
    hit = _changes.get(session_id)
    if hit and hit[0] == size:
        return hit[1]
    files = {}
    with f.open(encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if '"tool_use"' not in line or not any(k in line for k in ('"Edit"', '"Write"', '"MultiEdit"')):
                continue
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if d.get("type") != "assistant":
                continue  # 记录里还存着一份请求副本，不跳过的话每处改动会算两遍
            for b in (d.get("message") or {}).get("content") or []:
                if not isinstance(b, dict) or b.get("type") != "tool_use":
                    continue
                name, inp = b.get("name"), b.get("input") or {}
                fp = inp.get("file_path")
                if not fp or name not in ("Edit", "Write", "MultiEdit"):
                    continue
                item = files.setdefault(fp, {"file": fp, "edits": [], "time": d.get("timestamp", "")})
                item["time"] = d.get("timestamp", "") or item["time"]
                if name == "Write":
                    item["edits"].append({"type": "write", "new": (inp.get("content") or "")[:6000]})
                else:
                    for e in inp.get("edits") or [inp]:
                        item["edits"].append({"type": "edit", "old": (e.get("old_string") or "")[:3000],
                                              "new": (e.get("new_string") or "")[:3000]})
    out = sorted(files.values(), key=lambda x: x["time"], reverse=True)
    _changes[session_id] = (size, out)
    return out


TAILSCALE = r"C:\Program Files\Tailscale\tailscale.exe"


def page_title(port):
    """打开这个本地网页读一下标题（比如「亚马逊商品图片生成器」），比端口号好认；不是网页就返回空"""
    try:
        html = urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=1.5).read(20000).decode("utf-8", "replace")
        m = re.search(r"<title[^>]*>(.*?)</title>", html, re.S | re.I)
        return m.group(1).strip() if m else "（没有标题的网页）"
    except Exception:
        return ""


def preview_url(port):
    """让手机能打开 localhost:端口：用 Tailscale 给它套一个只在自己设备间可见的加密网址"""
    st = json.loads(subprocess.run([TAILSCALE, "status", "--json"], capture_output=True, text=True, encoding="utf-8",
                                   stdin=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW).stdout)
    dom = (st.get("CertDomains") or [None])[0]
    if not dom:
        raise RuntimeError("Tailscale 没开加密网址")
    subprocess.run([TAILSCALE, "serve", "--bg", f"--https={int(port)}", f"http://127.0.0.1:{int(port)}"],
                   capture_output=True, stdin=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW)
    return f"https://{dom}:{int(port)}/"


def task_progress(session_id):
    """这段对话里 Claude 列的任务清单进度：一共几项、做完几项、正在做哪项、大概还要多久"""
    f = transcript_file(session_id)
    if not f:
        return None
    size = f.stat().st_size
    hit = _progress.get(session_id)
    if hit and hit[0] == size:  # 对话没变就不重新读（长对话的记录有几十 MB）
        return hit[1]
    res = _task_progress(f)
    _progress[session_id] = (size, res)
    return res


_progress = {}


def _task_progress(f):
    creates, results, updates, todo = {}, {}, [], None
    with f.open(encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if not any(k in line for k in ('"TaskCreate"', '"TaskUpdate"', '"TodoWrite"', '"tool_result"')):
                continue
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if d.get("type") not in ("assistant", "user"):
                continue  # 记录里还存着一份请求副本（没有时间），不算
            ts = d.get("timestamp", "")
            for b in (d.get("message") or {}).get("content") or []:
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "tool_use" and b.get("name") == "TaskCreate":
                    creates[b["id"]] = {"subject": (b.get("input") or {}).get("subject", ""),
                                        "active": (b.get("input") or {}).get("activeForm", ""), "t": ts}
                elif b.get("type") == "tool_use" and b.get("name") == "TaskUpdate":
                    updates.append(((b.get("input") or {}).get("taskId"), (b.get("input") or {}).get("status"), ts))
                elif b.get("type") == "tool_use" and b.get("name") == "TodoWrite":
                    todo = ((b.get("input") or {}).get("todos") or [], ts)
                elif b.get("type") == "tool_result" and b.get("tool_use_id") in creates:
                    c = b.get("content")
                    text = c if isinstance(c, str) else " ".join(x.get("text", "") for x in c or [] if isinstance(x, dict))
                    results[b["tool_use_id"]] = text
    items = {}
    for i, (uid, c) in enumerate(creates.items(), 1):
        m = re.search(r"#(\d+)", results.get(uid, ""))
        items[m.group(1) if m else str(i)] = {**c, "status": "pending", "done_t": ""}
    for tid, st, ts in updates:
        if str(tid) in items and st:
            items[str(tid)]["status"] = st
            if st == "completed":
                items[str(tid)]["done_t"] = ts
    tasks = [t for t in items.values() if t["status"] != "deleted"]
    if not tasks and todo:  # 老版本用的是 TodoWrite：整张清单一次写进去
        tasks = [{"subject": x.get("content", ""), "active": x.get("activeForm", ""), "status": x.get("status", "pending"),
                  "t": todo[1], "done_t": todo[1] if x.get("status") == "completed" else ""} for x in todo[0]]
    if not tasks:
        return None
    done = [t for t in tasks if t["status"] == "completed"]
    cur_t = next((t for t in tasks if t["status"] == "in_progress"), None)
    from datetime import datetime
    parse = lambda s: datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp() if s else None
    start = min(filter(None, (parse(t["t"]) for t in tasks)), default=None)
    last = max(filter(None, (parse(t["done_t"]) for t in done)), default=None)
    eta = None
    if start and last and done and len(done) < len(tasks):
        eta = round((last - start) / len(done) * (len(tasks) - len(done)))  # 按已做完的平均速度估
    return {"total": len(tasks), "done": len(done), "current": (cur_t or {}).get("active") or (cur_t or {}).get("subject", ""),
            "eta": eta, "last_done": last, "items": [{"subject": t["subject"], "status": t["status"]} for t in tasks]}


_stt = {"model": None, "used": 0}
_stt_lock = threading.Lock()


def transcribe(audio_path):
    """手机按住说话的录音 → 文字。用本机显卡跑 faster-whisper large-v3（之前转课程逐字稿用的同一个）"""
    with _stt_lock:
        if _stt["model"] is None:
            # 显卡加速要用的 cublas / cudnn 装在 Python 包里，得先告诉系统去哪找（转课程逐字稿的脚本也是这么做的）
            import nvidia.cublas
            import nvidia.cudnn
            for p in (nvidia.cublas.__path__[0], nvidia.cudnn.__path__[0]):
                d = os.path.join(p, "bin")
                os.add_dll_directory(d)
                os.environ["PATH"] = d + ";" + os.environ["PATH"]
            from faster_whisper import WhisperModel
            _stt["model"] = WhisperModel("large-v3", device="cuda", compute_type="float16")
            threading.Thread(target=_stt_unload, daemon=True).start()
        _stt["used"] = time.time()
        segs, _ = _stt["model"].transcribe(str(audio_path), language="zh", beam_size=5, vad_filter=True,
                                           initial_prompt="以下是普通话的句子，使用简体中文和标点。")
        text = "".join(s.text for s in segs).strip()
        # 中文里冒出来的英文标点换成中文的
        return text.translate(str.maketrans({",": "，", "?": "？", "!": "！", ":": "：", ";": "；"}))


def _stt_unload():
    """10 分钟没人说话就把模型从显卡里放掉，别一直占着显存"""
    while True:
        time.sleep(60)
        with _stt_lock:
            if _stt["model"] is not None and time.time() - _stt["used"] > 600:
                _stt["model"] = None
                import gc
                gc.collect()
                return


_tok_cache = {}  # 文件 → (读到哪了, {消息编号: (日期, 项目, tokens)})


def token_stats(days=7):
    """最近几天每个项目、每天用了多少 token（按对话记录算；同一条回复在记录里会出现好几次，按消息编号去重）"""
    from datetime import datetime, timedelta
    since = (datetime.now() - timedelta(days=days - 1)).strftime("%Y-%m-%d")
    cutoff = time.time() - days * 86400
    out = {}
    for f in (MAIN_CONFIG / "projects").glob("*/*.jsonl"):
        st = f.stat()
        if st.st_mtime < cutoff:
            continue
        pos, msgs = _tok_cache.get(str(f), (0, {}))
        if st.st_size < pos:
            pos, msgs = 0, {}
        if st.st_size > pos:
            with f.open("rb") as fh:
                fh.seek(pos)
                for raw in fh:
                    if not raw.endswith(b"\n"):
                        break  # 正在写的半行，下次再读
                    pos += len(raw)
                    if b'"usage"' not in raw or b'"assistant"' not in raw:
                        continue
                    try:
                        d = json.loads(raw)
                    except ValueError:
                        continue
                    m = d.get("message") or {}
                    u = m.get("usage") or {}
                    if d.get("type") != "assistant" or not m.get("id") or not u:
                        continue
                    ts = d.get("timestamp", "")
                    day = datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone().strftime("%Y-%m-%d") if ts else ""
                    # 新写进去的内容（输入 + 输出 + 新建缓存）；读缓存很便宜，单独算
                    fresh = u.get("input_tokens", 0) + u.get("output_tokens", 0) + u.get("cache_creation_input_tokens", 0)
                    msgs[m["id"]] = (day, d.get("cwd") or "", fresh, u.get("cache_read_input_tokens", 0))
            _tok_cache[str(f)] = (pos, msgs)
        for day, cwd, fresh, cached in msgs.values():
            if day < since or not cwd or "\\Temp\\" in cwd:
                continue
            # 桌面上的项目，子文件夹（比如 神仙开麦\视频）都算到项目本身
            parts = Path(cwd).parts
            if len(parts) > 4 and parts[3].lower() == "desktop":
                cwd = str(Path(*parts[:5]))
            p = out.setdefault(cwd, {"project": cwd, "total": 0, "cached": 0, "days": {}})
            p["total"] += fresh
            p["cached"] += cached
            p["days"][day] = p["days"].get(day, 0) + fresh
    return sorted(out.values(), key=lambda x: x["total"], reverse=True)


USAGE_LOG = DATA_DIR / "usage-history.jsonl"


def log_usage(usage):
    """管理器每 5 分钟查一次额度时顺手记一笔，用来算每个号每天用掉多少（只能从开始记的那天算起）"""
    with USAGE_LOG.open("a", encoding="utf-8") as f:
        for acc, u in usage.items():
            if u and not u.get("error"):
                f.write(json.dumps({"t": int(time.time()), "acc": acc, "w": (u.get("seven_day") or {}).get("percent"),
                                    "h": (u.get("five_hour") or {}).get("percent")}) + "\n")


def account_daily(days=7):
    """每个号每天用掉了周额度的百分之几：把相邻两次记录之间的「上涨」加起来（额度重置时会往下掉，不算）"""
    from datetime import datetime
    rows = []
    try:
        for line in USAGE_LOG.open(encoding="utf-8"):
            try:
                rows.append(json.loads(line))
            except ValueError:
                pass
    except OSError:
        return {}
    cutoff = time.time() - days * 86400
    last, out = {}, {}
    for r in rows:
        if r.get("w") is None:
            continue
        prev = last.get(r["acc"])
        last[r["acc"]] = r["w"]
        if prev is None or r["t"] < cutoff or r["w"] <= prev:
            continue
        day = datetime.fromtimestamp(r["t"]).strftime("%Y-%m-%d")
        out.setdefault(r["acc"], {}).setdefault(day, 0)
        out[r["acc"]][day] += r["w"] - prev
    first = min((r["t"] for r in rows), default=None)
    return {"accounts": {a: {d: round(v, 1) for d, v in ds.items()} for a, ds in out.items()},
            "since": datetime.fromtimestamp(first).strftime("%m-%d %H:%M") if first else ""}


# ---------- Codex（桌面版）：看对话、接着聊 ----------
CODEX_DIR = HOME / ".codex" / "sessions"
_codex_jobs = {}  # 对话编号 → 正在跑的「接着聊」进程


def _codex_exe():
    """Codex 不在系统路径里，在 LOCALAPPDATA\\OpenAI\\Codex\\bin\\<一串编号>\\codex.exe，取最新的那个"""
    exes = sorted(Path(os.environ.get("LOCALAPPDATA", "")).glob("OpenAI/Codex/bin/*/codex.exe"), key=lambda p: p.stat().st_mtime)
    return str(exes[-1]) if exes else None


def _codex_text(content, role):
    parts = [c.get("text", "") for c in content or [] if isinstance(c, dict) and c.get("type") in ("input_text", "output_text")]
    text = "\n".join(parts).strip()
    # Codex 自己塞进对话里的说明（AGENTS.md、环境信息、桌面版上下文）不算你说的话
    if role == "user" and (not text or text.startswith(("<", "# AGENTS.md"))):
        return ""
    # 附了文件时，开头是 Codex 加的「# Files mentioned by the user」清单，真正说的话在「## My request」后面
    if role == "user" and text.startswith("# Files mentioned"):
        m = re.search(r"##\s*My request[^\n]*\n(.*)", text, re.S)
        text = m.group(1).strip() if m else ""
        text = re.sub(r"</?image[^>]*>", "", text).strip()  # 贴图时会夹着 <image …> 标签
        text = "📷 " + (text or "发了一张图")
    return text


def _codex_file(session_id):
    return next(iter(CODEX_DIR.glob(f"*/*/*/rollout-*{session_id}.jsonl")), None)


def codex_sessions(limit=8):
    """最近的 Codex 对话：标题（你说的第一句）、项目文件夹、是不是正在干活"""
    files = sorted(CODEX_DIR.glob("*/*/*/rollout-*.jsonl"), key=lambda f: f.stat().st_mtime, reverse=True)[:limit]
    out = []
    for f in files:
        sid, cwd, title, started, done = f.stem[-36:], "", "", "", ""
        with f.open(encoding="utf-8", errors="replace") as fh:
            for line in fh:
                vague = not title or title == "📷 发了一张图"  # 第一句只贴了图，就用后面说的话当标题
                if vague and '"role":"user"' in line or '"session_meta"' in line or '"task_' in line:
                    try:
                        d = json.loads(line)
                    except ValueError:
                        continue
                    p = d.get("payload") or {}
                    if d.get("type") == "session_meta":
                        cwd, sid = p.get("cwd", ""), p.get("id", sid)
                    elif p.get("type") == "message" and p.get("role") == "user" and vague:
                        title = _codex_text(p.get("content"), "user") or title
                    elif p.get("type") == "task_started":
                        started = d.get("timestamp", "")
                    elif p.get("type") == "task_complete":
                        done = d.get("timestamp", "")
        job = _codex_jobs.get(sid)
        busy = (job is not None and job.poll() is None) or (started > done and time.time() - f.stat().st_mtime < 600)
        out.append({"id": sid, "title": title[:200] or "（新对话）", "cwd": cwd, "mtime": f.stat().st_mtime,
                    "status": "busy" if busy else "idle"})
    return out


def codex_messages(session_id, limit=60):
    """一段 Codex 对话最近的内容：你说的、它回的、它调用了哪些工具"""
    f = _codex_file(session_id)
    if not f:
        return []
    out = []
    for line in _tail_lines(f):
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if d.get("type") != "response_item":
            continue
        p = d["payload"] if isinstance(d.get("payload"), dict) else {}
        if p.get("type") == "message" and p.get("role") in ("user", "assistant"):
            text = _codex_text(p.get("content"), p["role"])
            # 同一句话记录里有时出现两遍，挨着重复的只留一条
            if text and not (out and out[-1]["role"] == p["role"] and out[-1]["text"] == text[:4000]):
                out.append({"role": p["role"], "kind": "text", "text": text[:4000]})
        elif p.get("type") in ("function_call", "custom_tool_call"):
            detail = p.get("input") or ""
            try:
                a = json.loads(p.get("arguments") or "{}")
                detail = a.get("title") or a.get("command") or a.get("code") or detail
            except ValueError:
                pass
            out.append({"role": "assistant", "kind": "tool", "text": f"{p.get('name', '工具')}: {str(detail)[:120]}"})
    return out[-limit:]


CODEX_ACC_DIR = DATA_DIR / "codex"  # 每个 ChatGPT 号一个文件夹（登录信息分开；AGENTS.md、对话记录共用 ~/.codex 的）


def codex_accounts():
    """Codex 的号：原来的 ~/.codex 是第一个（配置里加了自定义服务商就算「中转」，按 Key 扣费；否则是普通 ChatGPT 号），
    其余是在 Remote CC 里另外登录的 ChatGPT 号"""
    home = HOME / ".codex"
    try:
        relay = "[model_providers." in (home / "config.toml").read_text(encoding="utf-8")
    except OSError:
        relay = False
    out = [{"id": "relay", "name": "中转" if relay else "默认", "home": str(home),
            "logged": relay or (home / "auth.json").exists(), "relay": relay}]
    for d in sorted(CODEX_ACC_DIR.glob("*")) if CODEX_ACC_DIR.exists() else []:
        if d.is_dir():
            name = (d / "name.txt").read_text(encoding="utf-8").strip() if (d / "name.txt").exists() else d.name.upper()
            out.append({"id": d.name, "name": name, "home": str(d), "logged": (d / "auth.json").exists(), "relay": False})
    return out


def codex_email(acc):
    """登录的 ChatGPT 邮箱（从登录信息里那张身份凭证读出来）"""
    try:
        tok = (json.loads((Path(acc["home"]) / "auth.json").read_text(encoding="utf-8")).get("tokens") or {}).get("id_token", "")
        import base64
        part = tok.split(".")[1]
        return json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))).get("email", "")
    except Exception:
        return ""


def codex_default():
    """默认的 Codex 号 = 用户级环境变量 CODEX_HOME 指向的那个；没设就是原来的 ~/.codex"""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as k:
            val = winreg.QueryValueEx(k, "CODEX_HOME")[0]
    except OSError:
        return "relay"
    return next((a["id"] for a in codex_accounts() if Path(a["home"]) == Path(val)), "relay")


def codex_set_default(acc_id):
    """设成默认后，任何地方新开的 codex 都用这个号（已开着的终端要重开）"""
    acc = next(a for a in codex_accounts() if a["id"] == acc_id)
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_SET_VALUE) as k:
        if acc["relay"]:
            try:
                winreg.DeleteValue(k, "CODEX_HOME")
            except FileNotFoundError:
                pass
        else:
            winreg.SetValueEx(k, "CODEX_HOME", 0, winreg.REG_SZ, acc["home"])
    ctypes.windll.user32.SendMessageTimeoutW(0xFFFF, 0x001A, 0, "Environment", 0x0002, 5000, None)


def add_codex_account(name):
    """新加一个 ChatGPT 号给 Codex 用：单独一个文件夹放登录信息，AGENTS.md 和对话记录跟原来的共用"""
    acc_id = re.sub(r"[^a-z0-9]", "", name.lower())[:16] or "gpt"
    acc_id = f"{acc_id}-{uuid.uuid4().hex[:4]}"
    home = CODEX_ACC_DIR / acc_id
    home.mkdir(parents=True, exist_ok=True)
    for item in ("AGENTS.md", "sessions"):
        src = HOME / ".codex" / item
        if src.exists() and not (home / item).exists():
            os.symlink(src, home / item, target_is_directory=src.is_dir())
    (home / "name.txt").write_text(name, encoding="utf-8")
    return acc_id


def rename_codex_account(acc_id, name):
    (CODEX_ACC_DIR / acc_id / "name.txt").write_text(name, encoding="utf-8")


def remove_codex_account(acc_id):
    if acc_id == "relay":
        raise ValueError("原来的 Codex 号（~/.codex）不能删")
    if codex_default() == acc_id:
        codex_set_default("relay")
    home = CODEX_ACC_DIR / acc_id
    # 先拆掉共用的链接，免得删的时候顺着链接删到原来的对话记录
    for item in ("AGENTS.md", "sessions"):
        p = home / item
        if p.is_symlink():
            p.rmdir() if p.is_dir() else p.unlink()
    shutil.rmtree(home, ignore_errors=True)


def _codex_home(acc_id):
    return next((a["home"] for a in codex_accounts() if a["id"] == acc_id), str(HOME / ".codex"))


def _codex_cli():
    """终端里用官方命令行版（npm 装的）；没有就用桌面版自带的"""
    npm = Path(os.environ.get("APPDATA", "")) / "npm" / "codex.cmd"
    return str(npm) if npm.exists() else _codex_exe()


def launch_codex(cwd=None, acc_id="relay", resume=None):
    """开一个跑 Codex 的终端窗口；用哪个号（CODEX_HOME 指向它的文件夹）；还没登录的先在终端里走设备码登录"""
    exe = _codex_cli()
    if not exe:
        raise RuntimeError("没找到 Codex")
    acc = next((a for a in codex_accounts() if a["id"] == acc_id), codex_accounts()[0])
    cwd = cwd if cwd and Path(cwd).is_dir() else str(HOME)
    env = {k: v for k, v in os.environ.items()
           if not (k.startswith("CLAUDE") or k == "ANTHROPIC_API_KEY" or k == "NO_COLOR")}
    steps = [] if acc["relay"] else [f"$env:CODEX_HOME='{acc['home']}'"]
    if not acc["logged"]:
        steps.append(f"& '{exe}' login --device-auth")
    steps.append(f"& '{exe}'" + (f" resume {resume}" if resume else ""))
    subprocess.Popen(
        ["wt.exe", "-w", "new", "new-tab", "--title", f"Codex · {acc['name']}", "--suppressApplicationTitle", "-d", cwd,
         # wt 会把裸分号当成「新开标签」，要转义
         "powershell", "-NoExit", "-NoLogo", "-Command", "\\; ".join(steps)],
        env=env, creationflags=subprocess.DETACHED_PROCESS,
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


_shell_cmd = {}  # 终端 shell 的进程号 → 它的命令行（看 CODEX_HOME 是哪个号；命令行不会变，问一次记住）


def _codex_term_acc(shell_pid):
    """看这个 Codex 终端用的是哪个号：往上找几层 shell 的命令行，里面写着 CODEX_HOME 指向哪个号的文件夹
    （官方命令行版是 powershell → cmd(codex.cmd) → node → codex.exe，号写在最外面那层 powershell 上）"""
    if shell_pid not in _shell_cmd:
        procs, chain, p = _procs(), [], shell_pid
        for _ in range(3):
            if procs.get(p, (0, ""))[1] not in ("powershell.exe", "pwsh.exe", "cmd.exe"):
                break
            chain.append(p)
            p = procs[p][0]
        flt = " or ".join(f"ProcessId={int(x)}" for x in chain) or f"ProcessId={int(shell_pid)}"
        out = subprocess.run(["powershell", "-NoProfile", "-Command",
                              f"(Get-CimInstance Win32_Process -Filter '{flt}').CommandLine"],
                             capture_output=True, text=True, encoding="utf-8", errors="replace", stdin=subprocess.DEVNULL,
                             creationflags=subprocess.CREATE_NO_WINDOW).stdout
        _shell_cmd[shell_pid] = out
    m = re.search(r"CODEX_HOME='([^']+)'", _shell_cmd[shell_pid])
    home = m.group(1) if m else str(HOME / ".codex")
    return next((a for a in codex_accounts() if Path(a["home"]) == Path(home)), codex_accounts()[0])


_codex_login = {}  # 号 → 正在等你授权的登录进程


def codex_login_start(acc_id):
    """在后台开始「设备码登录」，把网址和验证码拿出来给手机显示；你在任何设备上授权完，这边就自动登录好"""
    acc = next((a for a in codex_accounts() if a["id"] == acc_id), None)
    if not acc or acc["relay"]:
        raise ValueError("这个号不用登录")
    old = _codex_login.get(acc_id)
    if old and old[0].poll() is None:
        old[0].kill()
    log = Path(acc["home"]) / "login.log"
    env = {**os.environ, "CODEX_HOME": acc["home"], "NO_COLOR": "1"}
    p = subprocess.Popen([_codex_cli(), "login", "--device-auth"], env=env, stdout=log.open("w", encoding="utf-8"),
                         stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW)
    _codex_login[acc_id] = (p, log)
    for _ in range(40):
        time.sleep(0.5)
        text = re.sub(r"\x1b\[[0-9;]*m", "", log.read_text(encoding="utf-8", errors="replace"))
        url = re.search(r"https://auth\.openai\.com/\S+", text)
        code = re.search(r"\b([A-Z0-9]{4}-[A-Z0-9]{4,6})\b", text)
        if url and code:
            return {"url": url.group(0), "code": code.group(1)}
    raise RuntimeError("没拿到验证码，再试一次")


def open_codex_web(acc_id, url="https://chatgpt.com"):
    """用这个 Codex 号自己的 Chrome 打开网页（每个号一个独立的 Chrome，不会串号）"""
    acc = next((a for a in codex_accounts() if a["id"] == acc_id), None)
    if not acc or acc["relay"]:
        raise ValueError("中转号没有网页账号")
    d = Path(acc["home"]) / "chrome"
    d.mkdir(parents=True, exist_ok=True)
    subprocess.Popen([str(_chrome()), f"--user-data-dir={d}", "--no-first-run", "--no-default-browser-check", url],
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def codex_login_desktop(acc_id):
    """电脑上登录 Codex 号：Codex 正常登录一定会弹默认浏览器（可能登错号），所以改走设备码：
    在这个号自己的 Chrome 里打开授权页，验证码复制到剪贴板，粘贴进去就行"""
    r = codex_login_start(acc_id)
    subprocess.run(["clip"], input=r["code"].encode("utf-16-le"), stdin=None, capture_output=True,
                   creationflags=subprocess.CREATE_NO_WINDOW)
    open_codex_web(acc_id, r["url"])
    return r


def codex_usage(acc_id):
    """ChatGPT 号的 Codex 额度（5 小时 / 每周）：读这个号最近的对话记录里 Codex 自己记下的额度；「中转」号按 Key 扣钱，没有额度"""
    acc = next((a for a in codex_accounts() if a["id"] == acc_id), None)
    if not acc or acc["relay"]:
        return {"error": "按 Key 扣费，没有额度限制"}
    files = sorted((Path(acc["home"]) / "sessions").rglob("rollout-*.jsonl"), key=lambda f: f.stat().st_mtime, reverse=True)
    for f in files[:5]:
        for line in reversed(_tail_lines(f, 256 * 1024)):
            if '"rate_limits"' not in line or '"primary":{' not in line:
                continue
            try:
                rl = (json.loads(line).get("payload") or {}).get("rate_limits") or {}
            except ValueError:
                continue
            win = lambda w: _window(w.get("used_percent"), w.get("resets_at")) if w else None
            return {"five_hour": win(rl.get("primary")), "seven_day": win(rl.get("secondary")), "plan": rl.get("plan_type") or ""}
    return {"error": "还没有数据：用这个号跑一次 Codex 就有了"}


def switch_codex(pid, to_acc):
    """Codex 终端换号：关掉这个窗口，用另一个号在同一个文件夹接着这段对话（对话记录是共用的）"""
    r = next((x for x in codex_terminals() if x["pid"] == pid), None)
    if not r:
        raise ValueError("这个终端已经关了")
    close_terminal(pid)
    launch_codex(r["cwd"], to_acc, r["session"])


def _proc_start(pid):
    """进程是什么时候启动的（秒）"""
    k = ctypes.windll.kernel32
    k.OpenProcess.restype = ctypes.c_void_p
    h = k.OpenProcess(0x1000, False, pid)  # 只要「查信息」权限
    if not h:
        return None
    t = [ctypes.c_ulonglong() for _ in range(4)]
    ok = k.GetProcessTimes(ctypes.c_void_p(h), *[ctypes.byref(x) for x in t])
    k.CloseHandle(ctypes.c_void_p(h))
    return t[0].value / 1e7 - 11644473600 if ok else None  # Windows 时间（1601 年起，100 纳秒）→ 普通时间


def codex_terminals():
    """正在终端里跑的 Codex（不算桌面版后台和「接着聊」用的那种），对上它写的对话记录"""
    from datetime import datetime
    procs = _procs()
    shells = ("powershell.exe", "pwsh.exe", "cmd.exe")

    def shell_of(pid):
        # 官方命令行版是 shell → node → codex.exe；桌面版自带的是 shell → codex.exe
        pp = procs.get(pid, (0, ""))[0]
        if procs.get(pp, (0, ""))[1] == "node.exe":
            pp = procs.get(pp, (0, ""))[0]
        return pp if procs.get(pp, (0, ""))[1] in shells else None

    pids = [p for p, (pp, exe) in procs.items() if exe == "codex.exe" and shell_of(p)]
    if not pids:
        return []
    # 终端里跑的 Codex，对话记录开头写着 source=cli；按「进程启动之后最早出现的那段」对上
    metas = []
    for f in sorted(CODEX_DIR.glob("*/*/*/rollout-*.jsonl"), key=lambda f: f.stat().st_mtime, reverse=True)[:30]:
        try:
            with f.open(encoding="utf-8", errors="replace") as fh:
                p = json.loads(fh.readline()).get("payload") or {}
        except (OSError, ValueError):
            continue
        if p.get("source") == "cli":
            metas.append((datetime.fromisoformat(p["timestamp"].replace("Z", "+00:00")).timestamp(), p.get("id"), p.get("cwd", ""), f))
    metas.sort()
    used, out = set(), []
    for pid in sorted(pids, key=lambda p: _proc_start(p) or 0):
        start = _proc_start(pid) or 0
        m = next((x for x in metas if x[0] >= start - 5 and x[1] not in used), None)
        sid, cwd = (m[1], m[2]) if m else (None, "")
        active = m[3].stat().st_mtime if m else start
        if sid:
            used.add(sid)
        info = next((s for s in codex_sessions(30) if s["id"] == sid), None) if sid else None
        acc = _codex_term_acc(shell_of(pid))
        out.append({"pid": pid, "acc": "cx:" + acc["id"], "acc_name": "Codex · " + acc["name"], "kind": "codexterm",
                    "session": sid, "cwd": cwd,
                    "status": (info or {}).get("status", "idle"), "title": (info or {}).get("title", "") if info else "",
                    "in_place": False, "active": active})
    return out


def all_running():
    """Claude 和 Codex 的终端合在一起，最近有动静的排最上面（手机、电脑都用这个顺序）"""
    return sorted(list_running() + codex_terminals(), key=lambda r: -r.get("active", 0))


CODEX_HIDDEN = DATA_DIR / "codex-hidden.json"


def codex_hidden():
    try:
        return set(json.loads(CODEX_HIDDEN.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return set()


def codex_hide(session_id):
    """首页不再显示这段 Codex 对话（只是不显示，记录不删）"""
    CODEX_HIDDEN.write_text(json.dumps(sorted(codex_hidden() | {session_id})), encoding="utf-8")


def codex_stop(session_id):
    """让从手机发出去、正在后台跑的那句停下来"""
    job = _codex_jobs.get(session_id)
    if job is None or job.poll() is not None:
        raise RuntimeError("它现在没在跑")
    subprocess.run(["taskkill", "/PID", str(job.pid), "/T", "/F"], capture_output=True, stdin=subprocess.DEVNULL,
                   creationflags=subprocess.CREATE_NO_WINDOW)


def codex_send(session_id, text, cwd=None):
    """接着某段 Codex 对话说一句：在后台跑 codex exec resume，结果会写回同一段对话"""
    exe = _codex_exe()
    if not exe:
        raise RuntimeError("没找到 Codex")
    job = _codex_jobs.get(session_id)
    if job is not None and job.poll() is None:
        raise RuntimeError("Codex 还在处理上一句，等它做完再发")
    # 桌面版 Codex 的对话很多在 Documents\Codex\日期\… 这种不是代码仓库的文件夹里，不跳过检查会直接拒绝
    args = [exe, "exec", "--skip-git-repo-check", "resume", session_id, text]
    _codex_jobs[session_id] = subprocess.Popen(
        args, cwd=cwd if cwd and Path(cwd).is_dir() else str(HOME),
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW)


SNIPPETS = DATA_DIR / "snippets.json"


def snippets():
    try:
        return json.loads(SNIPPETS.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []


def save_snippets(items):
    SNIPPETS.write_text(json.dumps(items, ensure_ascii=False), encoding="utf-8")


def recent_folders(limit=12):
    """最近用 Claude 干活的文件夹（按最近使用排序），给手机上「新开终端」选"""
    seen, out = set(), []
    for s in list_sessions():
        c = s["cwd"]
        if c.lower() not in seen and Path(c).is_dir():
            seen.add(c.lower())
            out.append(c)
    if str(HOME).lower() not in seen:
        out.append(str(HOME))
    return out[:limit]


_sent = {}  # 对话 → [(时间, 你从手机发的话)]：Claude 还没读到、没进对话记录的那些


def remember_sent(session_id, text):
    _sent.setdefault(session_id, []).append((time.time(), text))


def unseen_sent(session_id, msgs=None):
    """你发了、但 Claude 还没读到的话（进了对话记录的就不算了；超过一天的也丢掉）"""
    items = [(t, x) for t, x in _sent.get(session_id, []) if time.time() - t < 86400]
    if not items:
        return []
    if msgs is None:
        msgs = read_messages(session_id, 120)
    seen = [m["text"] for m in msgs if m["role"] == "user"]
    left = [(t, x) for t, x in items if not any(x[:30] in s for s in seen)]
    _sent[session_id] = left
    return [x for _, x in left]


_inject_lock = threading.Lock()


def send_input(pid, text="", keys=None):
    """把文字和按键打进某个运行中的终端（Claude 或 Codex），相当于在电脑前打字"""
    if _procs().get(pid, (0, ""))[1] not in ("claude.exe", "codex.exe"):
        raise ValueError("这个终端已经关了")
    import inject
    # 管理器本身没有控制台，可以直接挂到那个终端上写，不用另起进程（快零点几秒）；同一时间只挂一个
    with _inject_lock:
        if text:
            inject.write(pid, text)
            # 文字和回车一口气打进去，Claude Code 会当成「粘贴」，回车只算换行不算发送；停一下再按
            if keys:
                time.sleep(0.25 + len(text) / 2000)
        if keys:
            inject.write(pid, "", keys)


def read_screen(pid):
    """终端窗口上现在显示的文字（给手机看选择框用）"""
    if _procs().get(pid, (0, ""))[1] not in ("claude.exe", "codex.exe"):
        raise ValueError("这个终端已经关了")
    import inject
    with _inject_lock:  # 跟打字一样要临时挂到它的控制台上，不能同时挂
        lines = inject.read_screen(pid)
    while lines and not lines[-1].strip():
        lines.pop()
    return lines


# ---------- 终端当前的模型 / 思考程度 / 权限模式：从屏幕最下面的状态栏读 ----------
MODELS = [("claude-opus-5-5", "Opus 5.5"), ("claude-sonnet-5-5", "Sonnet 5.5"),
          ("claude-haiku-5-5", "Haiku 5.5"), ("claude-fable-5-1", "Fable 5.1")]
EFFORTS = ["low", "medium", "high", "xhigh", "max"]
# 屏幕上的写法 → 模式名；一个都没有就是默认的「每次都问」
MODES = [("bypass permissions", "bypassPermissions"), ("auto mode", "auto"),
         ("accept edits", "acceptEdits"), ("plan mode", "plan")]


def term_state(pid):
    tail = read_screen(pid)[-8:]
    text = "\n".join(tail)
    m = re.search(r"((?:Opus|Sonnet|Haiku|Fable)\s+[\d.]+)", text)
    e = re.search(r"effort:\s*(\w+)", text)
    low = text.lower()
    mode = next((v for k, v in MODES if k in low), "manual")
    # 输入框（❯ 那一行）里有字：可能是没发出去的话，也可能只是 Claude 给的灰色建议（读出来分不清）
    return {"model": m.group(1) if m else "", "effort": e.group(1) if e else "", "mode": mode,
            "typed": _box(tail) != "❯"}


def _box(lines):
    return next((l.strip() for l in reversed(lines) if l.strip().startswith("❯")), "❯")


def _ready(pid):
    st = term_state(pid)
    if st["typed"]:
        # 灰色建议字和真输入颜色一样、光标也不准，只能试探：打个空格，建议字会消失、真输入后面多个空格；看完删掉空格
        send_input(pid, " ")
        time.sleep(0.3)
        box = _box(read_screen(pid)[-8:])
        send_input(pid, "", ["backspace"])
        time.sleep(0.2)
        if box != "❯":
            raise ValueError("终端输入框里还有没发出去的字，先在电脑上清空再切")
    return st


def _confirm(pid):
    """对话中途换模型 / 思考程度，Claude 会弹「确定要换吗（整段对话要重读）」；是手机上主动选的，替你点 Yes"""
    for _ in range(6):
        time.sleep(0.4)
        if any("1. Yes" in l for l in read_screen(pid)[-12:]):
            send_input(pid, "", ["enter"])
            return


def set_model(pid, model):
    if model not in dict(MODELS):
        raise ValueError("不认识这个模型")
    _ready(pid)
    send_input(pid, f"/model {model}", ["enter"])
    _confirm(pid)


def set_effort(pid, level):
    if level not in EFFORTS:
        raise ValueError("不认识这个档位")
    _ready(pid)
    send_input(pid, f"/effort {level}", ["enter"])
    _confirm(pid)


def set_mode(pid, mode):
    """权限模式只能按 Shift+Tab 一档档轮换：按一下看一眼屏幕，到了就停（最多转一圈）"""
    if _ready(pid)["mode"] == mode:
        return
    for _ in range(len(MODES) + 2):
        send_input(pid, "", ["shift+tab"])
        time.sleep(0.4)
        if term_state(pid)["mode"] == mode:
            return
    raise ValueError("转了一圈也没切到这个模式（这个终端可能不支持它）")


UPLOAD_DIR = DATA_DIR / "uploads"


def save_upload(data, ext):
    """手机传来的图片存到本机，返回路径（发给 Claude 时带上这个路径，它就能看图）"""
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    f = UPLOAD_DIR / f"{time.strftime('%m%d-%H%M%S')}-{uuid.uuid4().hex[:4]}.{ext}"
    f.write_bytes(data)
    return str(f)


def close_terminal(pid):
    """关掉一个终端：连 Claude 带窗口一起关（对话记录还在，以后能在历史对话里接着聊）。
    直接强杀的话 Windows 终端会留个「进程已退出」的空窗口，所以找到它所在的窗口，让窗口自己关"""
    if _procs().get(pid, (0, ""))[1] not in ("claude.exe", "codex.exe"):
        raise ValueError("这个终端已经关了")
    u, k = ctypes.windll.user32, ctypes.windll.kernel32
    k.GetConsoleWindow.restype = ctypes.c_void_p
    k.GetStdHandle.restype = ctypes.c_void_p
    k.SetStdHandle.argtypes = [ctypes.c_ulong, ctypes.c_void_p]
    u.GetWindow.restype = ctypes.c_void_p
    hwnd = None
    with _inject_lock:  # 和「往终端里打字」一样要临时挂到它的控制台上，不能同时挂
        std = {n: k.GetStdHandle(n) for n in (-10, -11, -12)}
        k.FreeConsole()
        if k.AttachConsole(pid):
            con = k.GetConsoleWindow()
            hwnd = u.GetWindow(ctypes.c_void_p(con), 4) if con else None  # 伪控制台窗口的主人 = 所在的终端窗口
        k.FreeConsole()
        for n, h in std.items():
            k.SetStdHandle(n, h)
    if hwnd:
        u.PostMessageW(ctypes.c_void_p(hwnd), 0x0010, 0, 0)  # WM_CLOSE：像点窗口右上角的 ✕
        for _ in range(30):
            time.sleep(0.1)
            if pid not in _procs():
                return
    # 找不到窗口或者没关掉，就直接结束（连同它所在的 shell）
    shell = _procs().get(pid, (0, ""))[0]
    subprocess.run(["taskkill", "/PID", str(shell or pid), "/T", "/F"], capture_output=True, stdin=subprocess.DEVNULL,
                   creationflags=subprocess.CREATE_NO_WINDOW)


def switch_running(pid, to_acc):
    """把一个运行中的终端换到另一个号，接着同一段对话；各号登录互不影响"""
    r = next((x for x in list_running() if x["pid"] == pid), None)
    if not r:
        raise ValueError("这个终端已经关了")
    if r.get("cwd"):
        _copy_trust(r["acc"], to_acc, r["cwd"])
    if r["in_place"]:
        ppid = _procs()[pid][0]
        _resumed[ppid] = r["session"]  # 记住这个窗口接下来接的是哪段对话
        RUN_DIR.mkdir(parents=True, exist_ok=True)
        (RUN_DIR / f"{ppid}.switch").write_text(
            json.dumps(_launch_args(to_acc, r["session"]), ensure_ascii=False), encoding="utf-8")
    subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, stdin=subprocess.DEVNULL,
                   creationflags=subprocess.CREATE_NO_WINDOW)
    if not r["in_place"]:
        launch_cli(to_acc, r["cwd"], r["session"])
    return r["in_place"]


def _reset_passed(iso):
    if not iso:
        return False
    from datetime import datetime
    try:
        return datetime.fromisoformat(iso).timestamp() < time.time()
    except ValueError:
        return False


def next_reset_in(usage):
    """所有号里最先到的重置还有几秒（没有就返回空）"""
    from datetime import datetime
    ts = []
    for u in usage.values():
        for k in ("five_hour", "seven_day"):
            iso = ((u or {}).get(k) or {}).get("resets_at")
            if iso:
                try:
                    ts.append(datetime.fromisoformat(iso).timestamp() - time.time())
                except ValueError:
                    pass
    future = [t for t in ts if t > 0]
    return min(future) if future else None


def _window(percent, reset):
    """一档额度：重置时间统一成 ISO；已经过了重置时间还没有新数据的，按已重置算 0%"""
    from datetime import datetime, timezone
    iso = datetime.fromtimestamp(reset, timezone.utc).isoformat() if isinstance(reset, (int, float)) else reset
    return {"percent": 0, "resets_at": None} if _reset_passed(iso) else {"percent": percent, "resets_at": iso}


def get_usage(acc_id):
    """这个号的 5 小时 / 每周额度：只用 Claude Code 自己交给状态栏的数据（statusline.ps1 存到 .usage-live.json），
    这个号开着终端时随时更新，不去调任何非公开接口"""
    try:
        v = json.loads((config_dir(acc_id) / ".usage-live.json").read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {"error": "还没有数据：用这个号开一次终端就有了"}
    return {k: _window((v.get(k) or {}).get("used_percentage"), (v.get(k) or {}).get("resets_at"))
            for k in ("five_hour", "seven_day")}


# ---------- Windows 防火墙：手机连不进来多半是它拦了 ----------
FW_RULE = "Remote CC - 手机网页"
# 第一次运行时 Windows 会弹「是否允许访问网络」，点了取消会生成拦 Python 的入站阻止规则，阻止规则比放行规则优先
_FW_BLOCKS = ("Get-NetFirewallApplicationFilter | Where-Object {{ $_.Program -like '{dir}\\*' }} | Get-NetFirewallRule | "
              "Where-Object {{ $_.Direction -eq 'Inbound' -and $_.Action -eq 'Block' }}")


def _ps(script, timeout=60):
    return subprocess.run(["powershell", "-NoProfile", "-Command", script], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", stdin=subprocess.DEVNULL, timeout=timeout,
                          creationflags=subprocess.CREATE_NO_WINDOW).stdout


def firewall_ok():
    """有放行规则、而且没有拦这个 Python 的阻止规则，手机才连得进来"""
    import sys
    blocks = _FW_BLOCKS.format(dir=Path(sys.executable).parent)
    return "ok" in _ps(f"$r = Get-NetFirewallRule -DisplayName '{FW_RULE}' -ErrorAction SilentlyContinue | Where-Object Enabled -eq 'True';"
                       f"$b = {blocks} | Where-Object Enabled -eq 'True';"
                       "if ($r -and -not $b) { 'ok' }")


def firewall_allow(port):
    """弹管理员授权：加一条只放局域网和 Tailscale（100.64.0.0/10）的放行规则，停掉拦这个 Python 的阻止规则"""
    import base64
    import sys
    script = (f"Remove-NetFirewallRule -DisplayName '{FW_RULE}' -ErrorAction SilentlyContinue;"
              f"New-NetFirewallRule -DisplayName '{FW_RULE}' -Direction Inbound -Action Allow -Protocol TCP -LocalPort {int(port)} "
              "-RemoteAddress LocalSubnet,100.64.0.0/10 -Profile Any | Out-Null;"
              + _FW_BLOCKS.format(dir=Path(sys.executable).parent) + " | Disable-NetFirewallRule")
    enc = base64.b64encode(script.encode("utf-16-le")).decode()
    # 用户在授权弹窗里点「否」时 Start-Process 会报错，这里不当回事，最后看规则在不在就知道成没成
    _ps(f"try {{ Start-Process powershell -Verb RunAs -Wait -WindowStyle Hidden -ArgumentList '-NoProfile','-EncodedCommand','{enc}' }} catch {{}}",
        timeout=300)
    return firewall_ok()


def _chrome():
    chrome = next((p for p in CHROME_CANDIDATES if p.exists()), None)
    if not chrome:
        raise FileNotFoundError("没找到 Chrome")
    return chrome


def _browser_cmd(acc_id):
    """生成一个「用这个号的 Chrome 打开网址」的小脚本，给 Claude Code 当浏览器用"""
    d = chrome_dir(acc_id)
    d.mkdir(parents=True, exist_ok=True)
    cmd = DATA_DIR / acc_id / "open-browser.cmd"
    cmd.write_text(f'@start "" "{_chrome()}" --user-data-dir="{d}" --no-first-run '
                   f'--no-default-browser-check %*\r\n', encoding="ascii")
    return cmd


def open_web(acc_id, url="https://claude.ai"):
    """用这个号独立的 Chrome 配置打开 claude.ai，各号互不踢下线"""
    chrome = _chrome()
    d = chrome_dir(acc_id)
    d.mkdir(parents=True, exist_ok=True)
    subprocess.Popen([str(chrome), f"--user-data-dir={d}", "--no-first-run",
                      "--no-default-browser-check", url],
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _first_prompt(text):
    """取一段对话里用户说的第一句话当标题（跳过命令、系统提示这类自动插入的内容）"""
    if isinstance(text, list):
        text = " ".join(b.get("text", "") for b in text if isinstance(b, dict) and b.get("type") == "text")
    text = (text or "").strip()
    return "" if not text or text.startswith("<") else text


def list_sessions(limit=150):
    """列出最近的对话记录。各号共享同一份记录，所以任何号都能接着聊"""
    files = [f for f in (MAIN_CONFIG / "projects").glob("*/*.jsonl")]
    files.sort(key=lambda f: f.stat().st_mtime, reverse=True)
    out = []
    for f in files:
        if len(out) >= limit:
            break
        cwd, title = None, ""
        try:
            with f.open(encoding="utf-8", errors="replace") as fh:
                for i, line in enumerate(fh):
                    if i > 80 or (cwd and title):
                        break
                    try:
                        d = json.loads(line)
                    except ValueError:
                        continue
                    cwd = cwd or d.get("cwd")
                    if not title and d.get("type") == "user" and not d.get("isMeta"):
                        title = _first_prompt((d.get("message") or {}).get("content"))
        except OSError:
            continue
        # 子任务/临时目录里的对话是自动产生的，不列
        if not cwd or not title or "\\Temp\\" in cwd:
            continue
        out.append({"id": f.stem, "cwd": cwd, "title": title[:200], "mtime": f.stat().st_mtime})
    return out


# ---------- 历史对话全文搜索 ----------
# 对话记录几百 MB，每次全翻太慢：只抽出双方说的文字存一份索引，文件没变就不重读
SEARCH_INDEX = DATA_DIR / "search-index.json"
_search_idx = None
_search_lock = threading.Lock()


def _index_file(f):
    cwd, title, parts = None, "", []
    with f.open(encoding="utf-8", errors="replace") as fh:
        for line in fh:
            # 先粗筛，工具输出、附件这些大行不用解析
            if '"type":"user"' not in line and '"type":"assistant"' not in line:
                continue
            try:
                d = json.loads(line)
            except ValueError:
                continue
            cwd = cwd or d.get("cwd")
            if d.get("type") not in ("user", "assistant") or d.get("isMeta"):
                continue
            text = _first_prompt((d.get("message") or {}).get("content"))
            if not text:
                continue
            if d["type"] == "user" and not title:
                title = text
            parts.append(text)
    return {"cwd": cwd, "title": title[:200], "text": "\n".join(parts)}


def _refresh_search_index():
    global _search_idx
    if _search_idx is None:
        try:
            _search_idx = json.loads(SEARCH_INDEX.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            _search_idx = {}
    files = {str(f): f for f in (MAIN_CONFIG / "projects").glob("*/*.jsonl")}
    changed = False
    for k in [k for k in _search_idx if k not in files]:
        del _search_idx[k]
        changed = True
    for k, f in files.items():
        st = f.stat()
        sig = [st.st_size, st.st_mtime]
        old = _search_idx.get(k)
        if old and old["sig"] == sig:
            continue
        try:
            _search_idx[k] = {**_index_file(f), "sig": sig, "id": f.stem}
            changed = True
        except OSError:
            pass
    if changed:
        SEARCH_INDEX.parent.mkdir(parents=True, exist_ok=True)
        SEARCH_INDEX.write_text(json.dumps(_search_idx, ensure_ascii=False), encoding="utf-8")


def search_sessions(q, limit=60):
    """在所有对话的聊天内容里搜，空格分开的几个词都要出现；带回命中的那句原话"""
    words = [w for w in q.lower().split() if w]
    with _search_lock:
        _refresh_search_index()
        items = list(_search_idx.values())
    out = []
    for it in sorted(items, key=lambda x: x["sig"][1], reverse=True):
        cwd, text = it["cwd"], it["text"]
        if not cwd or not it["title"] or "\\Temp\\" in cwd:
            continue
        low = (it["title"] + "\n" + cwd + "\n" + text).lower()
        if not all(w in low for w in words):
            continue
        i = text.lower().find(words[0])
        snip = ""
        if i >= 0:
            s = max(0, i - 30)
            snip = ("…" if s else "") + " ".join(text[s:i + len(words[0]) + 50].split())
        out.append({"id": it["id"], "cwd": cwd, "title": it["title"], "mtime": it["sig"][1], "snip": snip})
        if len(out) >= limit:
            break
    return out


if __name__ == "__main__":
    for a in list_accounts():
        print(a)


# ---------- 手机上长按账号卡片的菜单 ----------
def refresh_usage(acc):
    """马上重读一次额度（数据都在本机，直接重读就是最新的）"""
    return codex_usage(acc[3:]) if acc.startswith("cx:") else get_usage(acc)


def evacuate(acc):
    """把这个号正在跑的终端全部换到额度最宽裕的另一个号；返回换了几个、换到哪"""
    terms = [r for r in all_running() if r["acc"] == acc]
    if not terms:
        raise ValueError("这个号现在没有开着的终端")
    if acc.startswith("cx:"):
        cands = [a for a in codex_accounts() if a["logged"] and "cx:" + a["id"] != acc]
        score = lambda a: 1000 if a["relay"] else ((codex_usage(a["id"]).get("five_hour") or {}).get("percent") or 0)
        best = min(cands, key=score, default=None)
        to, name = (("cx:" + best["id"]), "Codex · " + best["name"]) if best else (None, "")
    else:
        def score(a):
            u = get_usage(a["id"])
            if u.get("error"):
                return 1000
            h, w = ((u.get(k) or {}).get("percent") or 0 for k in ("five_hour", "seven_day"))
            return 999 if w >= 100 else h
        cands = [a for a in _load()["accounts"] if a["id"] != acc]
        best = min(cands, key=score, default=None)
        to, name = (best["id"], "Claude · " + best["name"]) if best and score(best) < 1000 else (None, "")
    if not to:
        raise ValueError("没有别的能用的号")
    for r in terms:
        if to.startswith("cx:"):
            switch_codex(r["pid"], to[3:])
        else:
            switch_running(r["pid"], to)
    return {"count": len(terms), "to": name}


def usage_detail(acc):
    """额度详情：当前额度、最近 24 小时 5 小时额度的变化（画小曲线）、每天用掉的周额度、照最近速度几点用完"""
    if acc.startswith("cx:"):
        return {"usage": codex_usage(acc[3:]), "points": [], "daily": {}}
    u = get_usage(acc)
    pts = []
    try:
        for line in USAGE_LOG.open(encoding="utf-8"):
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if r.get("acc") == acc and r["t"] > time.time() - 86400 and r.get("h") is not None:
                pts.append([r["t"], r["h"]])
    except OSError:
        pass
    # 照最近 1 小时的速度，5 小时额度几点用完（没在涨、或者重置前用不完就不算）
    eta = None
    h = (u.get("five_hour") or {}).get("percent")
    recent = [p for p in pts if p[0] > time.time() - 3600]
    if h is not None and len(recent) >= 2 and recent[-1][1] > recent[0][1]:
        rate = (recent[-1][1] - recent[0][1]) / (recent[-1][0] - recent[0][0])
        t = time.time() + (100 - h) / rate
        reset = (u.get("five_hour") or {}).get("resets_at")
        from datetime import datetime
        if not reset or t < datetime.fromisoformat(reset).timestamp():
            eta = int(t)
    daily = (account_daily().get("accounts") or {}).get(acc, {})
    return {"usage": u, "points": pts, "daily": daily, "eta": eta}


def acc_web(acc):
    if acc.startswith("cx:"):
        open_codex_web(acc[3:])
    else:
        open_web(acc, "https://claude.ai/settings/usage")


def acc_login(acc):
    """在电脑上登录：Claude 号开一个这个号的终端（没登录会自动弹这个号的 Chrome 去授权）；Codex 号走设备码"""
    if acc.startswith("cx:"):
        return codex_login_desktop(acc[3:])
    launch_cli(acc, str(HOME))
    return {}


def acc_rename(acc, name, note):
    if acc.startswith("cx:"):
        home = CODEX_ACC_DIR / acc[3:]
        if acc == "cx:relay":
            home = HOME / ".claude-accounts" / "codex-relay"
            home.mkdir(parents=True, exist_ok=True)
        elif name:
            rename_codex_account(acc[3:], name)
        (home / "note.txt").write_text(note, encoding="utf-8")
    else:
        update_account(acc, **({"name": name} if name else {}), note=note)


def codex_note(acc_id):
    home = HOME / ".claude-accounts" / "codex-relay" if acc_id == "relay" else CODEX_ACC_DIR / acc_id
    try:
        return (home / "note.txt").read_text(encoding="utf-8")
    except OSError:
        return ""


def acc_remove(acc):
    """删号：有终端开着就不删（删了那些终端会断）；对话记录是共用的，不会跟着删"""
    if any(r["acc"] == acc for r in all_running()):
        raise ValueError("这个号还有终端开着，先关掉或换走")
    if acc.startswith("cx:"):
        remove_codex_account(acc[3:])
    else:
        remove_account(acc)
