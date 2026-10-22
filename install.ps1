# Remote CC 一键安装：检查环境 → 装依赖 → 配状态栏 → 建桌面快捷方式（可选开机自启、语音输入）
# 加 -Yes 全部按默认走、不提问（不开机自启、不装语音）
param([switch]$Yes, [string]$DesktopDir = [Environment]::GetFolderPath('Desktop'))

$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
function Ok($m) { Write-Host "  [好] $m" -ForegroundColor Green }
function Warn($m) { Write-Host "  [注意] $m" -ForegroundColor Yellow }
function Fail($m) { Write-Host "  [不行] $m" -ForegroundColor Red; exit 1 }
function Ask($q) { if ($Yes) { return $false }; (Read-Host "$q (y/N)") -match '^[yY]' }

Write-Host "`n== Remote CC 安装 ==`n" -ForegroundColor Cyan

# 1. 检查环境
Write-Host "1/4 检查环境"
$py = $null
foreach ($c in 'python', 'py') {
    try {
        $v = & $c -c "import sys;print(sys.version_info[0]*100+sys.version_info[1]);print(sys.executable)" 2>$null
        if ($v -and [int]$v[0] -ge 310) { $py = $v[1]; break }
    } catch {}
}
if (-not $py) { Fail "没找到 Python 3.10 以上。到 https://www.python.org/downloads/ 下载安装，安装时勾选「Add python.exe to PATH」，装完重新双击 install.bat" }
Ok "Python：$py"
$pyw = Join-Path (Split-Path $py) 'pythonw.exe'
if (-not (Test-Path $pyw)) { $pyw = $py }

if (Get-Command claude -ErrorAction SilentlyContinue) { Ok "Claude Code 已安装" }
else { Warn "没找到 Claude Code（终端里敲 claude 没反应）。安装方法见 https://docs.anthropic.com/claude-code ，装完再用 Remote CC 开终端" }
if (Get-Command wt.exe -ErrorAction SilentlyContinue) { Ok "Windows Terminal 已安装" }
else { Warn "没找到 Windows Terminal，Remote CC 开终端要用它。到微软商店搜「Windows Terminal」安装" }

# 2. 装依赖
Write-Host "`n2/4 安装依赖（第一次要一两分钟）"
& $py -m pip install --disable-pip-version-check -q -r (Join-Path $here 'requirements.txt')
if ($LASTEXITCODE) { Fail "依赖没装上，看上面的报错；常见原因是网络连不上 PyPI" }
Ok "依赖装好了"
if (Ask "要装「按住说话」语音输入吗？会下载约 1GB，有 NVIDIA 显卡会快很多") {
    & $py -m pip install --disable-pip-version-check -q faster-whisper nvidia-cublas-cu12 nvidia-cudnn-cu12
    if ($LASTEXITCODE) { Warn "语音输入没装上，不影响其他功能" } else { Ok "语音输入装好了" }
}

# 3. 配状态栏（Remote CC 读额度、读模型都靠它）
Write-Host "`n3/4 配置 Claude Code 状态栏"
$cfg = if ($env:CLAUDE_CONFIG_DIR) { $env:CLAUDE_CONFIG_DIR } else { Join-Path $HOME '.claude' }
$settings = Join-Path $cfg 'settings.json'
& $py (Join-Path $here 'setup_statusline.py') $settings
if ($LASTEXITCODE -eq 2) {
    if (Ask "要换成 Remote CC 的状态栏吗？（原设置会先备份；不换的话手机上看不到额度和模型）") {
        & $py (Join-Path $here 'setup_statusline.py') $settings --force
    } else { Warn "没换状态栏：额度和模型显示会不可用" }
} elseif ($LASTEXITCODE) { Warn "状态栏没配上，可以照 README 手动配" }

# 4. 快捷方式
Write-Host "`n4/4 创建快捷方式"
$ws = New-Object -ComObject WScript.Shell
function Shortcut($path, $arg) {
    $s = $ws.CreateShortcut($path)
    $s.TargetPath = $pyw
    $s.Arguments = "`"$(Join-Path $here 'app.py')`" $arg".Trim()
    $s.WorkingDirectory = $here
    $s.IconLocation = Join-Path $here 'icon.ico'
    $s.Save()
}
Shortcut (Join-Path $DesktopDir 'Remote CC.lnk') ''
Ok "桌面上有「Remote CC」了"
if (Ask "要开机自动启动吗？（启动后缩在右下角托盘里）") {
    Shortcut (Join-Path ([Environment]::GetFolderPath('Startup')) 'Remote CC.lnk') '--tray'
    Ok "已设置开机自启"
}

Write-Host "`n== 装好了 ==" -ForegroundColor Cyan
Write-Host "双击桌面上的「Remote CC」打开，点「手机」用手机扫二维码。"
Write-Host "想在外面也能用、或者要收推送通知：电脑和手机都装 Tailscale（https://tailscale.com），再运行一次："
Write-Host "  tailscale serve --bg 8765"
Write-Host "已经开着的 Claude Code 终端要重开一次，状态栏才会生效。`n"
