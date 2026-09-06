$ErrorActionPreference = "Stop"

$Installer = Join-Path $PSScriptRoot "install.py"

$Py = Get-Command py -ErrorAction SilentlyContinue
if ($Py) {
    & $Py.Source -3 $Installer
    exit $LASTEXITCODE
}

$Python = Get-Command python -ErrorAction SilentlyContinue
if ($Python) {
    & $Python.Source $Installer
    exit $LASTEXITCODE
}

Write-Error "Python 3.10 or newer is required. Install Python, then run this installer again."
exit 1
