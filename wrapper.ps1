# 管理器开终端时套的一层「守护」：claude 退出后，如果管理器留了换号指令，就在同一个窗口用新号接着那段对话
param([string]$ConfigDir, [string]$Browser, [string]$Title, [string]$Resume)
# 管理器用 none 表示空（空参数经过 wt 会被吞掉）
if ($ConfigDir -eq 'none') { $ConfigDir = '' }
if ($Resume -eq 'none') { $Resume = '' }

$run = Join-Path $HOME '.claude-accounts\run'
New-Item -ItemType Directory -Force $run | Out-Null
$mark = Join-Path $run "$PID.wrap"
$sw = Join-Path $run "$PID.switch"
New-Item -ItemType File -Force $mark | Out-Null
$env:CLAUDE_CODE_DISABLE_TERMINAL_TITLE = '1'

try {
    while ($true) {
        if ($ConfigDir) { $env:CLAUDE_CONFIG_DIR = $ConfigDir } else { Remove-Item Env:CLAUDE_CONFIG_DIR -ErrorAction SilentlyContinue }
        $env:BROWSER = $Browser
        $Host.UI.RawUI.WindowTitle = $Title
        # 接着旧对话时 Claude 会沿用那段对话原来的模式，所以这里明确指定 bypass
        if ($Resume) { claude --resume $Resume --permission-mode bypassPermissions } else { claude --permission-mode bypassPermissions }
        if (-not (Test-Path $sw)) { break }
        $s = Get-Content $sw -Raw -Encoding utf8 | ConvertFrom-Json
        Remove-Item $sw
        $ConfigDir = $s.config_dir; $Browser = $s.browser; $Title = $s.title; $Resume = $s.resume
        Clear-Host
        Write-Host ">>> $Title" -ForegroundColor Cyan
    }
} finally {
    Remove-Item $mark -ErrorAction SilentlyContinue
}
