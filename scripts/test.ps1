$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
. (Join-Path $PSScriptRoot 'pcar-location.ps1')
$runtimeHome = Get-PcarRuntimeHome
$python = Get-PcarPython -RuntimeHome $runtimeHome
$previousEncoding = $env:PYTHONIOENCODING
try {
    $env:PYTHONIOENCODING = 'utf-8'
    Push-Location -LiteralPath $projectRoot
    try {
        & $python -B -m unittest discover -s tests -v
        if ($LASTEXITCODE -ne 0) { throw 'Project verification failed' }
    } finally {
        Pop-Location
    }
} finally {
    $env:PYTHONIOENCODING = $previousEncoding
}
