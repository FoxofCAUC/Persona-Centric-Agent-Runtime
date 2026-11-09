param([string]$RuntimeHome = 'D:\Persona-Centric Agent Runtime')
$ErrorActionPreference = 'Stop'
$homeRoot = [IO.Path]::GetFullPath($RuntimeHome).TrimEnd('\')
if ($homeRoot -ne 'D:\Persona-Centric Agent Runtime') { throw 'Unexpected M8.E runtime root' }
$evidenceRoot = Join-Path $homeRoot 'exports\acceptance\M8.E-20261108'
$exitProof = Get-Content -Raw -LiteralPath (Join-Path $evidenceRoot 'formal-exit-result.json') | ConvertFrom-Json
if (-not $exitProof.passed) { throw 'Verified normal formal exit required before cleanup' }
$targets = @(
    (Join-Path $homeRoot 'tmp\pcar-m8e-acceptance'),
    (Join-Path $homeRoot 'tmp\pcar-m8e-component-probe-v2'),
    (Join-Path $homeRoot 'tmp\pcar-m8e-component-preview'),
    (Join-Path $homeRoot 'tmp\pcar-m8e-formal-component-stage'),
    (Join-Path $evidenceRoot 'formal-copy-migration\copy'),
    (Join-Path $evidenceRoot 'formal-copy-migration\source23.sqlite3'),
    (Join-Path $evidenceRoot 'formal-backup-restore\copy'),
    (Join-Path $evidenceRoot 'models.dpapi.json'),
    (Join-Path $evidenceRoot 'formal-host.log')
)
$allowedParents = @((Join-Path $homeRoot 'tmp'), $evidenceRoot)
foreach ($target in $targets) {
    $fullTarget = [IO.Path]::GetFullPath($target)
    if (-not ($allowedParents | Where-Object { $fullTarget.StartsWith($_ + '\', [StringComparison]::OrdinalIgnoreCase) })) {
        throw "Cleanup path escaped its declared stage root: $fullTarget"
    }
    if (Test-Path -LiteralPath $fullTarget) {
        $item = Get-Item -Force -LiteralPath $fullTarget
        if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'Refusing a reparse-point cleanup target' }
        $resolvedTarget = (Resolve-Path -LiteralPath $fullTarget).ProviderPath
        if ([IO.Path]::GetFullPath($resolvedTarget) -ne $fullTarget) { throw 'Resolved cleanup target changed' }
        Remove-Item -Force -Recurse -LiteralPath $fullTarget
    }
}
$remaining = @($targets | Where-Object { Test-Path -LiteralPath $_ })
if ($remaining.Count) { throw 'Some declared acceptance materials remain' }
[pscustomobject]@{ complete = $true; targets = $targets; production_history_preserved = $true; production_backups_preserved = $true } |
    ConvertTo-Json -Depth 5 | Set-Content -Encoding utf8 -LiteralPath (Join-Path $evidenceRoot 'cleanup-result.json')
