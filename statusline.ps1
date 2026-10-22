$inputJson = [Console]::In.ReadToEnd() | ConvertFrom-Json
$model = $inputJson.model.display_name
$dir = Split-Path -Leaf $inputJson.workspace.current_dir
$used = $inputJson.context_window.used_percentage
$fiveHour = $inputJson.rate_limits.five_hour.used_percentage
$sevenDay = $inputJson.rate_limits.seven_day.used_percentage
# 把 Claude Code 自己带来的额度存一份，Remote CC 显示额度、90% 提醒都读它
$cfgLive = if ($env:CLAUDE_CONFIG_DIR) { $env:CLAUDE_CONFIG_DIR } else { Join-Path $HOME '.claude' }
if ($inputJson.rate_limits) { try { $inputJson.rate_limits | ConvertTo-Json -Depth 5 -Compress | Set-Content (Join-Path $cfgLive '.usage-live.json') -Encoding utf8 -ErrorAction Stop } catch {} }

$effort = $inputJson.effort.level
$parts = @($model)
if ($effort) { $parts += "effort: $effort" }
$parts += $dir
if ($used -ne $null) { $parts += "Context: $([math]::Round($used))% used" }
if ($fiveHour -ne $null) { $parts += "5h: $([math]::Round($fiveHour))%" }
if ($sevenDay -ne $null) { $parts += "7d: $([math]::Round($sevenDay))%" }
$line = $parts -join " | "
$width = 0
try { $width = [Console]::WindowWidth } catch {}
if ($width -le 0) { $width = 110 }
# 按终端里实际占的宽度补空格：中文等全角字占两格，按字数补会超出窗口宽度，整行显示不出来
$disp = 0; foreach ($ch in $line.ToCharArray()) { $disp += $(if ([int]$ch -ge 0x2E80) { 2 } else { 1 }) }
if ($width - 1 -gt $disp) { $line = (' ' * ($width - 1 - $disp)) + $line }
$esc = [char]27
Write-Host "$esc[0m$line" -NoNewline
