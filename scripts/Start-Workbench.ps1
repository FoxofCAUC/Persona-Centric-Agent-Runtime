$ErrorActionPreference = 'Stop'
try {
    $projectRoot = Split-Path -Parent $PSScriptRoot
    $runtimeHome = if ($env:PERSONA_RUNTIME_HOME) { $env:PERSONA_RUNTIME_HOME } else { 'D:\Persona-Centric Agent Runtime' }
    $pythonw = 'D:\Persona-Centric Agent Runtime\.venv\Scripts\pythonw.exe'
    if (-not (Test-Path -LiteralPath $pythonw)) { throw 'Project environment is not prepared.' }
    $env:PERSONA_RUNTIME_HOME = $runtimeHome
    $env:PYTHONPATH = $projectRoot
    $env:PYTHONDONTWRITEBYTECODE = '1'
    $env:PYTHONIOENCODING = 'utf-8'
    Start-Process -FilePath $pythonw -ArgumentList @('-B', '-m', 'persona_runtime', 'gui') -WorkingDirectory $projectRoot -WindowStyle Hidden
} catch {
    Add-Type -AssemblyName System.Windows.Forms
    [System.Windows.Forms.MessageBox]::Show($_.Exception.Message, 'Persona Workbench') | Out-Null
    exit 1
}
