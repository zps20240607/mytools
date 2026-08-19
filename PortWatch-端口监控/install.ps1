# install.ps1 - Create desktop shortcut for PortWatch
# Uses Unicode escapes to avoid encoding issues
$ws = New-Object -ComObject WScript.Shell
$desktop = [Environment]::GetFolderPath('Desktop')

# Build Chinese strings from Unicode code points
$folderName = [char]0x7AEF + [char]0x53E3 + [char]0x76D1 + [char]0x63A7        # 端口监控
$startBat   = [char]0x542F + [char]0x52A8 + [char]0x63A7 + [char]0x5236 + [char]0x53F0  # 启动控制台

$targetDir = "D:\mytools\PortWatch-" + $folderName
$lnkPath = Join-Path $desktop ($folderName + ".lnk")

# Remove old shortcut if exists
if (Test-Path $lnkPath) { Remove-Item $lnkPath -Force }

$shortcut = $ws.CreateShortcut($lnkPath)
$shortcut.TargetPath = $targetDir + "\" + $startBat + ".bat"
$shortcut.WorkingDirectory = $targetDir
$shortcut.IconLocation = "shell32.dll,14"
$shortcut.Description = "PortWatch Port Monitor Console"
$shortcut.Save()

Write-Host "Desktop shortcut created: $lnkPath"
Write-Host "Target: $($shortcut.TargetPath)"
