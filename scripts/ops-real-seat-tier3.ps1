# Real-seat ops (Tier3). Automates everything except: CEO pastes packet into Codex app seat,
# and the seat writes scratch/<inv>.json.
#
# Usage:
#   powershell -NoProfile -File scripts/ops-real-seat-tier3.ps1
#   powershell -NoProfile -File scripts/ops-real-seat-tier3.ps1 -WaitSeconds 0   # prepare only
#   powershell -NoProfile -File scripts/ops-real-seat-tier3.ps1 -MockSeat       # CI/dev mock (NOT real seat)

param(
  [string]$Root = "",
  [string]$Slug = "",
  [int]$WaitSeconds = 120,
  [switch]$MockSeat
)

$ErrorActionPreference = "Stop"
if (-not $Root) { $Root = Join-Path $env:TEMP "art-real-seat" }
if (-not $Slug) { $Slug = "real-seat-" + (Get-Date -Format "yyyyMMdd-HHmmss") }

if (Test-Path $Root) { Remove-Item $Root -Recurse -Force }
New-Item -ItemType Directory -Path $Root | Out-Null

$topic = "real-seat path check"
$bg = "Tier1 start/resume/turn is no-go on standalone app-server (2026-08-07). This run exercises Tier3 real-seat ops: clipboard + seat JSON + collect."

Write-Host "== new-topic $Slug =="
python -m roundtable.cli new-topic $Slug --topic $topic --participants codex --background $bg --root $Root

Write-Host "== dispatch --tier 1 --async (expect Tier3 fallback) =="
python -m roundtable.cli dispatch $Slug --participant codex --tier 1 --async --timeout 10 --root $Root
if ($LASTEXITCODE -ne 0) { throw "dispatch failed: $LASTEXITCODE" }

$journalPath = Join-Path $Root "minutes/$Slug/journal.json"
$journal = Get-Content $journalPath -Raw | ConvertFrom-Json
$inv = @($journal.invocations.PSObject.Properties.Name)[0]
$out = Join-Path $Root "minutes/$Slug/scratch/$inv.json"
$seatsPath = Join-Path $Root "minutes/$Slug/seats.json"

Write-Host "invocation: $inv"
Write-Host "expect seat output: $out"
if (Test-Path $seatsPath) {
  Write-Host "seats.json:"
  Get-Content $seatsPath -Raw
}

if ($MockSeat) {
  Write-Host "== MOCK seat write (not real seat) =="
  $payload = @{
    invocation_id = $inv
    participant = "codex"
    opinion = "mock seat for CI — not a real Codex app reply"
    claims = @(@{
      claim = "mechanical path works"
      evidence_type = "observed"
      evidence = "ops-real-seat-tier3.ps1 -MockSeat"
    })
  } | ConvertTo-Json -Depth 5
  [System.IO.File]::WriteAllText($out, $payload, [System.Text.UTF8Encoding]::new($false))
}
elseif ($WaitSeconds -gt 0) {
  Write-Host "== waiting up to ${WaitSeconds}s for REAL seat JSON =="
  Write-Host "Action required: paste clipboard packet into Codex app seat chat."
  Write-Host "Seat must write JSON to: $out"
  $deadline = (Get-Date).AddSeconds($WaitSeconds)
  while ((Get-Date) -lt $deadline) {
    if (Test-Path $out) { break }
    Start-Sleep -Seconds 2
  }
  if (-not (Test-Path $out)) {
    Write-Host "TIMEOUT: no seat JSON. Leaving topic open for manual collect later."
    Write-Host "  python -m roundtable.cli collect $Slug --invocation $inv --root $Root"
    exit 2
  }
}
else {
  Write-Host "Prepared only (-WaitSeconds 0). Paste then:"
  Write-Host "  python -m roundtable.cli collect $Slug --invocation $inv --root $Root"
  exit 0
}

Write-Host "== collect =="
python -m roundtable.cli collect $Slug --invocation $inv --timeout 30 --root $Root
if ($LASTEXITCODE -ne 0) { throw "collect failed: $LASTEXITCODE" }

Write-Host "== status / close =="
python -m roundtable.cli status $Slug --root $Root
python -m roundtable.cli close $Slug --verdict "real-seat path closed" --root $Root
Write-Host "REAL_SEAT_PATH_OK root=$Root slug=$Slug inv=$inv mock=$MockSeat"
