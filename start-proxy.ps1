# Start deepseek-cursor-proxy and its public SSH tunnel, then print the Base URL
# to paste into Cursor. ASCII only on purpose: Windows PowerShell 5.1 reads .ps1
# as ANSI unless the file carries a UTF-8 BOM.
#
# Why an SSH tunnel instead of ngrok: this network runs HTTPS through a Kaspersky
# MITM whose certificate has a duplicate extension, which Go refuses to parse.
# ngrok and cloudflared are Go binaries and cannot open a TLS session here.
# localhost.run is plain SSH with TLS termination on their side, so it works, but
# the subdomain is random per run - that is why this script prints the URL.

$ErrorActionPreference = 'Stop'

$repo = 'C:\GIT\deepseek-cursor-proxy'
$proxyExe = Join-Path $repo '.venv\Scripts\deepseek-cursor-proxy.exe'
$port = 9000
$urlFile = Join-Path $env:TEMP 'deepseek-proxy-url.txt'
$tunnelOut = Join-Path $env:TEMP 'dcp-tunnel.out'
$tunnelErr = Join-Path $env:TEMP 'dcp-tunnel.err'

# 1. Stop SSH tunnels left over from a previous run, otherwise the old one keeps
#    answering on its (now unknown to us) URL.
Get-CimInstance Win32_Process -Filter "Name = 'ssh.exe'" |
    Where-Object { $_.CommandLine -like '*localhost.run*' } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }

# 2. Start the proxy if nothing is listening on its port.
$listening = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
if (-not $listening) {
    Start-Process -FilePath $proxyExe -WindowStyle Hidden `
        -RedirectStandardOutput (Join-Path $env:TEMP 'dcp-proxy.out') `
        -RedirectStandardError (Join-Path $env:TEMP 'dcp-proxy.err')
    Start-Sleep -Seconds 5
}

try {
    $health = (Invoke-WebRequest "http://127.0.0.1:$port/healthz" -UseBasicParsing -TimeoutSec 5).Content
} catch {
    throw "Proxy is not answering on port $port. See $env:TEMP\dcp-proxy.err. $($_.Exception.Message)"
}
Write-Host "Proxy OK: $health"

# 3. Open the public tunnel. 127.0.0.1 (not localhost) matters: the proxy binds
#    IPv4 only, while localhost resolves to ::1 first and yields 502.
Remove-Item $tunnelOut, $tunnelErr -ErrorAction SilentlyContinue
Start-Process -FilePath 'C:\Windows\System32\OpenSSH\ssh.exe' -WindowStyle Hidden `
    -ArgumentList '-R', "80:127.0.0.1:$port", '-T',
    '-o', 'StrictHostKeyChecking=accept-new',
    '-o', 'ServerAliveInterval=30',
    '-o', 'ExitOnForwardFailure=yes',
    'nokey@localhost.run' `
    -RedirectStandardOutput $tunnelOut `
    -RedirectStandardError $tunnelErr

# 4. Wait for localhost.run to report the public address.
$publicUrl = $null
for ($i = 0; $i -lt 30; $i++) {
    Start-Sleep -Seconds 1
    $match = Select-String -Path $tunnelOut -Pattern 'https://[a-z0-9]+\.lhr\.life' -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if ($match) { $publicUrl = $match.Matches[0].Value; break }
}
if (-not $publicUrl) {
    throw "Tunnel did not report a public URL. See $tunnelErr"
}

$baseUrl = "$publicUrl/v1"
Set-Content -Path $urlFile -Value $baseUrl -Encoding ASCII

# 5. Prove the whole chain before telling the user it works.
try {
    $status = (Invoke-WebRequest "$publicUrl/healthz" -UseBasicParsing -TimeoutSec 20).StatusCode
} catch {
    throw "Tunnel $publicUrl is up but /healthz failed: $($_.Exception.Message)"
}
if ($status -ne 200) { throw "Tunnel /healthz returned $status" }

Write-Host ''
Write-Host "Public URL: $publicUrl"
Write-Host "Set Cursor > Settings > Models > API Keys > Override OpenAI Base URL to:"
Write-Host "    $baseUrl"
Write-Host ''
Write-Host "Saved to $urlFile. The subdomain changes on every run, so re-read it after a restart."
