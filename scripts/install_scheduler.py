"""Create a Windows scheduled task that runs the agent daily.

Writes act through your already-signed-in Brave browser, so Brave must be
running with a debug port. This task makes sure it is, then runs the agent.

    python scripts/install_scheduler.py            # install
    python scripts/install_scheduler.py --remove   # uninstall
    python scripts/install_scheduler.py --status   # show current state
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
TASK_NAME = "AWSBuilderBadgeAgent"
BRAVE = Path(r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe")
PROFILE = "Profile 3"
PORT = 9222
HOUR = 9          # local time for the daily run
MINUTE = 10

RUNNER = PROJECT_ROOT / "scripts" / "run_daily.ps1"


def ps(script: str) -> tuple[int, str]:
    proc = subprocess.run(
        ["powershell", "-NoProfile", "-Command", script],
        capture_output=True, text=True,
    )
    return proc.returncode, (proc.stdout + proc.stderr).strip()


def already_scheduled() -> bool:
    code, _ = ps(f"(Get-ScheduledTask -TaskName '{TASK_NAME}' -ErrorAction SilentlyContinue) -ne $null")
    return code == 0 and ps(f"(Get-ScheduledTask -TaskName '{TASK_NAME}')")[1] != ""


def write_runner() -> None:
    """The script the task actually calls."""
    content = f"""# Daily run for the AWS Builder Badge Agent.
# Ensures Brave is listening on {PORT}, then runs the agent through it.

$ErrorActionPreference = 'Continue'
Set-Location '{PROJECT_ROOT}'

$brave = '{BRAVE}'
$endpoint = 'http://127.0.0.1:{PORT}'

# Start Brave with the debug port if it is not already listening.
function Test-DebugPort {{
    try {{
        Invoke-RestMethod -Uri "$endpoint/json/version" -TimeoutSec 2 | Out-Null
        return $true
    }} catch {{ return $false }}
}}

if (-not (Test-DebugPort)) {{
    if (Test-Path $brave) {{
        Write-Host 'Starting Brave with the debug port...'
        Start-Process -FilePath $brave -ArgumentList @(
            '--remote-debugging-port={PORT}',
            '--profile-directory="{PROFILE}"'
        )
        Start-Sleep -Seconds 12
    }} else {{
        Write-Host 'Brave not found - cannot attach.'
    }}
}}

if (-not (Test-DebugPort)) {{
    Write-Host 'No debug port; skipping this run.'
    exit 1
}}

# Load .env into the environment
$envFile = Join-Path '{PROJECT_ROOT}' '.env'
if (Test-Path $envFile) {{
    Get-Content $envFile | ForEach-Object {{
        if ($_ -match '^\\s*([A-Za-z_][A-Za-z0-9_]*)\\s*=\\s*(.*)\\s*$') {{
            $name = $matches[1]
            $value = $matches[2].Trim()
            if (-not $value.StartsWith('#')) {{ Set-Item -Path "Env:$name" -Value $value }}
        }}
    }}
}}

$env:BROWSER_CDP_ENDPOINT = '127.0.0.1:{PORT}'
$env:PYTHONIOENCODING = 'utf-8'

$python = Join-Path '{PROJECT_ROOT}' '.venv\\Scripts\\python.exe'
if (-not (Test-Path $python)) {{ $python = 'python' }}

# 1) propose - track badges and surface articles for approval
Write-Host '=== propose ==='
& $python -m agent.main --propose

# 2) inbox - act on any approvals already given
Write-Host '=== inbox ==='
& $python -m agent.main --inbox
"""
    RUNNER.write_text(content, encoding="utf-8")


def install() -> int:
    write_runner()
    print(f"Wrote runner: {RUNNER}")

    code, out = ps(
        f"Unregister-ScheduledTask -TaskName '{TASK_NAME}' -Confirm:$false -ErrorAction SilentlyContinue"
    )

    register = f"""
$action = New-ScheduledTaskAction -Execute 'powershell' `
  -Argument '-NoProfile -ExecutionPolicy Bypass -File "{RUNNER}"'
$trigger = New-ScheduledTaskTrigger -Daily -At '{HOUR:02d}:{MINUTE:02d}'
$settings = New-ScheduledTaskSettingsSet `
  -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
  -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Minutes 30)
Register-ScheduledTask -TaskName '{TASK_NAME}' -Action $action -Trigger $trigger `
  -Settings $settings -Description 'Daily AWS Builder badge tracking via your signed-in Brave browser'
"""
    code, out = ps(register)
    if code != 0:
        print("Could not register the task. Try running PowerShell as administrator.")
        print(out)
        return 1

    print(f"\nInstalled '{TASK_NAME}' - runs daily at {HOUR:02d}:{MINUTE:02d}")
    print("Requires Brave to be startable; the runner launches it if needed.")
    print(f"Manual run now:  powershell -ExecutionPolicy Bypass -File \"{RUNNER}\"")
    return 0


def remove() -> int:
    ps(f"Unregister-ScheduledTask -TaskName '{TASK_NAME}' -Confirm:$false -ErrorAction SilentlyContinue")
    print(f"Removed '{TASK_NAME}'")
    return 0


def status() -> int:
    code, out = ps(f"Get-ScheduledTask -TaskName '{TASK_NAME}' | Get-ScheduledTaskInfo | Format-List")
    print(out or "No task installed.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--remove", action="store_true")
    parser.add_argument("--status", action="store_true")
    args = parser.parse_args()
    if args.remove:
        return remove()
    if args.status:
        return status()
    return install()


if __name__ == "__main__":
    sys.exit(main())