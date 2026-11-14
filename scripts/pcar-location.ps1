# Shared launcher helpers, compatible with Windows PowerShell 5.1.
function Get-PcarRuntimeHome {
    param([string]$RuntimeHome, [switch]$Interactive)
    $sourceRoot = Split-Path -Parent $PSScriptRoot
    if (-not $RuntimeHome) { $RuntimeHome = $env:PERSONA_RUNTIME_HOME }
    $locator = Join-Path $sourceRoot '.pcar-local.json'
    if (-not $RuntimeHome -and (Test-Path -LiteralPath $locator)) {
        $value = Get-Content -LiteralPath $locator -Raw -Encoding UTF8 | ConvertFrom-Json
        if ($value.version -ne 1) { throw '本机启动定位配置版本不兼容，请重新准备环境。' }
        $RuntimeHome = $value.runtime_home
    }
    if (-not $RuntimeHome -and $Interactive) {
        Add-Type -AssemblyName System.Windows.Forms
        $dialog = New-Object System.Windows.Forms.FolderBrowserDialog
        $dialog.Description = '请选择PCAR专用数据目录，与源码目录分开。'
        try {
            if ($dialog.ShowDialog() -ne [System.Windows.Forms.DialogResult]::OK) { return $null }
            $RuntimeHome = $dialog.SelectedPath
        } finally { $dialog.Dispose() }
    }
    if (-not $RuntimeHome) { throw '尚未选择数据目录；请使用 -RuntimeHome 或从桌面入口完成首次准备。' }
    if ($RuntimeHome -notmatch '^[A-Za-z]:[\\/]') { throw '请选择本地磁盘中的绝对数据目录。' }
    return [System.IO.Path]::GetFullPath($RuntimeHome)
}

function Get-PcarPython {
    param([string]$RuntimeHome)
    if ($env:PCAR_PYTHON) { return $env:PCAR_PYTHON }
    return Join-Path $RuntimeHome '.venv\Scripts\python.exe'
}

function Get-PcarBasePython {
    param([string]$BasePython)
    if ($BasePython) { return $BasePython }
    $launcher = Get-Command py.exe -ErrorAction SilentlyContinue
    if ($launcher) {
        $found = & $launcher.Source -3.12 -I -B -c 'import json,sys;print(json.dumps(sys.executable,ensure_ascii=True))' 2>$null
        if ($LASTEXITCODE -eq 0 -and $found) { return ($found | ConvertFrom-Json) }
    }
    $python = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($python -and $python.Source -notlike '*WindowsApps*') { return $python.Source }
    throw '需要含Tkinter的Python 3.12 64位。请先安装Python，再重新准备。'
}

function Set-PcarProcessEnvironment {
    param([string]$RuntimeHome)
    $env:PERSONA_RUNTIME_HOME = $RuntimeHome
    $env:PYTHONPATH = Split-Path -Parent $PSScriptRoot
    $env:PYTHONDONTWRITEBYTECODE = '1'
    $env:PYTHONUTF8 = '1'
    $env:PYTHONIOENCODING = 'utf-8'
    [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
}
