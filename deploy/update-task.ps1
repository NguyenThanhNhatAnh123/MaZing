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
switch ([string] $task.Principal.LogonType) {
    'Interactive' { $triggers = New-MaZingTaskTriggers -LogonUser $task.Principal.UserId }
    'ServiceAccount' { $triggers = New-MaZingTaskTriggers }
    default { throw "Unsupported task logon type: $($task.Principal.LogonType)" }
}
# Preserve the installed principal, enabled state, and other task settings.
$task.Settings.MultipleInstances = (New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew).MultipleInstances
if ($PSCmdlet.ShouldProcess("$TaskPath$TaskName", 'Update windowless action and one-minute restart watchdog')) {
    Set-ScheduledTask -TaskName $TaskName -TaskPath $TaskPath -Action $action `
        -Trigger $triggers -Settings $task.Settings | Out-Null
    Write-Output "Updated $TaskPath$TaskName without stopping its running process. The next launch uses pythonw.exe."
}
