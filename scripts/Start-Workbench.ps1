param([string]$RuntimeHome, [ValidateSet('tk', 'webview')][string]$Frontend = 'tk')
$ErrorActionPreference = 'Stop'
try {
    . (Join-Path $PSScriptRoot 'pcar-location.ps1')
    $projectRoot = Split-Path -Parent $PSScriptRoot
    $RuntimeHome = Get-PcarRuntimeHome -RuntimeHome $RuntimeHome -Interactive
    if (-not $RuntimeHome) { exit 0 }
    Set-PcarProcessEnvironment -RuntimeHome $RuntimeHome
    $python = Get-PcarPython -RuntimeHome $RuntimeHome
    $existing = Test-Path -LiteralPath (Join-Path $RuntimeHome 'config\workbench.json') -PathType Leaf
    if (-not $existing) {
        $prepare = Join-Path $PSScriptRoot 'Prepare-Desktop.ps1'
        $options = @('-RuntimeHome', $RuntimeHome, '-Frontend', $Frontend)
        $publicModel = Join-Path $projectRoot 'public-model'
        if ($Frontend -eq 'webview' -and (Test-Path -LiteralPath $publicModel -PathType Container)) {
            $options += @('-LocalModelSource', $publicModel)
        }
        & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $prepare @options
        if ($LASTEXITCODE -ne 0) { throw '环境准备未完成，请查看所选数据目录中的准备日志。' }
        $python = Get-PcarPython -RuntimeHome $RuntimeHome
    }
    if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
        throw '当前实例缺少已准备的 Python 环境。请先修复该实例的运行环境。'
    }
    if ($Frontend -eq 'webview') {
        $previous = $ErrorActionPreference
        $ErrorActionPreference = 'Continue'
        try {
            $check = & $python -B -m persona_runtime --home $RuntimeHome prepare --ensure-webview 2>&1
            $prepareExit = $LASTEXITCODE
        } finally {
            $ErrorActionPreference = $previous
        }
        if ($prepareExit -ne 0) { throw ($check -join [Environment]::NewLine) }
    }
    $pythonw = Join-Path (Split-Path -Parent $python) 'pythonw.exe'
    if (-not (Test-Path -LiteralPath $pythonw)) { throw '当前实例缺少pythonw，请修复环境。' }
    & $python -B -m persona_runtime --home $RuntimeHome prepare --register-existing | Out-Null
    if ($LASTEXITCODE -ne 0) { throw '实例验证失败，未启动桌面。' }
    Start-Process -FilePath $pythonw -ArgumentList @('-B', '-m', 'persona_runtime', 'gui', '--frontend', $Frontend) -WorkingDirectory $projectRoot -WindowStyle Hidden
} catch {
    Add-Type -AssemblyName System.Windows.Forms
    [System.Windows.Forms.MessageBox]::Show($_.Exception.Message, 'Persona Workbench') | Out-Null
    exit 1
}
