<#
  Smoke test for the Split Inference Control Plane.

  Exercises the endpoints end to end so you can see the backend working without
  fighting curl quoting on Windows. Safe to re-run: /seed replaces the inventory.

      .\smoke.ps1                      # against http://127.0.0.1:8000
      .\smoke.ps1 -Port 8001
      .\smoke.ps1 -BaseUrl http://192.168.1.20:8000 -Token my-token
#>
[CmdletBinding()]
param(
  [string]$BaseUrl = "",
  [int]$Port = 8000,
  [string]$Token = ""
)

$ErrorActionPreference = "Stop"

if (-not $BaseUrl) { $BaseUrl = "http://127.0.0.1:$Port" }

# Default the token from .env so it stays in one place.
if (-not $Token) {
  $envFile = Join-Path $PSScriptRoot ".env"
  if (Test-Path $envFile) {
    $line = Select-String -Path $envFile -Pattern '^API_TOKEN=' | Select-Object -First 1
    if ($line) { $Token = ($line.Line -replace '^API_TOKEN=', '').Trim() }
  }
}
if (-not $Token) { $Token = "dev-token-change-me" }

$headers = @{ Authorization = "Bearer $Token" }
$pass = 0
$fail = 0

function Step($n, $text) { Write-Host "`n[$n] $text" -ForegroundColor Cyan }
function Ok($text)   { $script:pass++; Write-Host "    OK   $text" -ForegroundColor Green }
function Bad($text)  { $script:fail++; Write-Host "    FAIL $text" -ForegroundColor Red }
function Info($text) { Write-Host "         $text" -ForegroundColor DarkGray }

function Api($method, $path, $body) {
  $args = @{ Method = $method; Uri = "$BaseUrl$path"; Headers = $headers; TimeoutSec = 30 }
  if ($null -ne $body) {
    $args.Body = ($body | ConvertTo-Json -Depth 10 -Compress)
    $args.ContentType = "application/json"
  }
  Invoke-RestMethod @args
}

Write-Host "Split Inference Control Plane -- smoke test" -ForegroundColor White
Write-Host "target: $BaseUrl" -ForegroundColor DarkGray

# --------------------------------------------------------------- 1. reachable
Step 1 "Server reachable and healthy"
try {
  $health = Invoke-RestMethod -Uri "$BaseUrl/health" -TimeoutSec 10
  Ok "status=$($health.status)"
  if ($health.broker_connected) {
    Ok "RabbitMQ connected"
  } else {
    Info "RabbitMQ NOT connected -- Simulate mode and the Control tab still work,"
    Info "but /run/start will return 503. Start it with:"
    Info "  docker run -d -p 5672:5672 -p 15672:15672 rabbitmq:3.13-management-alpine"
  }
} catch {
  Bad "cannot reach $BaseUrl/health -- is the server running?"
  Write-Host "`n  Start it with:" -ForegroundColor Yellow
  Write-Host "    .venv\Scripts\python.exe -m uvicorn app.main:app --reload`n" -ForegroundColor Yellow
  exit 1
}

# ------------------------------------------------------------------- 2. auth
Step 2 "Auth is enforced"
try {
  Invoke-RestMethod -Uri "$BaseUrl/devices" -TimeoutSec 10 | Out-Null
  Bad "/devices answered without a token"
} catch {
  if ($_.Exception.Response.StatusCode.value__ -eq 401) { Ok "unauthenticated request rejected (401)" }
  else { Bad "expected 401, got $($_.Exception.Response.StatusCode.value__)" }
}
try {
  Api GET "/devices" $null | Out-Null
  Ok "token accepted"
} catch {
  Bad "token rejected -- check API_TOKEN in .env matches -Token"
  exit 1
}

# ------------------------------------------------------------------- 3. seed
Step 3 "Load the UI's default scenario (POST /seed)"
$scenario = @{
  model  = "yolov11n"
  config = @{ clustering = $true; numClusters = 2; autoBalance = "power"
              manualEnabled = $false; manualSplit = 5; modelName = "yolov11n" }
  stages = @(
    @{ id = "s1"; kind = "Edge"; name = "Edge"; devices = @(
        @{ id = "dA"; name = "Jetson-A"; gflops = 472; bw = 12; lat = 6; cluster = 1 },
        @{ id = "dB"; name = "Jetson-B"; gflops = 472; bw = 12; lat = 6; cluster = 1 },
        @{ id = "dC"; name = "Jetson-C"; gflops = 384; bw = 10; lat = 8; cluster = 2 }) },
    @{ id = "s2"; kind = "Cloud"; name = "Cloud"; devices = @(
        @{ id = "dG1"; name = "GPU-1"; gflops = 9800; bw = 125; lat = 2; cluster = 1 },
        @{ id = "dG2"; name = "GPU-2"; gflops = 9800; bw = 125; lat = 2; cluster = 2 }) }
  )
  clusters = @()
}
$seeded = Api POST "/seed" $scenario
if ($seeded.devices -eq 5) { Ok "5 devices imported across 2 clusters" }
else { Bad "expected 5 devices, got $($seeded.devices)" }

# ---------------------------------------------------------------- 4. metrics
Step 4 "Metrics match the UI simulator (GET /metrics/latest)"
$m = Api GET "/metrics/latest" $null
foreach ($c in $m.clusters) {
  Info ("cluster {0}: cut@{1}/{2}  e2e {3}ms  {4} fps  msg {5}MB  [{6}]" -f `
        $c.cluster, $c.cut, $c.layer_count, $c.e2e_ms, $c.fps, $c.msg_mb, $c.source)
}
# Golden values cross-checked against the UI's JS in an independent JS engine.
$c1 = $m.clusters | Where-Object { $_.cluster -eq 1 }
if ($c1.cut -eq 2 -and [math]::Abs($c1.fps - 33.621) -lt 0.01) {
  Ok "cluster 1 reproduces the UI exactly (cut@2, 33.621 fps)"
} else {
  Bad "cluster 1 drifted from the UI: cut=$($c1.cut) fps=$($c1.fps)"
}
if ([math]::Abs($m.aggregate_fps - 49.703) -lt 0.01) { Ok "aggregate 49.703 fps" }
else { Bad "aggregate drifted: $($m.aggregate_fps)" }

# ------------------------------------------------------- 5. cut-selection mode
Step 5 "Switching to latency mode changes the cut"
Api PATCH "/config" @{ autoBalance = "latency" } | Out-Null
$cuts = (Api GET "/metrics/latest" $null).clusters | ForEach-Object { $_.cut }
if (($cuts -join ",") -eq "8,8") { Ok "latency mode picks cut 8 for both clusters" }
else { Bad "expected cuts 8,8 -- got $($cuts -join ',')" }
Api PATCH "/config" @{ autoBalance = "power" } | Out-Null
Info "restored power balancing"

# --------------------------------------------------------- 6. command guards
Step 6 "Command allow-list and confirmation gate"

# 6a: an allow-listed command is accepted (SSH then fails -- no real host).
try {
  $r = Api POST "/control/exec" @{ device_ids = @("dA"); command = "uptime" }
  Ok "'uptime' accepted by the guard"
  Info "SSH itself failed as expected (dA has no host configured)"
} catch { Bad "'uptime' was rejected: $($_.Exception.Message)" }

# 6b: shell injection blocked.
foreach ($bad in @("rm -rf /", "nvidia-smi; rm -rf /", "cat /etc/shadow")) {
  try {
    Api POST "/control/exec" @{ device_ids = @("dA"); command = $bad } | Out-Null
    Bad "'$bad' was NOT blocked"
  } catch {
    if ($_.Exception.Response.StatusCode.value__ -eq 400) { Ok "blocked: '$bad'" }
    else { Bad "'$bad' gave $($_.Exception.Response.StatusCode.value__), expected 400" }
  }
}

# 6c: destructive needs confirm=true.
try {
  Api POST "/control/exec" @{ device_ids = @("dA"); command = "sudo reboot" } | Out-Null
  Bad "'sudo reboot' ran without confirmation"
} catch {
  if ($_.Exception.Response.StatusCode.value__ -eq 409) { Ok "'sudo reboot' needs confirm=true (409)" }
  else { Bad "expected 409, got $($_.Exception.Response.StatusCode.value__)" }
}
try {
  Api POST "/control/exec" @{ device_ids = @("dA"); command = "sudo reboot"; confirm = $true } | Out-Null
  Ok "'sudo reboot' allowed with confirm=true"
} catch { Bad "confirmed reboot was still refused" }

# 6d: the quoted-semicolon preset.
$pyPreset = 'python -c "import torch;print(torch.__version__, torch.cuda.is_available())"'
try {
  Api POST "/control/exec" @{ device_ids = @("dA"); command = $pyPreset } | Out-Null
  Ok "python version preset accepted (quoted ';' handled)"
} catch { Bad "python preset rejected: $($_.Exception.Message)" }

# ------------------------------------------------------- 7. broker/server card
Step 7 "Broker config + connection test"
Api POST "/server/config" @{
  ip = "127.0.0.1"; port = 5672; api_port = ([uri]$BaseUrl).Port
  user = "guest"; password = "guest"
} | Out-Null
$cfg = Api GET "/server/config" $null
if ($cfg.has_credentials -and -not ($cfg.PSObject.Properties.Name -contains "password")) {
  Ok "password stored but never returned (has_credentials=$($cfg.has_credentials))"
} else { Bad "password handling looks wrong" }

$t = Api POST "/server/test" $null
if ($t.ok) {
  Ok "broker reachable: RabbitMQ $($t.rabbitmq_version) / API $($t.api)"
} else {
  Info "test reported not-ok -- broker: '$($t.broker_error)' api: '$($t.api)'"
  Info "(expected if RabbitMQ isn't running)"
}

# ------------------------------------------------------------ 8. BROKER_IP
Step 8 "`$BROKER_IP substitution"
try {
  $r = Api POST "/control/exec" @{ device_ids = @("dA"); command = 'ping -c 3 $BROKER_IP' }
  if ($r.command -eq "ping -c 3 127.0.0.1") { Ok "substituted -> '$($r.command)'" }
  else { Bad "unexpected substitution: '$($r.command)'" }
} catch { Bad "BROKER_IP command failed: $($_.Exception.Message)" }

# ---------------------------------------------------------------- 9. audit
Step 9 "Audit trail for destructive commands"
$audit = Api GET "/control/audit" $null
$n = @($audit.entries).Count
if ($n -ge 2) {
  Ok "$n audit entries recorded"
  foreach ($e in $audit.entries | Select-Object -First 3) {
    Info ("{0}: '{1}' confirmed={2} -> {3}" -f $e.action, $e.command, $e.confirmed, $e.outcome)
  }
} else { Bad "expected at least 2 audit entries, found $n" }

# --------------------------------------------------------------- 10. summary
Write-Host "`n$('-' * 60)"
if ($fail -eq 0) {
  Write-Host "All $pass checks passed." -ForegroundColor Green
} else {
  Write-Host "$pass passed, $fail FAILED." -ForegroundColor Red
}
Write-Host @"

Next:
  Swagger UI    $BaseUrl/docs   (Authorize with: $Token)
  Metrics       $BaseUrl/metrics/latest
  Wire the UI   see ui\WIRING.md
"@ -ForegroundColor DarkGray

if ($fail -gt 0) { exit 1 }
