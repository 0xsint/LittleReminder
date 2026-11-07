<#
.SYNOPSIS
    Registers Notification App as a scheduled task that runs hidden at logon.

.DESCRIPTION
    Windows services run in session 0 and cannot show windows on your desktop, so
    this registers a per-user scheduled task instead: it starts at logon, runs
    hidden under pythonw.exe, and restarts if it ever exits unexpectedly. It also
    adds a "Notification App Settings" shortcut to the Start menu.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File install.ps1
    powershell -ExecutionPolicy Bypass -File install.ps1 -Uninstall
#>
param(
    [switch]$Uninstall,
    [string]$Python  # path to python.exe, if it isn't on PATH
)

$ErrorActionPreference = 'Stop'

$TaskName = 'Notification App'
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$AppPath = Join-Path $Root 'app.py'
$ShortcutPath = Join-Path ([Environment]::GetFolderPath('Programs')) 'Notification App Settings.lnk'

if ($Uninstall) {
    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        # Stopping the task kills its pythonw process; Stop-Process by name would
        # also take out unrelated Python programs.
        Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Host "Stopped and removed scheduled task '$TaskName'."
    } else {
        Write-Host "No scheduled task named '$TaskName' is registered."
    }
    if (Test-Path $ShortcutPath) {
        Remove-Item $ShortcutPath
        Write-Host "Removed the Start menu settings shortcut."
    }
    return
}

# Run the task under pythonw.exe so no console window appears. The project venv
# is preferred: Store-installed Python lives behind an app-execution alias that
# does not always launch from a non-interactive context.
$VenvPythonw = Join-Path $Root '.venv\Scripts\pythonw.exe'

$Pythonw = $null
if ($Python) {
    $windowless = Join-Path (Split-Path $Python) 'pythonw.exe'
    $Pythonw = if (Test-Path $windowless) { $windowless } else { $Python }
} elseif (Test-Path $VenvPythonw) {
    $Pythonw = $VenvPythonw
} else {
    foreach ($candidate in (Get-Command python.exe, python3.exe -ErrorAction SilentlyContinue).Source) {
        & $candidate -c "import tkinter" 2>$null
        if ($LASTEXITCODE -ne 0) { continue }
        $windowless = Join-Path (Split-Path $candidate) 'pythonw.exe'
        $Pythonw = if (Test-Path $windowless) { $windowless } else { $candidate }
        break
    }
}
if (-not $Pythonw -or -not (Test-Path $Pythonw)) {
    throw "No Python with tkinter found. Run 'python -m venv .venv', or pass -Python <path to python.exe>."
}

$action = New-ScheduledTaskAction -Execute $Pythonw -Argument "`"$AppPath`"" -WorkingDirectory $Root

$trigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
$trigger.Delay = 'PT30S'  # let the shell finish starting before we attach to it

$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -MultipleInstances IgnoreNew

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Principal $principal -Settings $settings `
    -Description 'Always-on-top reminder popup at the times set in config.json.' `
    -Force | Out-Null

# Restart, so a reinstall runs the new app.py instead of leaving the old
# process going (MultipleInstances IgnoreNew would skip the new start).
Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
for ($i = 0; $i -lt 20 -and (Get-ScheduledTask -TaskName $TaskName).State -eq 'Running'; $i++) {
    Start-Sleep -Milliseconds 250
}
Start-ScheduledTask -TaskName $TaskName

$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($ShortcutPath)
$shortcut.TargetPath = $Pythonw
$shortcut.Arguments = "`"$AppPath`" --settings"
$shortcut.WorkingDirectory = $Root
$shortcut.Description = 'Change Notification App reminder times'
$shortcut.Save()

Write-Host "Installed and started '$TaskName'."
Write-Host "  Interpreter: $Pythonw"
Write-Host "  Settings:    Start menu > Notification App Settings"
Write-Host "  Log file:    $(Join-Path $Root 'app.log')"
Write-Host "  Stop it:     powershell -File install.ps1 -Uninstall"
