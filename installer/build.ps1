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
Copy-Item "installer\launcher.ps1" "$payload\launch_midibridge.ps1" -Force
New-Item -ItemType Directory -Force -Path "$payload\ReaperScripts" | Out-Null
Copy-Item "src\adapters\reaper\JarvisBridge.lua" "$payload\ReaperScripts\JarvisBridge.lua" -Force
Copy-Item "src\adapters\reaper\__startup.lua" "$payload\ReaperScripts\__startup.lua" -Force
New-Item -ItemType Directory -Force -Path "$payload\StudioOne" | Out-Null
$studioSrc = "src\adapters\studioone"
$studioFiles = @(
    "classfactory.xml",
    "metainfo.xml",
    "Jarvis Bridge.surfacedata",
    "jarvisbridge.device",
    "JarvisBridge.device",
    "jarvisbridge.js",
    "JarvisBridge.js",
    "jarvisbridge.surface.xml",
    "JarvisBridge.surface.xml",
    "JarvisBridgeMappings.surface.xml",
    "JarvisBridgeControls.surface.xml",
    "JarvisBridgeDevice.js",
    "JarvisBridgeComponent.js",
    "JarvisBridge.txt"
)
foreach ($f in $studioFiles) {
    $srcPath = Join-Path $studioSrc $f
    if (Test-Path $srcPath) {
        Copy-Item $srcPath (Join-Path "$payload\StudioOne" $f) -Force
    }
}
# Always include the current Control Link surface data from the local Studio One profile when available.
$surfaceData = "C:\Users\brown\AppData\Roaming\PreSonus\Studio One 6\Surface Data[1]\Jarvis Bridge.surfacedata"
if (Test-Path $surfaceData) {
    Copy-Item $surfaceData "$payload\StudioOne\Jarvis Bridge.surfacedata" -Force
}
# Optional skin assets
if (Test-Path "$studioSrc\skin") {
    New-Item -ItemType Directory -Force -Path "$payload\StudioOne\skin" | Out-Null
    Copy-Item "$studioSrc\skin\*" "$payload\StudioOne\skin\" -Recurse -Force -ErrorAction SilentlyContinue
}
if (-not (Test-Path "$payload\loopMIDISetup.zip")) {
    throw "installer\payload\loopMIDISetup.zip is missing (the virtual MIDI driver package)"
}

Write-Host "3/3  Building the installer..."
python -m PyInstaller --noconfirm --onefile --windowed --uac-admin `
    --name MidiBridgeSetup --distpath installer\dist --workpath installer\build `
    --specpath installer `
    --add-data "$payload\MidiBridge.exe;." `
    --add-data "$payload\launch_midibridge.ps1;." `
    --add-data "$payload\ReaperScripts;ReaperScripts" `
    --add-data "$payload\StudioOne;StudioOne" `
    --add-data "$payload\AbletonBridgeMixer;AbletonBridgeMixer" `
    --add-data "$payload\loopMIDISetup.zip;." `
    installer\installer.py | Select-Object -Last 3

$setup = "installer\dist\MidiBridgeSetup.exe"
if (-not (Test-Path $setup)) { throw "build failed: $setup missing" }
Get-Item $setup | Select-Object FullName, @{n='MB';e={[math]::Round($_.Length/1MB,1)}}
