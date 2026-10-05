param(
    [int]$Port = 8765,
    [switch]$Restart
)

$projectRoot = $PSScriptRoot
$python = Join-Path $projectRoot ".venv\Scripts\python.exe"
$service = Join-Path $projectRoot "agent_service.py"
if (-not (Test-Path -LiteralPath $python)) {
    throw "The project virtual environment was not found. Run this from the project folder after installing dependencies."
}

try {
    $health = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/health" -TimeoutSec 2 -ErrorAction Stop
    if ($health.status -eq "ok" -and $health.protocol -eq 8 -and -not $Restart) {
        Write-Host "A compatible local Application Autopilot service is already running on port $Port."
        exit 0
    }
    # Upgrade only this project's own companion. Never stop an unrelated port owner.
    $listener = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction Stop | Select-Object -First 1
    $oldCompanion = Get-CimInstance Win32_Process -Filter "ProcessId = $($listener.OwningProcess)"
    $runtimePython = (& $python -c "import sys; print(sys._base_executable)").Trim()
    if ($oldCompanion.ExecutablePath -in @($python, $runtimePython) -and $oldCompanion.CommandLine.Contains('"' + $service + '"')) {
        Stop-Process -Id $oldCompanion.ProcessId -ErrorAction Stop
        Start-Sleep -Milliseconds 500
    } else {
        throw "An unrelated service is using port $Port. Its process was left running."
    }
} catch [System.Net.WebException] {
    # No listener yet: start the independent loopback companion below.
}

# Start-Process flattens an argument array. Quote the script path explicitly
# because this project path commonly contains spaces.
$arguments = '"' + $service + '" --port ' + $Port
$process = Start-Process -FilePath $python -ArgumentList $arguments -WorkingDirectory $projectRoot -WindowStyle Hidden -PassThru
$deadline = (Get-Date).AddSeconds(15)
do {
    try {
        $health = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/health" -TimeoutSec 2 -ErrorAction Stop
        if ($health.status -eq "ok" -and $health.protocol -eq 8) {
            Write-Host "Application Autopilot companion is ready (process $($process.Id)) on port $Port."
            exit 0
        }
    } catch {
        # The local server takes a moment to initialise.
    }
    if ($process.HasExited) {
        throw "Application Autopilot companion exited during startup (exit code $($process.ExitCode))."
    }
    Start-Sleep -Milliseconds 250
} while ((Get-Date) -lt $deadline)

throw "The local companion did not become healthy after 15 seconds."
