# open.ps1 - Launch Daily Todo widget (native WPF version)
# 1. Kill any existing instance (widget + legacy Edge/node version)
# 2. Start the WPF widget via VBS so NO console window / taskbar button appears

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$widget = Join-Path $scriptDir '每日清单.ps1'

# --- Close any existing widget instance ---
Get-CimInstance Win32_Process |
    Where-Object { $_.CommandLine -like '*每日清单.ps1*' -and $_.ProcessId -ne $PID } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }

# --- Legacy cleanup: old HTML version (Edge app-mode + node server) ---
$edgeProcs = Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*todo-widget-profile*' }
$edgeProcs | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
$conn = Get-NetTCPConnection -LocalPort 18964 -ErrorAction SilentlyContinue
if ($conn) {
    Stop-Process -Id $conn.OwningProcess -Force -ErrorAction SilentlyContinue
}
Start-Sleep -Milliseconds 300

# --- Launch fully hidden via VBS (no console flash, no taskbar terminal) ---
$vbs = Join-Path $env:TEMP 'todo_widget_launch.vbs'
$vbsContent = @"
Set ws = CreateObject("Wscript.Shell")
ws.CurrentDirectory = "$scriptDir"
ws.Run "powershell -STA -NoProfile -ExecutionPolicy Bypass -File ""$widget""", 0, False
"@
[IO.File]::WriteAllText($vbs, $vbsContent, [Text.Encoding]::Default)
Start-Process wscript.exe -ArgumentList "`"$vbs`""
