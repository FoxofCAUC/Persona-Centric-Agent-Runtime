param([string]$RuntimeHome, [ValidateSet('tk', 'webview')][string]$Frontend = 'tk')
$ErrorActionPreference = 'Stop'
try {
    . (Join-Path $PSScriptRoot 'pcar-location.ps1')
    $projectRoot = Split-Path -Parent $PSScriptRoot
    $RuntimeHome = Get-PcarRuntimeHome -RuntimeHome $RuntimeHome -Interactive
    if (-not $RuntimeHome) { exit 0 }
    Set-PcarProcessEnvironment -RuntimeHome $RuntimeHome
    $python = Get-PcarPython -RuntimeHome $RuntimeHome
    if (-not (Test-Path -LiteralPath (Join-Path $RuntimeHome 'config\workbench.json') -PathType Leaf)) {
        $prepare = Join-Path $PSScriptRoot 'Prepare-Desktop.ps1'
        $options = @('-RuntimeHome', $RuntimeHome, '-Frontend', $Frontend)
        $publicModel = Join-Path $projectRoot 'public-model'
        if ($Frontend -eq 'webview' -and (Test-Path -LiteralPath $publicModel -PathType Container)) {
            $options += @('-LocalModelSource', $publicModel)
        }
        & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $prepare @options
        if ($LASTEXITCODE -ne 0 -and -not (Test-Path -LiteralPath (Join-Path $RuntimeHome 'config\workbench.json') -PathType Leaf)) {
            throw '环境准备未完成，请查看所选数据目录中的准备日志。'
        }
        $python = Get-PcarPython -RuntimeHome $RuntimeHome
    }
    $verifier = $python
    if (-not (Test-Path -LiteralPath $verifier -PathType Leaf)) { $verifier = Get-PcarBasePython }
    Push-Location -LiteralPath $projectRoot
    try {
        $ErrorActionPreference = 'Continue'
        $verification = & $verifier -B -m persona_runtime --json --home $RuntimeHome prepare --register-existing --wake-existing --frontend $Frontend 2>&1
        $verifyCode = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = 'Stop'
        Pop-Location
    }
    if ($verifyCode -ne 0) { throw ("实例准备未完成。" + [Environment]::NewLine + ($verification -join [Environment]::NewLine)) }
    $result = ($verification -join [Environment]::NewLine) | ConvertFrom-Json
    if ($result.woke_existing) { exit 0 }
    $pythonw = Join-Path (Split-Path -Parent $python) 'pythonw.exe'
    if (-not (Test-Path -LiteralPath $pythonw -PathType Leaf)) { throw '当前实例缺少 pythonw，请修复环境。' }
    Start-Process -FilePath $pythonw -ArgumentList @('-B', '-m', 'persona_runtime', 'gui', '--frontend', $Frontend) -WorkingDirectory $projectRoot -WindowStyle Hidden
} catch {
    Add-Type -AssemblyName System.Windows.Forms
    [System.Windows.Forms.MessageBox]::Show($_.Exception.Message, 'Persona Workbench') | Out-Null
    exit 1
}
