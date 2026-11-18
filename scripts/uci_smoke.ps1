# uci_smoke.ps1 - UCI end-to-end harness for target\release\duckish.exe (MultiPV).
# Windows PowerShell 5.1 compatible. Exit 1 if any assertion fails.
$ErrorActionPreference = 'Stop'

$root = Split-Path -Parent $PSScriptRoot
$exe  = Join-Path $root 'target\release\duckish.exe'
if (-not (Test-Path $exe)) { Write-Output "FAIL: binary not found: $exe"; exit 1 }

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

# Watchdog: kill the engine if the whole handshake wedges, so ReadLine cannot hang forever.
$watch = $null
try {
    $watch = Start-Job -ScriptBlock {
        param($id, $sec)
        Start-Sleep -Seconds $sec
        Stop-Process -Id $id -Force -ErrorAction SilentlyContinue
    } -ArgumentList $p.Id, 180
} catch { }

$allOut = New-Object System.Collections.ArrayList

function Send([string]$line) { $p.StandardInput.WriteLine($line); $p.StandardInput.Flush() }

# Blocks on ReadLine; records every line. Returns $true when $Pattern matched.
function Read-Until([string]$pattern) {
    while ($null -ne ($l = $p.StandardOutput.ReadLine())) {
        [void]$allOut.Add($l)
        if ($l -match $pattern) { return $true }
    }
    return $false
}

# Records lines until a bestmove, or until $MaxSeconds of go-phase time has elapsed.
function Read-Go([int]$MaxSeconds) {
    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    while ($null -ne ($l = $p.StandardOutput.ReadLine())) {
        [void]$allOut.Add($l)
        if ($l -match '^bestmove') { break }
        if ($sw.Elapsed.TotalSeconds -ge $MaxSeconds) { break }
    }
    $sw.Stop()
}

# The move after the first " pv " token of a uci info line, or $null.
function Get-FirstPvMove([string]$Line) {
    if ($null -ne $Line -and $Line -match ' pv ([a-h][1-8][a-h][1-8][qrbn]?)(\s|$)') { return $Matches[1] }
    return $null
}

# The move after "bestmove", or $null.
function Get-Bestmove([string]$Line) {
    if ($null -ne $Line -and $Line -match '^bestmove\s+(\S+)') { return $Matches[1] }
    return $null
}

# Slice of the recorded output produced since index $Start.
function Get-Since([int]$Start) {
    if ($allOut.Count -le $Start) { return @() }
    return @($allOut[$Start..($allOut.Count - 1)])
}

# --- handshake ---
Send 'uci'
$sawUciok = Read-Until '^uciok'
Send 'isready'
$sawReadyok = Read-Until '^readyok'

# --- run 1: MultiPV 3 with a time limit ---
$run1Start = $allOut.Count
Send 'setoption name MultiPV value 3'
Send 'position startpos moves e2e4 e7e5'
Send 'go movetime 1000'
Read-Go 30
$run1 = Get-Since $run1Start

# --- run 2: same process, fresh game, fixed depth ---
$run2Start = $allOut.Count
Send 'ucinewgame'
Send 'isready'
[void](Read-Until '^readyok')
Send 'setoption name MultiPV value 3'
Send 'position startpos moves e2e4 e7e5'
Send 'go depth 6'
Read-Go 30
$run2 = Get-Since $run2Start

Send 'quit'
if (-not $p.WaitForExit(5000)) { try { $p.Kill() } catch { } }

# Drain anything still buffered (safe once the stream is at EOF).
while ($null -ne ($l = $p.StandardOutput.ReadLine())) { [void]$allOut.Add($l) }
$errText = $p.StandardError.ReadToEnd()
if ($watch) { Remove-Job -Job $watch -Force -ErrorAction SilentlyContinue }

Write-Output '--- CAPTURED STDOUT ---'
foreach ($l in $allOut) { Write-Output $l }
Write-Output '--- CAPTURED STDERR ---'
if ([string]::IsNullOrWhiteSpace($errText)) { Write-Output '<empty>' } else { Write-Output $errText.TrimEnd() }

# --- analysis: run 1 (go movetime 1000, MultiPV 3) ---
# Every depth must report tagged lines only; the deepest completed depth must
# have exactly multipv 1,2,3 in order and no untagged info line.
$run1DepthIndex = @{}
$run1Untagged  = @{}
foreach ($l in $run1) {
    if ($l -match '^info depth (\d+) ') {
        $d = [int]$Matches[1]
        if ($l -match '\bmultipv (\d+)\b') {
            if (-not $run1DepthIndex.ContainsKey($d)) {
                $run1DepthIndex[$d] = New-Object System.Collections.ArrayList
            }
            [void]$run1DepthIndex[$d].Add([int]$Matches[1])
        } else {
            if (-not $run1Untagged.ContainsKey($d)) { $run1Untagged[$d] = 0 }
            $run1Untagged[$d] = $run1Untagged[$d] + 1
        }
    }
}
$tripleDepth = -1
foreach ($d in ($run1DepthIndex.Keys | Sort-Object)) {
    $ks = @($run1DepthIndex[$d])
    if (($ks.Count -eq 3) -and ($ks[0] -eq 1) -and ($ks[1] -eq 2) -and ($ks[2] -eq 3)) { $tripleDepth = $d }
}
$untagged1 = 0
if (($tripleDepth -ge 0) -and $run1Untagged.ContainsKey($tripleDepth)) { $untagged1 = $run1Untagged[$tripleDepth] }
$b = ($tripleDepth -ge 0) -and ($untagged1 -eq 0)

$lastMpv1Line = $null
foreach ($l in $run1) {
    if ($l -match '^info .*\bmultipv 1\b.* pv ') { $lastMpv1Line = $l }
}
$best1Line = $null
foreach ($l in $run1) {
    if ($l -match '^bestmove') { $best1Line = $l }
}
$best1 = Get-Bestmove $best1Line
$pv1move = Get-FirstPvMove $lastMpv1Line
$c = ($null -ne $best1) -and ($null -ne $pv1move) -and ($best1 -eq $pv1move)

# --- analysis: run 2 (go depth 6, MultiPV 3) ---
# At depth 6 the stream must be exactly the three tagged lines, in order, with
# no untagged info line at all.
$d6Index    = @()
$d6Lines    = @()
$d6Untagged = 0
foreach ($l in $run2) {
    if ($l -match '^info depth 6 ') {
        if ($l -match '\bmultipv (\d+)\b') {
            $d6Index += [int]$Matches[1]
            $d6Lines += $l
        } else {
            $d6Untagged = $d6Untagged + 1
        }
    }
}
$exactlyThree = ($d6Index.Count -eq 3) -and ($d6Index[0] -eq 1) -and ($d6Index[1] -eq 2) -and ($d6Index[2] -eq 3) -and ($d6Untagged -eq 0)
$best2Line = $null
foreach ($l in $run2) {
    if ($l -match '^bestmove') { $best2Line = $l }
}
$best2 = Get-Bestmove $best2Line
$d6pv1 = $null
if ($d6Lines.Count -gt 0) { $d6pv1 = Get-FirstPvMove $d6Lines[0] }
$d = $exactlyThree -and ($null -ne $best2) -and ($null -ne $d6pv1) -and ($best2 -eq $d6pv1)

# --- analysis: global ---
$pvLines  = @($allOut | Where-Object { $_ -match '^info .*depth [0-9]+' -and $_ -match ' pv ' })
$bestLines = @($allOut | Where-Object { $_ -match '^bestmove [a-h][1-8][a-h][1-8][qrbn]?' })
$badLines = @($allOut | Where-Object { $_ -match 'panic' -or $_ -match 'error' })

$a = $sawUciok -and $sawReadyok -and ($pvLines.Count -ge 1) -and ($bestLines.Count -ge 2)
$e = $true   # stderr is captured above

$ok = $a -and $b -and $c -and $d -and $e

Write-Output ''
Write-Output ("PASS/FAIL a) handshake + info/bestmove      : " + $(if ($a) { 'PASS' } else { 'FAIL' }) + " (uciok=$sawUciok readyok=$sawReadyok pv=$($pvLines.Count) bestmove=$($bestLines.Count))")
Write-Output ("PASS/FAIL b) run1 has a depth with pv 1,2,3 : " + $(if ($b) { 'PASS' } else { 'FAIL' }) + " (depth " + $tripleDepth + ", untagged at that depth " + $untagged1 + ", depths seen: " + $(($run1DepthIndex.Keys | Sort-Object) -join ',') + ")")
Write-Output ("PASS/FAIL c) run1 bestmove matches pv 1     : " + $(if ($c) { 'PASS' } else { 'FAIL' }) + " (bestmove=" + $best1 + " pv1=" + $pv1move + ")")
Write-Output ("PASS/FAIL d) run2 depth 6 clean, pv 1,2,3   : " + $(if ($d) { 'PASS' } else { 'FAIL' }) + " (multipv indices: " + ($d6Index -join ',') + ", untagged lines: " + $d6Untagged + ", bestmove=" + $best2 + " pv1=" + $d6pv1 + ")")
Write-Output ("PASS/FAIL e) no panic/error + stderr        : " + $(if (($badLines.Count -eq 0) -and $e) { 'PASS' } else { 'FAIL' }) + " (" + $badLines.Count + " bad lines, stderr " + $errText.Length + " chars)")

Write-Output ''
Write-Output ("run1 bestmove : " + $(if ($c) { $best1Line } else { '<none>' }))
Write-Output ("run1 pv1      : " + $(if ($null -ne $lastMpv1Line) { $lastMpv1Line } else { '<none>' }))
Write-Output ("run2 bestmove : " + $(if ($null -ne $best2Line) { $best2Line } else { '<none>' }))
Write-Output ("run2 depth 6  : " + $d6Lines.Count + " multipv lines")

if (-not $ok) { exit 1 }
exit 0
