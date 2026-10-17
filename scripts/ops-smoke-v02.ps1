# v0.2 ops smoke (mock seat, no real AI)
$ErrorActionPreference = "Stop"
$root = Join-Path $env:TEMP "art-v02-smoke"
if (Test-Path $root) { Remove-Item $root -Recurse -Force }
New-Item -ItemType Directory -Path $root | Out-Null

python -m roundtable.cli new-topic smoke-v02 --topic "v0.2 smoke" --participants codex --background "KPI human_actions" --root $root
python -m roundtable.cli dispatch smoke-v02 --participant codex --no-clipboard --async --timeout 5 --root $root

$journalPath = Join-Path $root "minutes/smoke-v02/journal.json"
$journal = Get-Content $journalPath -Raw | ConvertFrom-Json
$inv = @($journal.invocations.PSObject.Properties.Name)[0]
$out = Join-Path $root "minutes/smoke-v02/scratch/$inv.json"
$payload = @{
  invocation_id = $inv
  participant = "codex"
  opinion = "smoke ok"
  claims = @(@{ claim = "mock seat works"; evidence_type = "observed"; evidence = "local smoke" })
} | ConvertTo-Json -Depth 5
Set-Content -Path $out -Value $payload -Encoding utf8

python -m roundtable.cli collect smoke-v02 --invocation $inv --timeout 5 --root $root
if ($LASTEXITCODE -ne 0) { throw "collect failed: $LASTEXITCODE" }
python -m roundtable.cli status smoke-v02 --root $root
python -m roundtable.cli close smoke-v02 --verdict "smoke pass" --root $root
Write-Output "SMOKE_OK"
