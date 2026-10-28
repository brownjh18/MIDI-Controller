# Builds the Windows setup: first the bridge itself (no Python needed on the
# target laptop), then the installer that carries it.
#
#   powershell -ExecutionPolicy Bypass -File installer\build.ps1
#
# Output: installer\dist\MidiBridgeSetup.exe  (asks for elevation on start)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$payload = Join-Path $root "installer\payload"

Write-Host "1/3  Building the bridge exe..."
python -m PyInstaller --noconfirm --onefile --console --name MidiBridge `
    --paths src --distpath installer\dist --workpath installer\build `
    --specpath installer src\main.py | Select-Object -Last 3

Write-Host "2/3  Assembling the payload..."
New-Item -ItemType Directory -Force -Path "$payload\AbletonBridgeMixer" | Out-Null
Copy-Item "installer\dist\MidiBridge.exe" "$payload\MidiBridge.exe" -Force
Copy-Item "src\ableton\AbletonBridgeMixer\__init__.py" `
    "$payload\AbletonBridgeMixer\__init__.py" -Force
if (-not (Test-Path "$payload\loopMIDISetup.zip")) {
    throw "installer\payload\loopMIDISetup.zip is missing (the virtual MIDI driver package)"
}

Write-Host "3/3  Building the installer..."
python -m PyInstaller --noconfirm --onefile --windowed `
    --name MidiBridgeSetup --distpath installer\dist --workpath installer\build `
    --specpath installer `
    --add-data "$payload\MidiBridge.exe;." `
    --add-data "$payload\AbletonBridgeMixer;AbletonBridgeMixer" `
    --add-data "$payload\loopMIDISetup.zip;." `
    installer\installer.py | Select-Object -Last 3

$setup = "installer\dist\MidiBridgeSetup.exe"
if (-not (Test-Path $setup)) { throw "build failed: $setup missing" }
Get-Item $setup | Select-Object FullName, @{n='MB';e={[math]::Round($_.Length/1MB,1)}}
