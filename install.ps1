<#
    LessTokenMoreSearch installer for Windows.

        irm https://raw.githubusercontent.com/Samet1771/lesstokenmoresearch/main/install.ps1 | iex

    Installs, in order, whatever is missing:
        uv          -> runs the tool without touching your system Python
        ltms        -> this project
        Docker      -> inside WSL2, runs SearXNG; no desktop app needed
        LM Studio   -> optional local model server

    Safe to run again: every step checks before it acts.

    Environment overrides (set before running):
        $env:LTMS_SKIP_DOCKER    = "1"   leave containers alone
        $env:LTMS_SKIP_LMSTUDIO  = "1"   do not offer LM Studio
        $env:LTMS_WITH_LMSTUDIO  = "1"   install LM Studio without asking
        $env:LTMS_SOURCE         = "..." install from a path, git url or PyPI name
        $env:LTMS_YES            = "1"   assume yes, never prompt
#>

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

# Until the package is published, installs come from the repository.
$Repo = 'Samet1771/lesstokenmoresearch'
$Assume = $env:LTMS_YES -eq '1'
$script:Warnings = @()

# ---------------------------------------------------------------- output ---

function Say($text, $color = 'Gray') { Write-Host $text -ForegroundColor $color }
function Step($text) { Write-Host ''; Write-Host "  $text" -ForegroundColor Cyan }
function Ok($text) { Write-Host "    ok    $text" -ForegroundColor Green }
function Info($text) { Write-Host "          $text" -ForegroundColor DarkGray }
function Warn($text) {
    Write-Host "    warn  $text" -ForegroundColor Yellow
    $script:Warnings += $text
}

function Banner {
    Write-Host ''
    Say '   _   _____ __  __ ___ ' Cyan
    Say '  | | |_   _|  \/  / __|   LessTokenMoreSearch' Cyan
    Say '  | |__ | | | |\/| \__ \   fewer tokens, more search' Cyan
    Say '  |____||_| |_|  |_|___/' Cyan
    Write-Host ''
}

function Have($name) { [bool](Get-Command $name -ErrorAction SilentlyContinue) }

<#
    Native programs write progress and warnings to stderr. Under
    $ErrorActionPreference = 'Stop', piping that back with 2>&1 turns every such
    line into a terminating error and kills the installer mid-step. So all
    external commands go through here, where the preference is relaxed and the
    exit code is what decides success.
#>
function Invoke-Native {
    param(
        [Parameter(Mandatory)][string] $File,
        [string[]] $Arguments = @(),
        [switch] $Show,
        [int] $Tail = 0
    )
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $output = & $File @Arguments 2>&1 | ForEach-Object { "$_" }
        $code = $LASTEXITCODE
    } catch {
        $output = @($_.Exception.Message)
        $code = 1
    } finally {
        $ErrorActionPreference = $previous
    }
    if ($Show -and $output) {
        $lines = if ($Tail -gt 0) { $output | Select-Object -Last $Tail } else { $output }
        $lines | ForEach-Object { Info $_ }
    }
    return [pscustomobject]@{ ExitCode = $code; Output = $output }
}

function Refresh-Path {
    $machine = [Environment]::GetEnvironmentVariable('Path', 'Machine')
    $user = [Environment]::GetEnvironmentVariable('Path', 'User')
    $extra = Join-Path $env:USERPROFILE '.local\bin'
    $env:Path = (@($machine, $user, $extra) | Where-Object { $_ }) -join ';'
}

function Ask($question, $default = $true) {
    if ($Assume) { return $default }
    $hint = if ($default) { 'Y/n' } else { 'y/N' }
    $reply = Read-Host "          $question [$hint]"
    if ([string]::IsNullOrWhiteSpace($reply)) { return $default }
    return $reply -match '^(y|yes|e|evet)$'
}

function Winget-Install($id, $label) {
    if (-not (Have 'winget')) {
        Warn "winget is not available, cannot install $label automatically"
        return $false
    }
    Info "installing $label (winget: $id)"
    $result = Invoke-Native 'winget' @(
        'install', '--exact', '--id', $id,
        '--accept-package-agreements', '--accept-source-agreements',
        '--disable-interactivity', '--silent'
    ) -Show -Tail 4

    # -1978335189 is winget's "already installed, nothing to do".
    if ($result.ExitCode -ne 0 -and $result.ExitCode -ne -1978335189) {
        Warn "$label install returned exit code $($result.ExitCode)"
        return $false
    }
    Refresh-Path
    return $true
}

function Resolve-Source {
    if ($env:LTMS_SOURCE) { return $env:LTMS_SOURCE }
    # Running install.ps1 from inside a checkout: install that checkout.
    if ($PSScriptRoot -and (Test-Path (Join-Path $PSScriptRoot 'pyproject.toml'))) {
        return $PSScriptRoot
    }
    return "git+https://github.com/$Repo"
}

# ------------------------------------------------------------------ steps ---

function Install-Uv {
    Step 'uv'
    if (Have 'uv') { Ok "already installed ($(uv --version))"; return $true }

    Info 'downloading from astral.sh'
    try {
        Invoke-RestMethod https://astral.sh/uv/install.ps1 | Invoke-Expression
    } catch {
        Warn "uv install failed: $($_.Exception.Message)"
        return $false
    }
    Refresh-Path
    if (Have 'uv') { Ok "installed ($(uv --version))"; return $true }

    Warn 'uv installed but is not on PATH yet — open a new terminal and rerun'
    return $false
}

function Install-Ltms {
    Step 'ltms'
    $source = Resolve-Source
    Info "installing from $source"

    $result = Invoke-Native 'uv' @('tool', 'install', '--force', $source) -Show -Tail 6
    if ($result.ExitCode -ne 0) {
        Warn 'could not install ltms'
        if ($source -like 'git+*') {
            Info 'if the repository is private or the name is wrong, install from a local clone:'
            Info '    git clone https://github.com/Samet1771/lesstokenmoresearch'
            Info '    uv tool install --force .\lesstokenmoresearch'
        }
        return $false
    }

    Invoke-Native 'uv' @('tool', 'update-shell') | Out-Null
    Refresh-Path
    if (Have 'ltms') { Ok 'ltms is on PATH' } else { Warn 'installed, but PATH needs a new terminal' }
    return $true
}

function Get-WslDistro {
    $result = Invoke-Native 'wsl' @('--list', '--quiet')
    if ($result.ExitCode -ne 0) { return $null }
    $names = $result.Output | ForEach-Object { $_.Trim() } | Where-Object { $_ }
    if (-not $names) { return $null }
    if ($names -contains 'Ubuntu') { return 'Ubuntu' }
    return $names[0]
}

function Wsl-Root {
    param(
        [Parameter(Mandatory)][string] $Distro,
        [Parameter(Mandatory)][string] $Script,
        [switch] $Show,
        [int] $Tail = 0
    )
    return Invoke-Native 'wsl' @('-d', $Distro, '-u', 'root', '--', 'sh', '-lc', $Script) -Show:$Show -Tail $Tail
}

function Install-Containers {
    Step 'container engine'
    if ($env:LTMS_SKIP_DOCKER -eq '1') { Info 'skipped'; return }

    # A native engine on PATH wins; nothing to set up.
    if (Have 'docker') {
        if ((Invoke-Native 'docker' @('info', '--format', '{{.ServerVersion}}')).ExitCode -eq 0) {
            Ok 'docker is already running'
            return
        }
    }

    if (-not (Have 'wsl')) {
        Warn 'WSL is not available on this machine'
        Info 'run this in an ADMIN terminal, reboot, then rerun this script:'
        Info '    wsl --install'
        return
    }

    $env:WSL_UTF8 = '1'
    if ((Invoke-Native 'wsl' @('--status')).ExitCode -ne 0) {
        Warn 'WSL2 is not ready'
        Info 'run this in an ADMIN terminal, reboot, then rerun this script:'
        Info '    wsl --install'
        return
    }

    $distro = Get-WslDistro
    if (-not $distro) {
        Info 'no WSL distribution yet — installing Ubuntu (a few minutes)'
        # --no-launch skips the interactive account setup. ltms only ever talks
        # to this distro as root, so no ordinary user is needed.
        $install = Invoke-Native 'wsl' @('--install', '-d', 'Ubuntu', '--no-launch') -Show -Tail 3
        if ($install.ExitCode -ne 0) {
            Warn 'could not install the Ubuntu distribution'
            Info 'try by hand:  wsl --install -d Ubuntu'
            return
        }
        $distro = Get-WslDistro
        if (-not $distro) { Warn 'Ubuntu did not register; reboot and rerun this script'; return }
    }
    Ok "using WSL distribution: $distro"

    if ((Wsl-Root $distro 'command -v docker').ExitCode -ne 0) {
        Info 'installing Docker Engine inside the distribution (~2 minutes)'
        $docker = Wsl-Root $distro 'curl -fsSL https://get.docker.com | sh' -Show -Tail 3
        if ($docker.ExitCode -ne 0 -or (Wsl-Root $distro 'command -v docker').ExitCode -ne 0) {
            Warn 'Docker Engine install failed inside WSL'
            Info "try by hand:  wsl -d $distro -u root -- sh -c 'curl -fsSL https://get.docker.com | sh'"
            return
        }
    }
    Ok 'docker engine present'

    Wsl-Root $distro 'service docker start' | Out-Null
    Start-Sleep -Seconds 2
    if ((Wsl-Root $distro 'docker info >/dev/null 2>&1').ExitCode -ne 0) {
        Warn 'the docker daemon did not start inside WSL'
        Info "check with:  wsl -d $distro -u root -- service docker status"
        return
    }
    Ok 'docker daemon running'

    Info 'pre-pulling the SearXNG image (~250 MB) so the first search is not a long wait'
    $pull = Wsl-Root $distro 'docker pull searxng/searxng:latest' -Show -Tail 1
    if ($pull.ExitCode -eq 0) { Ok 'searxng image ready' }
    else { Warn 'image pull failed; ltms will retry on first run' }
}

function Install-ModelServer {
    Step 'local model server'
    if ($env:LTMS_SKIP_LMSTUDIO -eq '1') { Info 'skipped'; return }

    foreach ($probe in @(
            @{ Name = 'LM Studio'; Url = 'http://127.0.0.1:1234/v1/models' },
            @{ Name = 'Ollama'; Url = 'http://127.0.0.1:11434/api/tags' })) {
        try {
            $null = Invoke-RestMethod -Uri $probe.Url -TimeoutSec 2
            Ok "$($probe.Name) is already serving"
            return
        } catch { }
    }

    if (Have 'lms') { Ok 'LM Studio is installed'; Info 'start its server: Developer tab, or  lms server start'; return }
    if (Have 'ollama') { Ok 'Ollama is installed'; Info 'pull a model:  ollama pull qwen3:14b'; return }

    Info 'ltms needs a local model server. LM Studio is the easiest one.'
    $wanted = $env:LTMS_WITH_LMSTUDIO -eq '1' -or (Ask 'install LM Studio now?' $true)
    if (-not $wanted) { Info 'skipping — install one later, then run: ltms init'; return }

    if (Winget-Install 'ElementLabs.LMStudio' 'LM Studio') {
        Ok 'LM Studio installed'
        Info 'open it, download a model, then turn the local server on (Developer tab)'
    }
}

function Finish {
    Write-Host ''
    Write-Host ('  ' + ('-' * 62)) -ForegroundColor DarkGray

    if ($script:Warnings.Count -gt 0) {
        Write-Host ''
        Say '  some steps need you:' Yellow
        foreach ($warning in $script:Warnings) { Say "    - $warning" Yellow }
    }

    Write-Host ''
    Say '  next' DarkGray
    Say '    ltms init                     configure (detects what you have)' White
    Say '    ltms "your research topic"    run it' White
    Write-Host ''
    Say '  point your coding agent at it by adding one line to CLAUDE.md:' DarkGray
    Say '    For web research run: ltms "<topic>" and read the report file it prints.' White
    Write-Host ''

    if ((Have 'ltms') -and (Ask 'run ltms init now?' $true)) {
        Write-Host ''
        & ltms init
    }
}

# ------------------------------------------------------------------- main ---

Banner

if ($PSVersionTable.PSVersion.Major -lt 5) {
    Say '  This installer needs PowerShell 5 or newer.' Red
    exit 1
}

if (-not (Install-Uv)) { Finish; exit 1 }
if (-not (Install-Ltms)) { Finish; exit 1 }
Install-Containers
Install-ModelServer
Finish
