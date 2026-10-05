param(
    [int]$Port = 9222,
    [string]$Url = "about:blank"
)

$chromeCandidates = @(
    "${env:ProgramFiles}\Google\Chrome\Application\chrome.exe",
    "${env:ProgramFiles(x86)}\Google\Chrome\Application\chrome.exe"
)
$chrome = $chromeCandidates | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
if (-not $chrome) {
    throw "Google Chrome was not found. Install Chrome, then run this script again."
}

$profile = Join-Path $env:LOCALAPPDATA "JobApplicationAgentChrome"
New-Item -ItemType Directory -Force -Path $profile | Out-Null

$process = Start-Process -FilePath $chrome -ArgumentList @(
    "--remote-debugging-port=$Port",
    "--remote-debugging-address=127.0.0.1",
    "--user-data-dir=$profile",
    "--new-window",
    $Url
) -PassThru

$endpoint = "http://127.0.0.1:$Port/json/version"
$deadline = (Get-Date).AddSeconds(15)
do {
    try {
        $response = Invoke-WebRequest -UseBasicParsing -Uri $endpoint -TimeoutSec 2 -ErrorAction Stop
        if ($response.Content -match "webSocketDebuggerUrl") {
            Write-Host "Chrome is ready for the application agent (process $($process.Id)) at http://127.0.0.1:$Port"
            Write-Host "Sign in directly in that Chrome window, open the employer application, then use the Application Autopilot extension side panel to start safe filling."
            exit 0
        }
    } catch {
        # Chrome can take a few seconds to initialise its separate profile.
    }
    if ($process.HasExited) {
        throw "Chrome exited during startup (exit code $($process.ExitCode))."
    }
    Start-Sleep -Milliseconds 250
} while ((Get-Date) -lt $deadline)

throw "Chrome opened but the local debugging endpoint was not ready after 15 seconds. Close that separate Chrome window and run this script again."
