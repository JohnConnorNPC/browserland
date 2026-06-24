# Run the webterm broker on Windows.
#
#   $env:WEB_TERMINAL_TOKEN = '...'   # optional; gates /ws,/sessions,/launch
#   .\launchers\run-broker.ps1 [--host H] [--port P] [--config path]
$ErrorActionPreference = 'Stop'

# Refresh PATH from the registry (Machine then User) so the broker — and every
# agent it launches — sees programs installed since this PowerShell logged in,
# without a re-login (todo task 17). webterm/agent/env_util.py re-reads it again
# per spawn; this also fixes `python` resolution for the line below.
try {
    $machinePath = [Environment]::GetEnvironmentVariable('Path', 'Machine')
    $userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
    $merged = @($machinePath, $userPath) | Where-Object { $_ } | ForEach-Object { $_ -split ';' } |
        Where-Object { $_ -and $_.Trim() } | Select-Object -Unique
    if ($merged) { $env:PATH = ($merged -join ';') }
} catch {
    Write-Warning "PATH refresh skipped: $_"
}

$repoDir = Split-Path -Parent $PSScriptRoot
$env:PYTHONPATH = if ($env:PYTHONPATH) { "$repoDir;$($env:PYTHONPATH)" } else { $repoDir }

python -m webterm.broker @args
exit $LASTEXITCODE
