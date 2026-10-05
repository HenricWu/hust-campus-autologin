param([switch]$ForgetCredentials)
$ErrorActionPreference = 'Stop'
$dataDir = Join-Path $env:ProgramData ('CampusAutoLogin\data\' + $env:USERNAME)
$sid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
$taskName = 'Campus Auto Login - ' + $sid
if (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue) {
    Stop-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
}
$settingsPath = Join-Path $dataDir 'settings.json'
if (Test-Path -LiteralPath $settingsPath) {
    $settings = Get-Content -LiteralPath $settingsPath -Raw | ConvertFrom-Json
    $settings.enabled = $false
    if ($ForgetCredentials) { $settings.secret = '' }
    [IO.File]::WriteAllText($settingsPath, ($settings | ConvertTo-Json), [Text.UTF8Encoding]::new($false))
}
foreach ($name in @('校园网自动登录.lnk', '华中科技大学校园网自动登录.lnk')) {
    $shortcut = Join-Path ([Environment]::GetFolderPath('Desktop')) $name
    if (Test-Path -LiteralPath $shortcut) { Remove-Item -LiteralPath $shortcut }
}
Write-Output 'Automatic login removed. The campus network session has not been disconnected. Source and logs are retained.'
