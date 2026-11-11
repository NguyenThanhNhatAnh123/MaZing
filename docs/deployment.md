# Production at mz.eagleglobal2023.com

Quote reading uses the official HTTPS Messages API through Python's standard
library. It does not load the Anthropic SDK or its native `jiter` parser, which
Windows App Control can block after a reboot. Keep Windows code-integrity policy
enabled; `tools/test_ocr.py` covers operation with native SDK imports blocked.

## Dashboard login

The public dashboard requires the shared MaZing account. Login sessions last
30 days from sign-in, survive releases/restarts, and are revoked on logout or
credential changes. The `mayzing_login` cookie contains an opaque token with
Secure, HttpOnly and SameSite=Lax; passwords and shop cookies never enter it.
Each browser keeps its own workspace even when the team shares one account.
Health, the connection guide and extension downloads remain public; the
extension's paired bearer channel still works without dashboard cookies.

Provision credentials once on the host (outside release directories):

```powershell
python deploy/configure-login.py
# To use the existing PodSach account on this host:
python deploy/configure-login.py --from-podsach-env F:\PodSach\.env
```

Only a password hash is written to `auth/web-login.json`; never commit it or
`auth/web-sessions.sqlite3`. Re-run the command when changing the account.
This copies the current PodSach credentials; later PodSach changes require
running the command again. Missing credentials deny access to protected routes.
Loopback access stays available for host tools; set `MAYZING_REQUIRE_LOCAL_LOGIN=1`
to require login there as well. Logout waits for a running job to finish, then
revokes that browser's session and disconnects its extension.

Cloudflare Tunnel `office-qa` routes `mz.eagleglobal2023.com` to
`http://127.0.0.1:8780`. Keep this origin loopback-only. The production process
uses Waitress; customers connect their own Chrome extension over the public HTTPS
origin, so Chrome is not launched on the Windows server.

## Automatic delivery

1. Push reviewed changes to the repository's default `main` branch.
2. GitHub Actions `MaZing CI` runs the offline Python tests, extension/dashboard
   JavaScript syntax checks, deployment gate/rollback tests, and a real WSGI smoke
   test on Windows and Linux. No shop campaigns are modified.
3. The Windows deployment supervisor checks GitHub every five minutes. It accepts
   only the current `main` commit with a successful **push** run of
   `.github/workflows/ci.yml` in this same repository. PR and fork runs never deploy.
4. It downloads that immutable commit into a separate release directory, creates
   its own virtual environment, and smoke-tests with temporary data on another port.
5. It stops accepting new mutations, waits up to ten minutes for existing jobs
   and synchronous uploads/changes,
   then switches its own WSGI process. If a job is still running, delivery waits
   for the next check. If the new server fails its health check, it starts the old
   release and records the failed SHA so it will not loop on the same broken build.

The server is deliberately not a GitHub self-hosted runner for the public repo.
GitHub recommends using self-hosted runners only with private repositories because
fork PR code can otherwise reach the host. See [GitHub runner guidance](https://docs.github.com/en/actions/how-tos/manage-runners/self-hosted-runners/add-runners)
and the [workflow runs API](https://docs.github.com/en/rest/actions/workflow-runs).
The supervisor needs outbound HTTPS, GitHub public API access, PyPI, and the local
Python interpreter. No inbound deployment port or GitHub token is required for
this public repository. An optional `MAYZING_DEPLOY_GITHUB_TOKEN` host environment
variable can supply read-only Actions access if the repository becomes private;
private archive downloads also require adapting the download authentication.

## First installation on this Windows host

Commit and push the production changes first, then wait for the main push CI run
to pass. Use the full 40-character successful commit SHA below. In an elevated
PowerShell window:

```powershell
Set-Location F:\Mayzing
.\deploy\install.ps1 -InitialCommit '<FULL_MAIN_SHA>' -NoStart
```

This prepares a release and registers the `MaZingProduction` task as SYSTEM at
Windows startup. With `-NoStart`, the task stays disabled so its watchdog cannot
start before migration; the existing server keeps running. Stop **only the
verified old MaZing dashboard process** currently listening on port 8780, then:

```powershell
Enable-ScheduledTask -TaskName 'MaZingProduction'
Start-ScheduledTask -TaskName 'MaZingProduction'
Invoke-RestMethod http://127.0.0.1:8780/api/health
Invoke-RestMethod https://mz.eagleglobal2023.com/api/health
```

Both health responses must show `ok: true`, `active_jobs: 0`, `active_requests: 0` and the deployed
commit in `release`. The health route does not create a workspace or set cookies.
If administrator startup is unavailable, `-RunAsCurrentUser` registers login
startup instead; that mode requires the Windows user to remain signed in.

This host is installed with `-RunAsCurrentUser`: `MaZingProduction` starts at
Windows sign-in and is already running in the current session. Signing out stops
availability; locking the desktop keeps it running. Switching to startup before
sign-in requires reinstalling the task as SYSTEM from an Administrator shell.

The scheduled action uses `pythonw.exe` beside the configured `python.exe`, so it
has no console window. Release preparation still uses `python.exe`. A second
trigger runs every minute indefinitely; `IgnoreNew` skips it while the supervisor
is running and starts the task again after an unexpected exit. The supervisor's
own file log remains available without console output. This watchdog requires
the task to be enabled and, in current-user mode, the user to remain signed in.

After a production update, clients should reload their dashboard. For the
multi-device release, download extension **1.2.0** from `/api/huongdan`, extract
it and reload or reinstall it in `chrome://extensions`. Check the extension's
server field is `https://mz.eagleglobal2023.com`, then create a new code in that
same browser profile and reconnect its Mayzing tab. Old loopback-only extension
versions cannot connect remotely.

The stable supervisor copy, release virtual environments, status, and logs live in
`F:\Mayzing\work\deployment`. The deployment settings default to:

```json
{
  "repository": "NguyenThanhNhatAnh123/MaZing",
  "branch": "main",
  "data_root": "F:\\Mayzing",
  "python": "F:\\Mayzing\\.venv\\Scripts\\python.exe",
  "port": 8780,
  "interval": 300,
  "drain_timeout": 600
}
```

Application state is shared through `MAYZING_DATA_ROOT=F:\Mayzing`; `.env` is
loaded through `MAYZING_ENV_FILE=F:\Mayzing\.env`. A release never overwrites
`.env`, input/output images, per-workspace state, auth, settings, or shop history.
The working checkout and its uncommitted files are not reset or replaced.
Keep a separate backup of this data directory. Releases and logs are retained
for diagnosis; archive old releases manually when disk usage warrants it.

## Operations and recovery

```powershell
Get-ScheduledTask -TaskName 'MaZingProduction'
Get-Content F:\Mayzing\work\deployment\state.json
Get-Content F:\Mayzing\work\deployment\logs\supervisor.log -Tail 40
```

To apply the windowless action and one-minute watchdog to an existing installation
without downloading a release or stopping its running server:

```powershell
.\deploy\update-task.ps1 -WhatIf
.\deploy\update-task.ps1
```

This preserves the task's principal and enabled state. The running process keeps
serving; its next launch uses `pythonw.exe`. On 4 November 2026, the stopped task
reported `0xC000013A` (console interrupt). The origin had stopped while Cloudflare
Tunnel remained running, causing HTTP 502. That exit code does not identify who
or what sent the interrupt; windowless startup and the watchdog address the
console dependency and missing automatic recovery.

An update or server restart disconnects in-memory extension sessions. Reconnect
the shop using a new pairing code if the extension requests it. Existing user
files and completed campaign state persist. The supervisor never automatically
resends a Mayzing mutation after a restart.

To pause automatic updates while keeping the current server online, create an
empty `work\deployment\paused` file. Remove it to resume normal update checks.
Stopping the scheduled task also stops its WSGI child; the Windows job object
prevents an orphan from continuing on the port. Disable the task before planned
maintenance, otherwise the one-minute watchdog can start it again. Enable it
after maintenance and start it explicitly to resume service immediately.

For a manual rollback, disable and stop the task when jobs are idle, change only `current` in
`work\deployment\state.json` to the retained `previous` SHA, and start the task.
Set `failed` to the unwanted SHA so it is not redeployed. Do not remove data files.
To retry that same SHA after an infrastructure fix, clear `failed` in `state.json`
while the task is stopped. Enable and start the task after editing state. A code
fix should normally use a new commit.

The installed supervisor is stable infrastructure. To upgrade it, disable and stop the task
when jobs are idle, copy the reviewed `deploy\supervisor.py` to
`work\deployment\supervisor.py`, then enable and start the task again. Application commits
are delivered automatically by the installed supervisor.
