param([switch]$NoOpen)
#Requires -RunAsAdministrator
$ErrorActionPreference = 'Stop'
$installDir = Join-Path $env:ProgramData 'CampusAutoLogin'
$dataDir = Join-Path $installDir ('data\' + $env:USERNAME)
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$taskName = 'Campus Auto Login - ' + $identity.User.Value
$python = (Get-Command python.exe -ErrorAction Stop).Source
$pythonw = Join-Path (Split-Path -Parent $python) 'pythonw.exe'
if (-not (Test-Path -LiteralPath $pythonw)) { throw 'pythonw.exe is required.' }
& $python -c 'import tkinter, ctypes, http.client'
if ($LASTEXITCODE -ne 0) { throw 'Python with Tkinter is required.' }
New-Item -ItemType Directory -Path $installDir,$dataDir -Force | Out-Null
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'campus_login.py') -Destination $installDir -Force
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'uninstall.ps1') -Destination $installDir -Force
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'campus-icon.ico') -Destination $installDir -Force
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'campus-icon.png') -Destination $installDir -Force
Get-ChildItem -LiteralPath $PSScriptRoot -Filter 'hust-seal*.png' -File | Copy-Item -Destination $installDir -Force
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'assets') -Destination $installDir -Recurse -Force
# Keep saved settings readable only by this account, SYSTEM and administrators.
& icacls.exe $dataDir /inheritance:r /grant:r "*$($identity.User.Value):(OI)(CI)F" '*S-1-5-18:(OI)(CI)F' '*S-1-5-32-544:(OI)(CI)F' | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'Could not protect the settings directory.' }
$scriptPath = Join-Path $installDir 'campus_login.py'
& $python $scriptPath --data-dir $dataDir --migrate-credentials
if ($LASTEXITCODE -ne 0) { throw 'Could not migrate saved credentials for unattended operation.' }
$action = New-ScheduledTaskAction -Execute $pythonw -Argument ('"' + $scriptPath + '" --once --data-dir "' + $dataDir + '"') -WorkingDirectory $installDir
$startup = New-ScheduledTaskTrigger -AtStartup
$minute = New-ScheduledTaskTrigger -Once -At ((Get-Date).AddMinutes(1)) -RepetitionInterval (New-TimeSpan -Minutes 1)
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 2)
$principal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
Register-ScheduledTask -TaskName $taskName -Action $action -Trigger @($startup,$minute) -Settings $settings -Principal $principal -Description 'Unattended campus recovery: at boot and every minute, even while locked or signed out. Reconnect only after an ePortal authentication redirect.' -Force | Out-Null
$shell = New-Object -ComObject WScript.Shell
$desktopDir = [Environment]::GetFolderPath('Desktop')
$shortcut = $shell.CreateShortcut((Join-Path $desktopDir '华中科技大学校园网自动登录.lnk'))
$shortcut.TargetPath = $pythonw
$shortcut.Arguments = '"' + $scriptPath + '" --gui --data-dir "' + $dataDir + '"'
$shortcut.WorkingDirectory = $installDir
$shortcut.Description = '华中科技大学校园网：一键登录、每分钟检测、掉线自动重登'
$shortcut.IconLocation = (Join-Path $installDir 'campus-icon.ico') + ',0'
$shortcut.Save()
$oldShortcut = Join-Path $desktopDir '校园网自动登录.lnk'
if (Test-Path -LiteralPath $oldShortcut) { Remove-Item -LiteralPath $oldShortcut }
$launchCommand = '"' + $pythonw + '" "' + $scriptPath + '" --gui --data-dir "' + $dataDir + '"'
$vbs = 'CreateObject("WScript.Shell").Run "' + $launchCommand.Replace('"','""') + '", 0, False'
[IO.File]::WriteAllText((Join-Path $installDir 'open.vbs'), $vbs, [Text.Encoding]::Unicode)
$manifest = @{installed_at=(Get-Date).ToString('o');task=$taskName;python=$python;install_dir=$installDir}
$manifest | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $dataDir 'installation.json') -Encoding UTF8
& $python $scriptPath --diagnose --data-dir $dataDir
if (-not $NoOpen) { Start-Process -FilePath $pythonw -ArgumentList ('"' + $scriptPath + '" --gui --data-dir "' + $dataDir + '"') -WindowStyle Hidden }
Write-Output 'Installed. Open the desktop shortcut and click One-click login. Existing saved settings are preserved.'
