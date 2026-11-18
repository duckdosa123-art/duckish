# uci_smoke.ps1 - UCI end-to-end harness for target\release\duckish.exe (MultiPV).
# Windows PowerShell 5.1 compatible. Exit 1 if any assertion fails.
$ErrorActionPreference = 'Stop'

$root = Split-Path -Parent $PSScriptRoot
$exe  = Join-Path $root 'target\release\duckish.exe'
if (-not (Test-Path $exe)) { Write-Output "FAIL: binary not found: $exe"; exit 1 }

function Run-Test {
    param(
        [string]$TestName,
        [int]$Threads,
        [int]$MultiPV,
        [string]$GoCommand,
        [int]$WatchdogSeconds = 30
    )

    Write-Host ("=== {0}: Threads={1} MultiPV={2} {3} ===" -f $TestName, $Threads, $MultiPV, $GoCommand)

    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName               = $exe
    $psi.WorkingDirectory       = $root
    $psi.UseShellExecute        = $false
    $psi.RedirectStandardInput  = $true
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError  = $true
    $psi.CreateNoWindow         = $true

    $p = New-Object System.Diagnostics.Process
    $p.StartInfo = $psi
    [void]$p.Start()

    # Watchdog: kill the engine if the whole handshake wedges.
    $watch = $null
    try {
        $watch = Start-Job -ScriptBlock {
            param($id, $sec)
            Start-Sleep -Seconds $sec
            Stop-Process -Id $id -Force -ErrorAction SilentlyContinue
        } -ArgumentList $p.Id, $WatchdogSeconds
    } catch { }

    $allOut = New-Object System.Collections.ArrayList

    function Send([string]$line) { $p.StandardInput.WriteLine($line); $p.StandardInput.Flush() }

    function Read-Until([string]$pattern) {
        while ($null -ne ($l = $p.StandardOutput.ReadLine())) {
            [void]$allOut.Add($l)
            if ($l -match $pattern) { return $true }
        }
        return $false
    }

    function Read-Go([int]$MaxSeconds) {
        $sw = [System.Diagnostics.Stopwatch]::StartNew()
        while ($null -ne ($l = $p.StandardOutput.ReadLine())) {
            [void]$allOut.Add($l)
            if ($l -match '^bestmove') { break }
            if ($sw.Elapsed.TotalSeconds -ge $MaxSeconds) { break }
        }
        $sw.Stop()
    }

    function Get-FirstPvMove([string]$Line) {
        if ($null -ne $Line -and $Line -match ' pv ([a-h][1-8][a-h][1-8][qrbn]?)(\s|$)') { return $Matches[1] }
        return $null
    }

    function Get-Bestmove([string]$Line) {
        if ($null -ne $Line -and $Line -match '^bestmove\s+(\S+)') { return $Matches[1] }
        return $null
    }

    # --- handshake ---
    Send 'uci'
    $sawUciok = Read-Until '^uciok'
    Send 'isready'
    $sawReadyok = Read-Until '^readyok'

    # --- configure and run ---
    $runStart = $allOut.Count
    Send "setoption name Threads value $Threads"
    Send "setoption name MultiPV value $MultiPV"
    Send 'position startpos moves e2e4 e7e5'
    Send $GoCommand
    Read-Go $WatchdogSeconds
    $runOut = @($allOut[$runStart..($allOut.Count - 1)])

    Send 'quit'
    if (-not $p.WaitForExit(5000)) { try { $p.Kill() } catch { } }

    # Drain anything still buffered.
    while ($null -ne ($l = $p.StandardOutput.ReadLine())) { [void]$allOut.Add($l) }
    $errText = $p.StandardError.ReadToEnd()
    if ($watch) { Remove-Job -Job $watch -Force -ErrorAction SilentlyContinue }

    # --- analysis ---
    # Find bestmove line
    $bestLine = $null
    foreach ($l in $runOut) { if ($l -match '^bestmove') { $bestLine = $l; break } }
    $best = Get-Bestmove $bestLine

    # Find last multipv 1 line (for MultiPV > 1) or last PV line (for MultiPV = 1)
    $lastMpv1Line = $null
    if ($MultiPV -gt 1) {
        foreach ($l in $runOut) { if ($l -match '^info .*\bmultipv 1\b.* pv ') { $lastMpv1Line = $l } }
    } else {
        foreach ($l in $runOut) { if ($l -match '^info depth \d+ .* pv ') { $lastMpv1Line = $l } }
    }
    $pv1move = Get-FirstPvMove $lastMpv1Line

    # Check for panic/error
    $badLines = @($runOut | Where-Object { $_ -match 'panic' -or $_ -match 'error' })

    $handshakeOk = $sawUciok -and $sawReadyok
    $hasBestmove = $null -ne $best
    $bestMatchesPv1 = ($null -ne $best) -and ($null -ne $pv1move) -and ($best -eq $pv1move)
    $noErrors = ($badLines.Count -eq 0)

    $pass = $handshakeOk -and $hasBestmove -and $bestMatchesPv1 -and $noErrors

    Write-Host "  Handshake: $(if ($handshakeOk) { 'PASS' } else { 'FAIL' })"
    Write-Host "  Has bestmove: $(if ($hasBestmove) { 'PASS' } else { 'FAIL' })"
    Write-Host "  Bestmove matches PV1: $(if ($bestMatchesPv1) { 'PASS' } else { 'FAIL' }) (bestmove=$best pv1=$pv1move)"
    Write-Host "  No panic/error: $(if ($noErrors) { 'PASS' } else { 'FAIL' }) ($($badLines.Count) bad lines)"
    Write-Host "  Result: $(if ($pass) { 'PASS' } else { 'FAIL' })"
    Write-Host ""

    return @{ Pass = $pass; Output = $runOut; Errors = $badLines; Best = $best; Pv1 = $pv1move }
}

# --- Run all test cases ---
$results = @()

# Test 1: Threads=1 + MultiPV=3 -> go movetime 1000 (existing behavior)
$results += Run-Test "Test1" -Threads 1 -MultiPV 3 -GoCommand "go movetime 1000" -WatchdogSeconds 30

# Test 2: Threads=4 + MultiPV=3 -> go movetime 400 (fixed deadlock combo)
$results += Run-Test "Test2" -Threads 4 -MultiPV 3 -GoCommand "go movetime 400" -WatchdogSeconds 30

# Test 3: Threads=4 + MultiPV=1 -> go movetime 400
$results += Run-Test "Test3" -Threads 4 -MultiPV 1 -GoCommand "go movetime 400" -WatchdogSeconds 30

# --- Summary ---
$allPass = $true
foreach ($r in $results) {
    if (-not $r.Pass) { $allPass = $false }
}

Write-Host "=== SUMMARY ==="
for ($i = 0; $i -lt $results.Count; $i++) {
    $r = $results[$i]
    Write-Host ("Test {0}: {1}" -f ($i+1), $(if ($r.Pass) { 'PASS' } else { 'FAIL' }))
}

if (-not $allPass) { exit 1 }
exit 0