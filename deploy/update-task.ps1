[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [string] $DeploymentRoot = (Join-Path (Split-Path -Parent $PSScriptRoot) 'work\deployment'),
    [string] $TaskName = 'MaZingProduction',
    [string] $TaskPath = '\'
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'task-config.ps1')
$DeploymentRoot = [IO.Path]::GetFullPath($DeploymentRoot)
$settings = Get-Content -LiteralPath (Join-Path $DeploymentRoot 'settings.json') -Raw | ConvertFrom-Json
$action = New-MaZingTaskAction -Python $settings.python -DeploymentRoot $DeploymentRoot
$task = Get-ScheduledTask -TaskName $TaskName -TaskPath $TaskPath
# CIM may return a shortened account name that Windows cannot resolve again.
# The exported definition retains the exact SID originally registered for it.
[xml] $definition = Export-ScheduledTask -TaskName $TaskName -TaskPath $TaskPath -ErrorAction Stop
$principalUserId = [string] $definition.Task.Principals.Principal.UserId
if (-not $principalUserId) { throw 'The installed task has no resolvable principal.' }
$task.Principal.UserId = $principalUserId
switch ([string] $task.Principal.LogonType) {
    'Interactive' { $triggers = New-MaZingTaskTriggers -LogonUser $principalUserId }
    'ServiceAccount' { $triggers = New-MaZingTaskTriggers }
    default { throw "Unsupported task logon type: $($task.Principal.LogonType)" }
}
# Preserve the installed principal, enabled state, and other task settings.
$task.Settings.MultipleInstances = (New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew).MultipleInstances
if ($PSCmdlet.ShouldProcess("$TaskPath$TaskName", 'Update windowless action and one-minute restart watchdog')) {
    Set-ScheduledTask -TaskName $TaskName -TaskPath $TaskPath -Action $action `
        -Trigger $triggers -Settings $task.Settings -Principal $task.Principal -ErrorAction Stop | Out-Null
    $updated = Get-ScheduledTask -TaskName $TaskName -TaskPath $TaskPath -ErrorAction Stop
    if ($updated.Actions.Execute -ne $action.Execute -or $updated.Triggers.Count -ne 2) {
        throw 'Task update did not persist the expected windowless action and watchdog.'
    }
    Write-Output "Updated $TaskPath$TaskName without stopping its running process. The next launch uses pythonw.exe."
}
