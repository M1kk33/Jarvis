param(
    [Parameter(Mandatory = $true)][string]$DataDir,
    [switch]$EnableNetwork,
    [switch]$EnableBrowser
)
$ErrorActionPreference = 'Stop'
$projectDir = Split-Path -Parent $PSScriptRoot
$pythonWindow = Join-Path $projectDir '.venv\Scripts\pythonw.exe'
$entry = Join-Path $projectDir 'Jarvis.pyw'
$privateDir = (Resolve-Path -LiteralPath $DataDir).Path
if (-not (Test-Path -LiteralPath $pythonWindow -PathType Leaf)) {
    throw 'Install the locked project environment first (uv sync --python 3.12).'
}
if ($EnableBrowser -and -not $EnableNetwork) {
    throw 'EnableBrowser requires EnableNetwork.'
}
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
$shell = New-Object -ComObject WScript.Shell
$destinations = @(
    (Join-Path ([Environment]::GetFolderPath('Desktop')) 'Jarvis.lnk'),
    (Join-Path ([Environment]::GetFolderPath('Programs')) 'Jarvis.lnk')
)
foreach ($destination in $destinations) {
    $shortcut = $shell.CreateShortcut($destination)
    $shortcut.TargetPath = $pythonWindow
    $shortcut.Arguments = $launchArgs -join ' '
    $shortcut.WorkingDirectory = $projectDir
    $shortcut.Description = 'Open the local Jarvis interface'
    $shortcut.Save()
    Write-Output "Created $destination"
}
