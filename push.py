"""手机通知：盯着各终端的状态，Claude 回复完了 / 停下来等你确认时，推一条通知到手机（网页推送）"""
import base64
import json
import re
import threading

from cryptography.hazmat.primitives import serialization
from py_vapid import Vapid
from pywebpush import WebPushException, webpush

import core

KEY_PEM = core.DATA_DIR / "push-vapid.pem"
SUBS = core.DATA_DIR / "push-subs.json"
lock = threading.Lock()


def _vapid():
    if not KEY_PEM.exists():
        v = Vapid()
        v.generate_keys()
        v.save_key(str(KEY_PEM))
    return Vapid.from_file(str(KEY_PEM))


def public_key():
    """给浏览器订阅用的公钥（base64url）"""
    raw = _vapid().public_key.public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _subs():
    try:
        return json.loads(SUBS.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []


def subscribe(sub):
    with lock:
        subs = [s for s in _subs() if s.get("endpoint") != sub.get("endpoint")] + [sub]
        SUBS.write_text(json.dumps(subs), encoding="utf-8")


def send(title, body, url, tag):
    payload = json.dumps({"title": title, "body": body, "url": url, "tag": tag}, ensure_ascii=False)
    dead = []
    for s in _subs():
        try:
            webpush(s, payload, vapid_private_key=str(KEY_PEM), vapid_claims={"sub": "mailto:claude-manager@example.com"}, ttl=600)
        except WebPushException as e:
            # 手机上取消了通知 / 订阅过期，就从名单里去掉
            if e.response is not None and e.response.status_code in (404, 410):
                dead.append(s["endpoint"])
        except Exception:
            pass
    if dead:
        with lock:
            SUBS.write_text(json.dumps([s for s in _subs() if s["endpoint"] not in dead]), encoding="utf-8")


def _last_reply(session):
    for m in reversed(core.read_messages(session, 20)):
        if m["role"] == "assistant" and m["kind"] == "text":
            t = re.sub(r"[#*`>|]+", "", m["text"])
            return re.sub(r"\s+", " ", t).strip()[:140]
    return ""


def watch(key_fn, stop):
    """每 3 秒看一眼各终端状态，状态变化时推送"""
    last = {}
    while not stop.is_set():
        try:
            if _subs():
                now = {r["pid"]: r for r in core.list_running()}
                for pid, r in now.items():
                    prev = last.get(pid, {}).get("status")
                    title = f"{r['acc_name']} · {(r['title'] or '新对话')[:30]}"
                    url = f"/app/{key_fn()}#s={r['session']}"
                    if r["status"] == "waiting" and prev != "waiting" and prev is not None:
                        send(title, "Claude 在等你确认，点开处理", url, r["session"])
                    elif prev == "busy" and r["status"] not in ("busy", "waiting"):
                        send(title, _last_reply(r["session"]) or "Claude 回复完了", url, r["session"])
                last = now
        except Exception as e:
            print("通知检查出错", e)
        stop.wait(3)


def start(key_fn):
    stop = threading.Event()
    threading.Thread(target=watch, args=(key_fn, stop), daemon=True).start()
    return stop
