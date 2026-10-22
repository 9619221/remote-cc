"""把 Claude Code 的状态栏指向 Remote CC 自带的 statusline.ps1（Remote CC 读额度、读模型都靠它）。

用法：python setup_statusline.py <settings.json 路径> [--force]
改之前先备份；已经有别的状态栏时不覆盖，除非加 --force。
"""
import json
import shutil
import sys
import time
from pathlib import Path

settings = Path(sys.argv[1])
script = (Path(__file__).parent / "statusline.ps1").resolve().as_posix()
cmd = f'powershell -NoProfile -ExecutionPolicy Bypass -File "{script}"'

data = {}
if settings.exists():
    try:
        data = json.loads(settings.read_text(encoding="utf-8-sig") or "{}")
    except ValueError:
        sys.exit(f"设置文件格式有问题，没有改动：{settings}")

old = (data.get("statusLine") or {}).get("command", "")
if old == cmd:
    print("状态栏已经配好了")
    sys.exit(0)
if old and "--force" not in sys.argv:
    print(f"你已经有自己的状态栏：{old}")
    sys.exit(2)  # 交给安装脚本问用户要不要替换

if settings.exists():
    backup = settings.with_name(f"settings.json.bak-{time.strftime('%Y%m%d-%H%M%S')}")
    shutil.copy2(settings, backup)
    print(f"已备份原设置：{backup}")
data["statusLine"] = {"type": "command", "command": cmd}
settings.parent.mkdir(parents=True, exist_ok=True)
settings.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print(f"状态栏已配好：{settings}")
