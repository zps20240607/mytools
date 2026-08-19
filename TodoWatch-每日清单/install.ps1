# install.ps1 - Create desktop shortcut for Daily Todo
# Uses Unicode escapes to avoid encoding issues
$ws = New-Object -ComObject WScript.Shell
$desktop = [Environment]::GetFolderPath('Desktop')

# Build Chinese strings from Unicode code points
$folderName = [char]0x6BCF + [char]0x65E5 + [char]0x6E05 + [char]0x5355        # 每日清单
$openBat   = [char]0x6253 + [char]0x5F00 + [char]0x6BCF + [char]0x65E5 + [char]0x6E05 + [char]0x5355  # 打开每日清单

$targetDir = "D:\mytools\TodoWatch-" + $folderName
$lnkPath = Join-Path $desktop ($folderName + ".lnk")

# Remove old shortcut if exists
if (Test-Path $lnkPath) { Remove-Item $lnkPath -Force }

$shortcut = $ws.CreateShortcut($lnkPath)
$shortcut.TargetPath = $targetDir + "\" + $openBat + ".bat"
$shortcut.WorkingDirectory = $targetDir
$shortcut.IconLocation = "shell32.dll,43"
$shortcut.Description = "Daily Todo Desktop Widget"
$shortcut.Save()

Write-Host "Desktop shortcut created: $lnkPath"
Write-Host "Target: $($shortcut.TargetPath)"
