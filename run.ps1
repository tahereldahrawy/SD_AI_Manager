# Start the Subscription Manager over HTTPS on the LAN.
# First run sets up the venv, installs deps, and generates a self-signed cert.
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$port = 8443
if ($args.Count -ge 1) { $port = $args[0] }

# 1. venv
if (-not (Test-Path ".venv")) {
    Write-Host "Creating virtual environment..."
    python -m venv .venv
}
$py = ".\.venv\Scripts\python.exe"

# 2. deps
Write-Host "Installing dependencies..."
& $py -m pip install --quiet --upgrade pip
& $py -m pip install --quiet -r requirements.txt

# 3. cert (self-signed, LAN). Pass extra hostnames/IPs as you like.
if (-not (Test-Path "data\cert.pem")) {
    Write-Host "Generating self-signed certificate..."
    & $py scripts\gen_cert.py
}

# 4. run with TLS, bound to all interfaces so other LAN machines can reach it
Write-Host "Starting on https://0.0.0.0:$port  (Ctrl+C to stop)"
& $py -m uvicorn app.main:app --host 0.0.0.0 --port $port `
    --ssl-keyfile data\key.pem --ssl-certfile data\cert.pem
