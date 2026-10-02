param(
    [ValidateSet('demo','serial')][string]$Mode = 'demo',
    [string]$SerialPort = '',
    [int]$Port = 8770,
    [string]$OllamaModel = '',
    [string]$VisionModel = '',
    [switch]$NoBrowser,
    [switch]$Check,
    [switch]$Backup
)
$ErrorActionPreference = 'Stop'
$bundledPython = Join-Path $env:USERPROFILE '.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
if (Test-Path -LiteralPath $bundledPython) { $pythonExe = $bundledPython }
else { $pythonExe = (Get-Command python -ErrorAction Stop).Source }
$previousPath = $env:PYTHONPATH
$previousEncoding = $env:PYTHONIOENCODING
$previousLocation = Get-Location
try {
    Set-Location -LiteralPath $PSScriptRoot
    $env:PYTHONPATH = Join-Path $PSScriptRoot '.runtime'
    $env:PYTHONIOENCODING = 'utf-8'
    if ($Check -and $Backup) { throw 'Use either -Check or -Backup, not both.' }
    if ($Check -or $Backup) {
        $maintenanceAction = if ($Check) { 'check' } else { 'backup' }
        & $pythonExe -m room.maintenance $maintenanceAction --mode $Mode
        exit $LASTEXITCODE
    }
    $launchArgs = @('-m', 'room', '--mode', $Mode, '--port', "$Port")
    if ($Mode -eq 'serial') {
        if (-not $SerialPort) { throw 'Serial mode requires -SerialPort COMx.' }
        $launchArgs += @('--serial-port', $SerialPort)
    }
    if ($OllamaModel) { $launchArgs += @('--ollama-model', $OllamaModel) }
    if ($VisionModel) { $launchArgs += @('--vision-model', $VisionModel) }
    Write-Host "Open http://127.0.0.1:$Port after the server is ready. Press Ctrl+C to stop."
    if (-not $NoBrowser) { $launchArgs += '--open-browser' }
    & $pythonExe @launchArgs
    exit $LASTEXITCODE
} finally {
    $env:PYTHONPATH = $previousPath
    $env:PYTHONIOENCODING = $previousEncoding
    Set-Location $previousLocation
}
