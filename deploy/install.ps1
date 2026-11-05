[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[a-f0-9]{40}$')]
    [string] $InitialCommit,
    [string] $SourceRoot = (Split-Path -Parent $PSScriptRoot),
    [string] $DeploymentRoot = '',
    [string] $DataRoot = '',
    [string] $Python = '',
    [string] $Repository = 'NguyenThanhNhatAnh123/MaZing',
    [string] $TaskName = 'MaZingProduction',
    [ValidateRange(1, 65535)]
    [int] $Port = 8780,
    [switch] $RunAsCurrentUser,
    [switch] $NoStart
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'task-config.ps1')
$SourceRoot = [IO.Path]::GetFullPath($SourceRoot)
if (-not $DeploymentRoot) { $DeploymentRoot = Join-Path $SourceRoot 'work\deployment' }
if (-not $DataRoot) { $DataRoot = $SourceRoot }
if (-not $Python) { $Python = Join-Path $SourceRoot '.venv\Scripts\python.exe' }
$DeploymentRoot = [IO.Path]::GetFullPath($DeploymentRoot)
$DataRoot = [IO.Path]::GetFullPath($DataRoot)
$Python = [IO.Path]::GetFullPath($Python)
if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) { throw "Python not found: $Python" }
if ($DeploymentRoot.Contains('"') -or $Python.Contains('"')) { throw 'Paths cannot contain double quotes.' }
if (-not $RunAsCurrentUser) {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw 'Run PowerShell as Administrator to register startup as SYSTEM, or pass -RunAsCurrentUser for login startup.'
    }
}
$existingTask = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existingTask -and $existingTask.State -eq 'Running') {
    throw "Task $TaskName is already running. Stop this managed task before upgrading the supervisor."
}
New-Item -ItemType Directory -Path $DeploymentRoot -Force | Out-Null
New-Item -ItemType Directory -Path (Join-Path $DeploymentRoot 'logs') -Force | Out-Null
$controller = Join-Path $DeploymentRoot 'supervisor.py'
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'supervisor.py') -Destination $controller -Force
$settings = @{
    repository = $Repository
    branch = 'main'
    data_root = $DataRoot
    python = $Python
    port = $Port
    interval = 300
    drain_timeout = 600
}
$json = $settings | ConvertTo-Json
[IO.File]::WriteAllText((Join-Path $DeploymentRoot 'settings.json'), $json, (New-Object Text.UTF8Encoding($false)))

# Preparation verifies the exact main SHA and CI, creates a separate venv, and
# smoke-tests on an ephemeral port with disposable data. Existing production
# and all user files remain available until the operator starts the task.
& $Python $controller --root $DeploymentRoot --prepare $InitialCommit
if ($LASTEXITCODE -ne 0) { throw 'Release preparation failed; the scheduled task was not changed.' }

$action = New-MaZingTaskAction -Python $Python -DeploymentRoot $DeploymentRoot
$taskSettings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -RestartCount 20 -RestartInterval (New-TimeSpan -Minutes 1) -MultipleInstances IgnoreNew -Disable
if ($RunAsCurrentUser) {
    $user = [Security.Principal.WindowsIdentity]::GetCurrent().Name
    $taskPrincipal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
    $triggers = New-MaZingTaskTriggers -LogonUser $user
} else {
    $taskPrincipal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
    $triggers = New-MaZingTaskTriggers
}
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $triggers `
    -Settings $taskSettings -Principal $taskPrincipal -Description 'MaZing WSGI and CI-gated production deployments' -Force | Out-Null

if (-not $NoStart) {
    $listener = Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue
    if ($listener) {
        throw "Task registered but disabled: port $Port is occupied. Stop only the verified old MaZing server, then Enable-ScheduledTask and Start-ScheduledTask -TaskName '$TaskName'."
    }
    Enable-ScheduledTask -TaskName $TaskName | Out-Null
    Start-ScheduledTask -TaskName $TaskName
}
if ($NoStart) {
    Write-Output "Task is disabled so its watchdog cannot start before migration. Enable-ScheduledTask -TaskName '$TaskName' when ready."
}
Write-Output "Prepared $InitialCommit. Scheduled task: $TaskName. Logs: $(Join-Path $DeploymentRoot 'logs')"
