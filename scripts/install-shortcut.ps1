param(
    [Parameter(Mandatory = $true)][string]$DataDir,
    [switch]$EnableNetwork,
    [switch]$EnableBrowser
)
$ErrorActionPreference = 'Stop'
$projectDir = Split-Path -Parent $PSScriptRoot
$pythonConsole = Join-Path $projectDir '.venv\Scripts\python.exe'
$startScript = Join-Path $PSScriptRoot 'start-jarvis.ps1'
$entry = Join-Path $projectDir 'Jarvis.pyw'
$privateDir = (Resolve-Path -LiteralPath $DataDir).Path
if (-not (Test-Path -LiteralPath $pythonConsole -PathType Leaf)) {
    throw 'Install the locked project environment first (uv sync --python 3.12).'
}
if ($EnableBrowser -and -not $EnableNetwork) {
    throw 'EnableBrowser requires EnableNetwork.'
}
& $pythonConsole (Join-Path $PSScriptRoot 'repair-launch-environment.py') --worker-profiles $privateDir
if ($LASTEXITCODE -ne 0) { throw 'Could not verify the physical Python environment.' }
# These arguments are passed directly to pythonw, never evaluated by a shell.
$launchArgs = @('"' + $entry + '"', '--data-dir', '"' + $privateDir + '"')
$profiles = @(
    @('model.toml', '--profile'),
    @('image.toml', '--image-profile'),
    @('speech.toml', '--speech-profile'),
    @('piper.toml', '--piper-profile'),
    @('vision.toml', '--vision-profile')
)
if (-not (Test-Path -LiteralPath (Join-Path $privateDir 'model.toml') -PathType Leaf)) {
    throw 'model.toml is required: the shortcut must not silently use the test provider.'
}
foreach ($profile in $profiles) {
    $profilePath = Join-Path $privateDir $profile[0]
    if (Test-Path -LiteralPath $profilePath -PathType Leaf) {
        $launchArgs += $profile[1]
        $launchArgs += '"' + $profilePath + '"'
    }
}
if ($EnableNetwork) { $launchArgs += '--enable-network' }
if ($EnableBrowser) { $launchArgs += '--enable-browser' }
$launchArgs += 'web'
$encodedArguments = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($launchArgs -join ' '))
$windowsPowerShell = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
$shell = New-Object -ComObject WScript.Shell
$destinations = @(
    (Join-Path ([Environment]::GetFolderPath('Desktop')) 'Jarvis.lnk'),
    (Join-Path ([Environment]::GetFolderPath('Programs')) 'Jarvis.lnk')
)
foreach ($destination in $destinations) {
    $shortcut = $shell.CreateShortcut($destination)
    $shortcut.TargetPath = $windowsPowerShell
    $shortcut.Arguments = '-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File "' + $startScript + '" -ArgumentsBase64 ' + $encodedArguments
    $shortcut.IconLocation = (Join-Path $projectDir 'src\jarvis\interfaces\static\jarvis.ico') + ',0'
    $shortcut.WorkingDirectory = $projectDir
    $shortcut.Description = 'Jarvis — local desktop assistant'
    $shortcut.Save()
    Write-Output "Created $destination"
}
