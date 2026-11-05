# Shared Task Scheduler configuration. Dot-sourcing this file does not change a task.
function New-MaZingTaskAction {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [string] $Python,
        [Parameter(Mandatory = $true)]
        [string] $DeploymentRoot
    )

    $Python = [IO.Path]::GetFullPath($Python)
    $DeploymentRoot = [IO.Path]::GetFullPath($DeploymentRoot)
    if ($DeploymentRoot.Contains('"') -or $Python.Contains('"')) {
        throw 'Paths cannot contain double quotes.'
    }
    # Keep the task outside a console session. Preparation still uses python.exe.
    $windowlessPython = Join-Path (Split-Path -Parent $Python) 'pythonw.exe'
    if (-not (Test-Path -LiteralPath $windowlessPython -PathType Leaf)) {
        throw "Windowless Python not found: $windowlessPython"
    }
    $controller = Join-Path $DeploymentRoot 'supervisor.py'
    if (-not (Test-Path -LiteralPath $controller -PathType Leaf)) {
        throw "Deployment supervisor not found: $controller"
    }
    $arguments = '"{0}" --root "{1}"' -f $controller, $DeploymentRoot
    New-ScheduledTaskAction -Execute $windowlessPython -Argument $arguments -WorkingDirectory $DeploymentRoot
}

function New-MaZingTaskTriggers {
    [CmdletBinding()]
    param(
        [string] $LogonUser = '',
        [datetime] $WatchdogStart = (Get-Date).AddMinutes(1)
    )

    if ($LogonUser) {
        New-ScheduledTaskTrigger -AtLogOn -User $LogonUser
    } else {
        New-ScheduledTaskTrigger -AtStartup
    }
    # Omitting RepetitionDuration means no end date. IgnoreNew makes this a
    # restart watchdog: an already running supervisor is never interrupted.
    New-ScheduledTaskTrigger -Once -At $WatchdogStart -RepetitionInterval (New-TimeSpan -Minutes 1)
}
