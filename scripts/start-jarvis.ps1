param(
    [Parameter(Mandatory = $true)][string]$ArgumentsBase64
)
$ErrorActionPreference = 'Stop'
try {
    $projectDir = Split-Path -Parent $PSScriptRoot
    $venvDir = Join-Path $projectDir '.venv'
    $config = Join-Path $venvDir 'pyvenv.cfg'
    $homeLine = Get-Content -LiteralPath $config -Encoding UTF8 |
        Where-Object { $_ -match '^home\s*=' } | Select-Object -First 1
    if (-not $homeLine) { throw 'The project Python environment has no home path.' }
    $pythonHome = ($homeLine -split '=', 2)[1].Trim()
    $pythonWindow = Join-Path $pythonHome 'pythonw.exe'
    if (-not (Test-Path -LiteralPath $pythonWindow -PathType Leaf)) {
        throw "The windowed Python interpreter is missing: $pythonWindow"
    }
    $arguments = [Text.Encoding]::UTF8.GetString(
        [Convert]::FromBase64String($ArgumentsBase64)
    )
    $start = New-Object System.Diagnostics.ProcessStartInfo
    $start.FileName = $pythonWindow
    $start.Arguments = $arguments
    $start.WorkingDirectory = $projectDir
    $start.UseShellExecute = $false
    $start.CreateNoWindow = $true
    # Match CPython's venv redirector without invoking that extra executable.
    # Python still discovers the venv's config, dependencies and sys.executable.
    $start.EnvironmentVariables['__PYVENV_LAUNCHER__'] = Join-Path $venvDir 'Scripts\pythonw.exe'
    $process = [System.Diagnostics.Process]::Start($start)
    $process.Dispose()
} catch {
    Add-Type -AssemblyName System.Windows.Forms
    [void][System.Windows.Forms.MessageBox]::Show(
        "Jarvis could not start. Check the project Python environment.`n$($_.Exception.Message)",
        'Jarvis', 'OK', 'Error'
    )
    exit 1
}
