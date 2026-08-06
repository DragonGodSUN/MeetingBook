# ============================================================
#  MeetingBook Launcher
#  - checks Python & dependencies
#  - applies network env fallbacks (mirror, SSL, xet)
#  - starts the interactive menu or passes args through
#  Usage:
#    powershell -ExecutionPolicy Bypass -File start_meetingbook.ps1
#    powershell -ExecutionPolicy Bypass -File start_meetingbook.ps1 search "budget"
# ============================================================

param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$Args2Pass
)

$ErrorActionPreference = "Continue"  # native commands: rely on $LASTEXITCODE

# --- locate project root (this script lives in <root>/scripts) ---
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $Root

Write-Host "==============================================" -ForegroundColor Cyan
Write-Host "  MeetingBook  Meeting Assistant" -ForegroundColor Cyan
Write-Host "==============================================" -ForegroundColor Cyan

# --- python check ---
$Py = Get-Command python -ErrorAction SilentlyContinue
if (-not $Py) {
    Write-Host "[ERROR] Python not found in PATH. Install Python 3.10+ first." -ForegroundColor Red
    Read-Host "Press Enter to exit"
    exit 1
}
Write-Host "[OK] Python: $($Py.Source)"

# --- network env fallbacks (safe to re-apply; user-level values already set) ---
if (-not $env:HF_ENDPOINT)        { $env:HF_ENDPOINT = "https://hf-mirror.com" }
if (-not $env:HF_HUB_DISABLE_XET) { $env:HF_HUB_DISABLE_XET = "1" }
$Certifi = Join-Path (Split-Path -Parent (Split-Path -Parent $Py.Source)) "Lib\site-packages\certifi\cacert.pem"
if (-not $env:SSL_CERT_FILE -and (Test-Path $Certifi)) { $env:SSL_CERT_FILE = $Certifi }
if (-not $env:REQUESTS_CA_BUNDLE -and (Test-Path $Certifi)) { $env:REQUESTS_CA_BUNDLE = $Certifi }

# --- dependency check ---
Write-Host "[1/2] Checking dependencies (faster-whisper, openai, jieba) ..." -ForegroundColor Yellow
python -W ignore -c "import faster_whisper, openai, jieba" 2>&1 | Out-Null
if ($LASTEXITCODE -ne 0) {
    Write-Host "     Missing packages. Installing ..." -ForegroundColor Yellow
    python -m pip install --disable-pip-version-check faster-whisper openai jieba
    if ($LASTEXITCODE -ne 0) {
        Write-Host "[ERROR] pip install failed. Check your network." -ForegroundColor Red
        Read-Host "Press Enter to exit"
        exit 1
    }
    Write-Host "     Dependencies installed." -ForegroundColor Green
} else {
    Write-Host "     Dependencies OK." -ForegroundColor Green
}

# --- API key hint ---
if (-not $env:DEEPSEEK_API_KEY) {
    $EnvFile = Join-Path $Root ".env"
    $HasKey = $false
    if (Test-Path $EnvFile) {
        $HasKey = [bool](Select-String -Path $EnvFile -Pattern "^\s*DEEPSEEK_API_KEY=.+" -Quiet)
    }
    if (-not $HasKey) {
        Write-Host "[NOTE] DEEPSEEK_API_KEY not found: summarize/ask need it." -ForegroundColor Yellow
        Write-Host "       Inside the menu choose [6] Configure API Key, or run:" -ForegroundColor Yellow
        Write-Host "       python tools\meetingbook.py config --set sk-..." -ForegroundColor Yellow
    }
}

# --- launch ---
Write-Host "[2/2] Starting MeetingBook ..." -ForegroundColor Yellow
Write-Host ""
if ($Args2Pass.Count -gt 0 -and $Args2Pass[0] -eq "webui") {
    # Web 可视化界面模式：透传剩余参数给 webui.py
    $WebArgs = @()
    if ($Args2Pass.Count -gt 1) { $WebArgs = $Args2Pass[1..($Args2Pass.Count - 1)] }
    & python "tools\webui.py" @WebArgs
} elseif ($Args2Pass.Count -gt 0) {
    & python "tools\meetingbook.py" @Args2Pass
} else {
    & python "tools\meetingbook.py"
}
$code = $LASTEXITCODE
Write-Host ""
Write-Host "MeetingBook exited (code $code)." -ForegroundColor Cyan
exit $code
