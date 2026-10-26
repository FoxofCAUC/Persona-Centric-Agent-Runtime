$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$python = 'D:\Persona-Centric Agent Runtime\.venv\Scripts\python.exe'
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
