# Run a webterm agent on Windows (ConPTY via pywinpty).
#
#   $env:BROWSERLAND_BROKER_URL = 'ws://host:4445/browserland'
#   $env:WEB_TERMINAL_TOKEN   = '...'         # only for non-loopback brokers
#   .\launchers\run-agent.ps1 [agent opts] [-- command...]
#
# Defaults: broker ws://127.0.0.1:4445/browserland, command %COMSPEC%.
$ErrorActionPreference = 'Stop'

$repoDir = Split-Path -Parent $PSScriptRoot
$env:PYTHONPATH = if ($env:PYTHONPATH) { "$repoDir;$($env:PYTHONPATH)" } else { $repoDir }

python -c "import winpty" 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host '[run-agent] installing pywinpty + websockets' -ForegroundColor Yellow
    python -m pip install --quiet "pywinpty>=2" "websockets>=12"
}

python -m webterm.agent @args
exit $LASTEXITCODE
