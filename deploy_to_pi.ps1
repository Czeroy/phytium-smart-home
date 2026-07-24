param(
    [string]$HostName = "10.137.2.101",
    [string]$UserName = "goushi",
    [string]$RemoteDir = "/home/goushi/smart-home",
    [switch]$Restart,
    [switch]$StartJailhouse
)

$ErrorActionPreference = "Stop"
$SourceDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ManifestPath = Join-Path $SourceDir "deployment_manifest.json"

if (-not (Test-Path -LiteralPath $ManifestPath -PathType Leaf)) {
    throw "Missing deployment manifest: $ManifestPath"
}

$manifest = Get-Content -LiteralPath $ManifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
$files = @()
foreach ($category in $manifest.PSObject.Properties) {
    $files += @($category.Value)
}
$files = @($files | Select-Object -Unique)

$missing = $files | Where-Object {
    -not (Test-Path -LiteralPath (Join-Path $SourceDir $_) -PathType Leaf)
}
if ($missing.Count -gt 0) {
    Write-Host "Missing local files:" -ForegroundColor Red
    $missing | ForEach-Object { Write-Host "  $_" -ForegroundColor Red }
    exit 1
}

if (-not (Get-Command tar -ErrorAction SilentlyContinue)) {
    throw "Windows tar command was not found. Run on Windows 10/11 or install bsdtar."
}

$target = "$UserName@$HostName"
Write-Host "Deploy target: ${target}:$RemoteDir" -ForegroundColor Cyan

$archiveName = "smart-home-deploy-$PID.tar.gz"
$localArchive = Join-Path ([System.IO.Path]::GetTempPath()) $archiveName
$remoteArchive = "/tmp/$archiveName"

try {
    Write-Host "Packing $($files.Count) deployment files..." -ForegroundColor Cyan
    Push-Location $SourceDir
    try {
        & tar -czf $localArchive -- @files
        if ($LASTEXITCODE -ne 0) {
            throw "tar failed with exit code $LASTEXITCODE"
        }
    }
    finally {
        Pop-Location
    }

    Write-Host "Preparing remote directory and backing up runtime configuration..." -ForegroundColor Cyan
    ssh $target "mkdir -p '$RemoteDir' && cd '$RemoteDir' && if [ -f ir_codes.json ]; then cp ir_codes.json ir_codes.backup.`$(date +%F-%H%M%S).json; fi && if [ -f data/ir_forwarder_codes.json ]; then cp data/ir_forwarder_codes.json data/ir_forwarder_codes.backup.`$(date +%F-%H%M%S).json; fi"
    if ($LASTEXITCODE -ne 0) {
        throw "Could not prepare remote directory"
    }

    Write-Host "Uploading one deployment archive..." -ForegroundColor Cyan
    scp $localArchive "${target}:$remoteArchive"
    if ($LASTEXITCODE -ne 0) {
        throw "scp failed with exit code $LASTEXITCODE"
    }

    Write-Host "Extracting deployment archive..." -ForegroundColor Cyan
    ssh $target "set -e; mkdir -p '$RemoteDir'; tar -xzf '$remoteArchive' -C '$RemoteDir'; rm -f '$remoteArchive'; chmod +x '$RemoteDir'/*.sh"
    if ($LASTEXITCODE -ne 0) {
        throw "Remote extraction failed"
    }

    if ($Restart) {
        Write-Host "Checking Python syntax and restarting services..." -ForegroundColor Cyan
        ssh -t $target "cd '$RemoteDir' && python3 -m py_compile app.py device_control.py dht11_sensor.py runtime_store.py realtime_bridge.py && sudo bash deploy_remote_finalize.sh '$RemoteDir'"
        if ($LASTEXITCODE -ne 0) {
            throw "Remote service installation failed"
        }
    }

    if ($StartJailhouse) {
        Write-Host "Starting Jailhouse mailbox channel..." -ForegroundColor Cyan
        ssh -t $target "cd '$RemoteDir' && ./jailhouse_mailbox_channel_start.sh"
        if ($LASTEXITCODE -ne 0) {
            throw "Jailhouse mailbox startup failed"
        }
    }

    Write-Host "Done." -ForegroundColor Green
    Write-Host "Open: http://$HostName:5000" -ForegroundColor Green
}
finally {
    if (Test-Path -LiteralPath $localArchive) {
        Remove-Item -LiteralPath $localArchive -Force
    }
}
