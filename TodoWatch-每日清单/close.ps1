# close.ps1 - Close Daily Todo widget (native WPF version)
# Also cleans up any leftover legacy HTML version processes.

# --- Kill the WPF widget (powershell running 每日清单.ps1) ---
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
