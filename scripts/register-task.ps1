# register-task.ps1 - create (or repair) the houmai watcher task.
#
# One call, one definition: action, trigger, settings and principal go in
# together, and the power policy is part of the settings from the start.
# Creating the task first and patching its settings afterwards is fragile:
# the patch can be lost while the creation is still settling, which is
# exactly how the AC-only restriction kept coming back.
#
# Exit codes:
#   0  registered and verified (runs on AC and on battery)
#   1  registration failed (reason printed)
#   2  hidden launcher not found
#   3  registered, but the AC-only restriction is still in place
param(
    [string]$TaskName = 'houmai',
    [string]$Vbs = '',
    [int]$IntervalMinutes = 10
)

$ErrorActionPreference = 'Stop'

if (-not $Vbs -or -not (Test-Path -LiteralPath $Vbs)) {
    Write-Host "[FAIL] hidden launcher not found: $Vbs"
    exit 2
}

$userId = $env:USERDOMAIN + '\' + $env:USERNAME

try {
    $action = New-ScheduledTaskAction -Execute 'wscript.exe' -Argument ('"' + $Vbs + '"')
    $trigger = New-ScheduledTaskTrigger -Once -At ((Get-Date).AddMinutes(1)) `
               -RepetitionInterval (New-TimeSpan -Minutes $IntervalMinutes)
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries `
                -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew
    $principal = New-ScheduledTaskPrincipal -UserId $userId `
                 -LogonType Interactive -RunLevel Limited
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
        -Settings $settings -Principal $principal -Force | Out-Null
} catch {
    Write-Host "[FAIL] could not register the task: $($_.Exception.Message)"
    exit 1
}

# Read it back: only what is really inside the task counts. A registration
# that "succeeded" but left the settings wrong must not be reported as fine.
$t = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if (-not $t) {
    Write-Host "[FAIL] the task was registered but cannot be read back."
    exit 1
}
$next = (Get-ScheduledTaskInfo -TaskName $TaskName).NextRunTime

Write-Host ""
Write-Host "  Task     : $($t.TaskName)  [$($t.State)]"
Write-Host "  Action   : $($t.Actions[0].Execute) $($t.Actions[0].Arguments)"
Write-Host "  Repeat   : every $IntervalMinutes minutes, hidden window"
Write-Host "  Next run : $next"

if ($t.Settings.DisallowStartIfOnBatteries -or $t.Settings.StopIfGoingOnBatteries) {
    Write-Host ""
    Write-Host "[WARN] the task still carries an AC-only restriction"
    Write-Host "       (allow start on battery = $(-not $t.Settings.DisallowStartIfOnBatteries),"
    Write-Host "        keep running on battery = $(-not $t.Settings.StopIfGoingOnBatteries))."
    Write-Host "       The watch would silently stop while the laptop runs on battery."
    Write-Host "       Open Task Scheduler - $TaskName - Properties - Conditions and clear"
    Write-Host "       'Start the task only if the computer is on AC power' and"
    Write-Host "       'Stop if the computer switches to battery power'."
    exit 3
}

Write-Host ""
Write-Host "[OK] task registered and verified - the watch also runs on battery."
exit 0
