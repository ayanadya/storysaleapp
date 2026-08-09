<#
    StorySale server-side setup script.
    Run from an elevated PowerShell on the target server:
        Set-ExecutionPolicy -Scope Process Bypass
        .\scripts\setup_server.ps1

    Assumes you've already installed (manually, with GUI installers):
      - Python 3.10+    (with "Add Python to PATH" checked)
      - Git for Windows
      - NVIDIA driver   (verify with `nvidia-smi`)
      - Tailscale       (signed into your tailnet)

    Idempotent: safe to re-run.
#>

#Requires -RunAsAdministrator

[CmdletBinding()]
param(
    [string]$ProjectPath = 'C:\storysaleapp',

    # Which Python builds the venv. Pinned rather than "whatever `python`
    # resolves to" because PyTorch CUDA wheels lag new CPython releases by
    # months — a 3.14 venv will fail at the torch install step with an
    # unhelpful "no matching distribution" error.
    [string]$PythonVersion = '3.12'
)

$ErrorActionPreference = 'Stop'

function Step($label) {
    Write-Host ''
    Write-Host ('=' * 64) -ForegroundColor Cyan
    Write-Host "STEP: $label" -ForegroundColor Cyan
    Write-Host ('=' * 64) -ForegroundColor Cyan
}
function OK($msg)   { Write-Host "  [OK]   $msg" -ForegroundColor Green }
function Warn($msg) { Write-Host "  [WARN] $msg" -ForegroundColor Yellow }
function Fail($msg) { Write-Host "  [FAIL] $msg" -ForegroundColor Red; throw $msg }

# ----------------------------------------------------------------
# 0. Prerequisite checks
# ----------------------------------------------------------------
Step 'Prerequisite checks'

foreach ($tool in 'git','nvidia-smi','tailscale') {
    $cmd = Get-Command $tool -ErrorAction SilentlyContinue
    if (-not $cmd) { Fail "$tool not found in PATH. Install it before running this script." }
    OK "$tool found at $($cmd.Source)"
}

# Resolve the interpreter that will build the venv. Prefer `py -<version>`
# (the launcher can pick a non-default install); fall back to bare `python`.
$BootstrapPython = $null
if (Get-Command 'py' -ErrorAction SilentlyContinue) {
    & py "-$PythonVersion" --version *> $null
    if ($LASTEXITCODE -eq 0) {
        $BootstrapPython = @('py', "-$PythonVersion")
        OK "Using py -$PythonVersion to build the venv"
    } else {
        Warn "py -$PythonVersion not available (install it with: py install $PythonVersion)"
    }
}
if (-not $BootstrapPython) {
    $cmd = Get-Command 'python' -ErrorAction SilentlyContinue
    if (-not $cmd) { Fail "Neither 'py -$PythonVersion' nor 'python' found. Install Python $PythonVersion and re-run." }
    $BootstrapPython = @('python')
    Warn "Falling back to bare 'python' at $($cmd.Source)"
}

$pyVer = (& $BootstrapPython[0] $BootstrapPython[1..($BootstrapPython.Count-1)] --version 2>&1).ToString() -replace '^Python ',''
$maj,$min = $pyVer.Split('.')[0..1] | ForEach-Object { [int]$_ }
if ($maj -lt 3 -or ($maj -eq 3 -and $min -lt 10)) {
    Fail "Python $pyVer is too old. Install 3.10+ and re-run."
}
if ($maj -eq 3 -and $min -gt 13) {
    Warn "Python $pyVer is newer than the last version with reliable PyTorch CUDA wheels (3.13)."
    Warn "If the PyTorch step fails, run: py install $PythonVersion   then delete .venv and re-run this script."
}
OK "Python $pyVer"

# GPU check
$gpu = (& nvidia-smi -L 2>&1) -join ' '
OK "GPU: $gpu"

# ----------------------------------------------------------------
# 1. Project location
# ----------------------------------------------------------------
Step "Project at $ProjectPath"

if (-not (Test-Path "$ProjectPath\storysale\cli.py")) {
    Fail "$ProjectPath does not look like the storysale repo. Clone it there first:`n  git clone <your repo url> $ProjectPath"
}
Set-Location $ProjectPath
OK "Working in $ProjectPath"

# ----------------------------------------------------------------
# 2. venv + pip
# ----------------------------------------------------------------
Step 'Python venv'

if (-not (Test-Path '.venv\Scripts\python.exe')) {
    & $BootstrapPython[0] $BootstrapPython[1..($BootstrapPython.Count-1)] -m venv .venv
    OK "Created .venv from Python $pyVer"
} else {
    $existing = (& '.venv\Scripts\python.exe' --version 2>&1).ToString() -replace '^Python ',''
    OK ".venv already exists (Python $existing)"
    if ($existing.Split('.')[0..1] -join '.' -ne $pyVer.Split('.')[0..1] -join '.') {
        Warn "Existing .venv is Python $existing but you asked for $pyVer."
        Warn "To rebuild: Remove-Item -Recurse -Force .venv   then re-run this script."
    }
}
$py = "$ProjectPath\.venv\Scripts\python.exe"

& $py -m pip install --upgrade pip --quiet
OK 'pip upgraded'

# ----------------------------------------------------------------
# 3. PyTorch + CUDA (BEFORE the rest of requirements)
# ----------------------------------------------------------------
Step 'PyTorch + CUDA'

$cudaAvail = (& $py -c "import torch; print(torch.cuda.is_available())" 2>$null).Trim()
if ($cudaAvail -eq 'True') {
    $dev = (& $py -c "import torch; print(torch.cuda.get_device_name(0))").Trim()
    OK "PyTorch already installed with working CUDA — device: $dev"
} else {
    Warn 'Installing PyTorch CUDA 12.1 build — ~2.5 GB download, takes a few minutes'
    & $py -m pip uninstall -y torch torchvision 2>$null | Out-Null
    & $py -m pip install torch torchvision --index-url https://download.pytorch.org/whl/cu121
    $cudaAvail = (& $py -c "import torch; print(torch.cuda.is_available())").Trim()
    if ($cudaAvail -ne 'True') {
        Fail 'PyTorch installed but torch.cuda.is_available() == False. Driver mismatch — reinstall NVIDIA driver and try again.'
    }
    $dev = (& $py -c "import torch; print(torch.cuda.get_device_name(0))").Trim()
    OK "CUDA confirmed — device: $dev"
}

# ----------------------------------------------------------------
# 4. Project deps
# ----------------------------------------------------------------
Step 'Project requirements'

& $py -m pip install -r requirements.txt
OK 'requirements.txt installed'

# ----------------------------------------------------------------
# 5. .env and session file
# ----------------------------------------------------------------
Step '.env and instagrapi session'

if (-not (Test-Path '.env')) {
    Fail ".env missing. Copy it from your dev machine to $ProjectPath\.env and re-run."
}
OK '.env present'

$haveSession = Test-Path 'secrets\instagrapi-session.json'
if (-not $haveSession) {
    Warn 'No instagrapi session file. After this script finishes, run:'
    Warn "  $py -m storysale.cli login"
    Warn '(May trigger an IG new-device email — confirm it on your phone.)'
} else {
    OK 'instagrapi session present'
}

# ----------------------------------------------------------------
# 6. Power settings — never sleep while plugged in
# ----------------------------------------------------------------
Step 'Power settings'

& powercfg /change standby-timeout-ac 0
& powercfg /change hibernate-timeout-ac 0
& powercfg /change monitor-timeout-ac 10
OK 'Sleep + hibernate disabled (monitor still turns off after 10 min)'

# ----------------------------------------------------------------
# 7. Defender exclusion (avoids per-file AV scan slowdown)
# ----------------------------------------------------------------
Step 'Defender exclusion'

try {
    Add-MpPreference -ExclusionPath $ProjectPath -ErrorAction Stop
    OK "Excluded $ProjectPath from Defender real-time scans"
} catch {
    Warn "Defender exclusion failed: $($_.Exception.Message) — not fatal"
}

# ----------------------------------------------------------------
# 8. Firewall rule for Streamlit
# ----------------------------------------------------------------
Step 'Firewall: allow inbound 8501'

if (-not (Get-NetFirewallRule -DisplayName 'StorySale UI 8501' -ErrorAction SilentlyContinue)) {
    New-NetFirewallRule -DisplayName 'StorySale UI 8501' `
        -Direction Inbound -Action Allow -Protocol TCP -LocalPort 8501 `
        -Profile Any | Out-Null
    OK 'Rule added'
} else {
    OK 'Rule already exists'
}

# ----------------------------------------------------------------
# 9. Scheduled tasks
# ----------------------------------------------------------------
Step 'Scheduled tasks: scrape every 30 min + Streamlit at startup'

$user = "$env:USERDOMAIN\$env:USERNAME"
$streamlit = "$ProjectPath\.venv\Scripts\streamlit.exe"

# Drop prior versions so re-runs land in a clean state
Get-ScheduledTask -TaskName 'StorySale Scrape','StorySale UI' -ErrorAction SilentlyContinue `
    | Unregister-ScheduledTask -Confirm:$false

# --- Scrape task ---
$scrapeAction = New-ScheduledTaskAction -Execute $py `
    -Argument '-m storysale.cli scrape --batch-size 56' `
    -WorkingDirectory $ProjectPath
$scrapeTrigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(2) `
    -RepetitionInterval (New-TimeSpan -Minutes 30)
$scrapeSettings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable -RestartCount 2 -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 25)
Register-ScheduledTask -TaskName 'StorySale Scrape' `
    -Action $scrapeAction -Trigger $scrapeTrigger -Settings $scrapeSettings `
    -User $user -RunLevel Highest `
    -Description 'Scrape IG every 30 min' | Out-Null
OK 'Task created: StorySale Scrape (every 30 min, max 25 min runtime)'

# --- UI task ---
$uiAction = New-ScheduledTaskAction -Execute $streamlit `
    -Argument 'run storysale\ui\app.py --server.address=0.0.0.0 --server.headless=true --server.port=8501' `
    -WorkingDirectory $ProjectPath
$uiTrigger = New-ScheduledTaskTrigger -AtStartup
$uiSettings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Hours 0)
Register-ScheduledTask -TaskName 'StorySale UI' `
    -Action $uiAction -Trigger $uiTrigger -Settings $uiSettings `
    -User $user -RunLevel Highest `
    -Description 'Streamlit UI on :8501' | Out-Null
OK 'Task created: StorySale UI (at startup)'

# ----------------------------------------------------------------
# 10. Validation
# ----------------------------------------------------------------
Step 'Validation'

if ($haveSession) {
    & $py -m storysale.cli diagnose
    if ($LASTEXITCODE -eq 0) { OK 'diagnose: session is alive' }
    else { Warn 'diagnose returned non-zero — check output above' }
} else {
    Warn 'Skipped diagnose — no session yet'
}

# Start the UI now so we don't wait for a reboot
Start-ScheduledTask -TaskName 'StorySale UI'
OK 'Started StorySale UI task'

# ----------------------------------------------------------------
Step 'All done'

Write-Host ''
Write-Host 'Final manual bits:' -ForegroundColor Green
if (-not $haveSession) {
    Write-Host '  1. Create the IG session (will likely send a new-device email):' -ForegroundColor Green
    Write-Host "     $py -m storysale.cli login" -ForegroundColor Green
}
Write-Host '  2. Enable auto-login so the box recovers cleanly from reboots:' -ForegroundColor Green
Write-Host '     netplwiz   (uncheck "Users must enter a user name and password")' -ForegroundColor Green
Write-Host '  3. Find this PC in your tailnet:' -ForegroundColor Green
Write-Host '     tailscale status' -ForegroundColor Green
Write-Host '  4. From any device on Tailscale, visit:' -ForegroundColor Green
Write-Host '     http://<this-pc-tailscale-name>:8501' -ForegroundColor Green
Write-Host '  5. Watch the live scrape log:' -ForegroundColor Green
Write-Host "     Get-Content $ProjectPath\data\scrape.log -Wait -Tail 30" -ForegroundColor Green
