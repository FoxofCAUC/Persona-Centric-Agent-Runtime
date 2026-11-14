param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$RuntimeArgs = @('paths')
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
. (Join-Path $PSScriptRoot 'pcar-location.ps1')
$runtimeHome = Get-PcarRuntimeHome
$runtimePython = Get-PcarPython -RuntimeHome $runtimeHome
if (-not (Test-Path -LiteralPath $runtimePython)) {
    throw "Project Python environment is missing: $runtimePython"
}

# Keep these settings scoped to this script and its child process.
$settings = @{
    PERSONA_RUNTIME_HOME = $runtimeHome
    UV_CACHE_DIR = (Join-Path $runtimeHome 'cache\uv')
    PIP_CACHE_DIR = (Join-Path $runtimeHome 'cache\pip')
    PYTHONPYCACHEPREFIX = (Join-Path $runtimeHome 'cache\pycache')
    PYTHONIOENCODING = 'utf-8'
    TEMP = (Join-Path $runtimeHome 'tmp')
    TMP = (Join-Path $runtimeHome 'tmp')
}
$previous = @{}
try {
    foreach ($key in $settings.Keys) {
        $previous[$key] = [Environment]::GetEnvironmentVariable($key, 'Process')
        [Environment]::SetEnvironmentVariable($key, $settings[$key], 'Process')
    }
    Push-Location -LiteralPath $projectRoot
    try {
        & $runtimePython -B -m persona_runtime @RuntimeArgs
        if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    } finally {
        Pop-Location
    }
} finally {
    foreach ($key in $previous.Keys) {
        [Environment]::SetEnvironmentVariable($key, $previous[$key], 'Process')
    }
}
