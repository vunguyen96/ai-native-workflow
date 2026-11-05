param(
    [ValidateRange(1, 65535)]
    [int]$Port = 8765
)

$serverPath = Join-Path $PSScriptRoot "server.py"

if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
    throw "Python was not found on PATH. Install Python or add it to PATH, then try again."
}

Write-Host "Starting the workflow playbook. Press Ctrl+C to stop."
& python $serverPath $Port
exit $LASTEXITCODE
