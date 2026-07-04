# Hive Mind installer (Windows PowerShell).
#
#   irm https://raw.githubusercontent.com/TejesMunde/hive-ai/main/install.ps1 | iex
#
# Phase A distribution: installs the Python package (requires Python >= 3.10).
# Prefers pipx (isolated), falls back to `pip install --user`.
$ErrorActionPreference = "Stop"

$Pkg = "hive-ai"

function Find-Python {
    foreach ($c in @(@("py", @("-3")), @("python", @()), @("python3", @()))) {
        $cmd, $pre = $c
        if (Get-Command $cmd -ErrorAction SilentlyContinue) {
            $ok = & $cmd @pre -c "import sys; raise SystemExit(0 if sys.version_info[:2] >= (3,10) else 1)" 2>$null
            if ($LASTEXITCODE -eq 0) { return ,@($cmd, $pre) }
        }
    }
    return $null
}

$py = Find-Python
if (-not $py) {
    Write-Error "[hive] Python 3.10+ is required but was not found. Install from https://www.python.org/downloads/ and re-run."
    exit 1
}
$cmd, $pre = $py
Write-Host "[hive] Using $cmd ($(& $cmd @pre --version 2>&1))"

if (Get-Command pipx -ErrorAction SilentlyContinue) {
    Write-Host "[hive] Installing $Pkg with pipx..."
    pipx install $Pkg
} else {
    Write-Host "[hive] pipx not found; installing $Pkg with pip --user..."
    & $cmd @pre -m pip install --user --upgrade $Pkg
    Write-Host "[hive] Note: if 'hive' is not found, add your Python user Scripts dir to PATH."
}

Write-Host "`n[hive] Installed. Try:  hive --help"
