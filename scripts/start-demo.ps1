# Start the local synthetic evaluation after a reboot. Run from any directory.
[CmdletBinding()]
param()
$ErrorActionPreference = 'Stop'
$repo = Split-Path $PSScriptRoot -Parent
$backend = Join-Path $repo 'backend'
$frontend = Join-Path $repo 'frontend'
$python = Join-Path $backend '.venv/Scripts/python.exe'
$vite = Join-Path $frontend 'node_modules/vite/bin/vite.js'
$store = Join-Path $env:LOCALAPPDATA 'CaseManagement-Synthetic-Demo'
if (!(Test-Path -LiteralPath $python) -or !(Test-Path -LiteralPath $vite)) {
    throw 'Install the backend and frontend dependencies from the project README before starting the demo.'
}
New-Item -ItemType Directory -Force $store | Out-Null
# Use a child scope for the demo profile. Restore the caller's environment on exit.
$keys = 'APP_ENV','SYNTHETIC_ENVIRONMENT','DATABASE_URL','DOCUMENTS_PATH','OUTBOX_DISPATCHER_ENABLED','VITE_DEMO_WORKSPACES','VITE_LOCAL_API_PORT','VITE_DEV_USER'
$previous = @{}
foreach ($key in $keys) { $previous[$key] = [Environment]::GetEnvironmentVariable($key, 'Process') }

function Test-Listener([int]$Port) {
    return [bool](Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue)
}
function Wait-Demo([string]$Url) {
    for ($attempt = 0; $attempt -lt 30; $attempt++) {
        try { return Invoke-RestMethod $Url -TimeoutSec 2 } catch { Start-Sleep -Milliseconds 400 }
    }
    throw "Could not reach $Url. See service logs under $store."
}
try {
    $env:APP_ENV = 'local'
    $env:SYNTHETIC_ENVIRONMENT = '1'
    $env:DATABASE_URL = 'sqlite+aiosqlite:///' + ((Join-Path $store 'demo.db') -replace '\\','/')
    $env:DOCUMENTS_PATH = Join-Path $store 'documents'
    $env:OUTBOX_DISPATCHER_ENABLED = 'false'
    if (!(Test-Listener 8002)) {
        Push-Location $backend
        try {
            & $python -m alembic upgrade head
            if ($LASTEXITCODE) { throw 'Demo database migration failed.' }
            & $python -m app.cli seed
            if ($LASTEXITCODE) { throw 'Synthetic seed failed.' }
        } finally { Pop-Location }
        Start-Process $python -WindowStyle Hidden -WorkingDirectory $backend -ArgumentList '-m uvicorn app.main:app --host 127.0.0.1 --port 8002' -RedirectStandardOutput "$store/api-8002.log" -RedirectStandardError "$store/api-8002.err"
    }
    $health = Wait-Demo 'http://127.0.0.1:8002/api/v1/health'
    if (!$health.synthetic_environment) { throw 'Port 8002 must serve the synthetic API.' }
    $env:VITE_DEMO_WORKSPACES = '1'
    $env:VITE_LOCAL_API_PORT = '8002'
    foreach ($entry in @(@(5173,'supervisor'),@(5174,'intake'),@(5175,'process_admin'))) {
        $port, $actor = $entry
        if (!(Test-Listener $port)) {
            $env:VITE_DEV_USER = $actor
            Start-Process (Get-Command node.exe).Source -WindowStyle Hidden -WorkingDirectory $frontend -ArgumentList "node_modules/vite/bin/vite.js --host 127.0.0.1 --port $port --strictPort" -RedirectStandardOutput "$store/$actor-ui.log" -RedirectStandardError "$store/$actor-ui.err"
        }
        $health = Wait-Demo "http://127.0.0.1:$port/api/v1/health"
        if (!$health.synthetic_environment) { throw "Port $port must serve the synthetic demo." }
        Write-Output "$actor workspace  http://127.0.0.1:$port/home"
    }
} finally {
    foreach ($key in $keys) { [Environment]::SetEnvironmentVariable($key, $previous[$key], 'Process') }
}
