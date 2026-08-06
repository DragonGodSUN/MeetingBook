# ============================================================
#  Stop MeetingBook Web UI
#  - kills the python process running tools/webui.py
#  - falls back to killing the process listening on the port
#  Usage:
#    powershell -ExecutionPolicy Bypass -File stop_webui.ps1
#    powershell -ExecutionPolicy Bypass -File stop_webui.ps1 -Port 8080
# ============================================================

param(
    [int]$Port = 8765
)

$stopped = @()

# 1) match python processes whose command line contains webui.py
$procs = Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -like "*webui.py*" }
foreach ($p in $procs) {
    Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
    $stopped += $p.ProcessId
    Write-Host ("[OK] Stopped MeetingBook Web UI (PID {0}, command match)" -f $p.ProcessId) -ForegroundColor Green
}

# 2) fall back: kill whatever listens on the web UI port
$conns = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
foreach ($c in $conns) {
    if ($c.OwningProcess -notin $stopped) {
        Stop-Process -Id $c.OwningProcess -Force -ErrorAction SilentlyContinue
        $stopped += $c.OwningProcess
        Write-Host ("[OK] Stopped process on port {0} (PID {1})" -f $Port, $c.OwningProcess) -ForegroundColor Green
    }
}

if ($stopped.Count -eq 0) {
    Write-Host "[INFO] No running MeetingBook Web UI found." -ForegroundColor Yellow
} else {
    Write-Host ("[OK] MeetingBook Web UI stopped ({0} process(es))." -f $stopped.Count) -ForegroundColor Green
}
