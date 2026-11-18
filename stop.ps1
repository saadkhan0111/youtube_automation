# Stops the Shorts Publisher if it is running in the background (listening on port 8000 by default).
# An upload in progress is paused safely: its resumable session is saved and resumes on next start.
param([int]$Port = 8000)
$conns = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
if (-not $conns) { Write-Host "Nothing is listening on port $Port."; exit 0 }
foreach ($procId in ($conns | Select-Object -ExpandProperty OwningProcess -Unique)) {
    $p = Get-Process -Id $procId -ErrorAction SilentlyContinue
    if ($p -and $p.ProcessName -like "python*") {
        Stop-Process -Id $procId
        Write-Host "Stopped $($p.ProcessName) (PID $procId)."
    } else {
        Write-Host "Port $Port is used by $($p.ProcessName) (PID $procId), not the publisher; leaving it alone."
    }
}
