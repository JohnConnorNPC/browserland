# Run a webterm agent on Windows (ConPTY via pywinpty).
#
#   $env:BROWSERLAND_BROKER_URL = 'ws://host:4445/browserland'
#   $env:WEB_TERMINAL_TOKEN   = '...'         # REQUIRED (#142), any broker
#   .\launchers\run-agent.ps1 [agent opts] [-- command...]
#
# Defaults: broker ws://127.0.0.1:4445/browserland, command %COMSPEC%.
$ErrorActionPreference = 'Stop'

$repoDir = Split-Path -Parent $PSScriptRoot
$env:PYTHONPATH = if ($env:PYTHONPATH) { "$repoDir;$($env:PYTHONPATH)" } else { $repoDir }

# A token is REQUIRED on every connection, including loopback (#142). Fall back
# to the sidecar the broker mints beside its state store so a hand-started agent
# on the same box needs no setup; fail loudly rather than dialling a broker that
# will only close us with 4401.
if (-not $env:WEB_TERMINAL_TOKEN) {
    $tokenFile = if ($env:WEB_TERMINAL_TOKEN_FILE) { $env:WEB_TERMINAL_TOKEN_FILE }
                 else { Join-Path $repoDir 'webterm_token.json' }
    if (Test-Path $tokenFile) {
        try {
            $env:WEB_TERMINAL_TOKEN = (Get-Content -Raw $tokenFile | ConvertFrom-Json).auth_token
        } catch { }
    }
}
if (-not $env:WEB_TERMINAL_TOKEN) {
    Write-Error ("[run-agent] no broker token. Set `$env:WEB_TERMINAL_TOKEN (or point " +
                 "`$env:WEB_TERMINAL_TOKEN_FILE at the broker's webterm_token.json). " +
                 "Print it with: python -m webterm.broker --print-token")
    exit 1
}

python -c "import winpty" 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host '[run-agent] installing pywinpty + websockets' -ForegroundColor Yellow
    python -m pip install --quiet "pywinpty>=2" "websockets>=12"
}

python -m webterm.agent @args
exit $LASTEXITCODE
