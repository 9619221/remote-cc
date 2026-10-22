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
if ($width -gt $line.Length) { $line = $line.PadLeft($width - 1) }
$esc = [char]27
Write-Host "$esc[0m$line" -NoNewline
