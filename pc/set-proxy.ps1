<#
.SYNOPSIS
  Point Windows' system proxy at the iPhone (or turn it back off).

.EXAMPLE
  .\set-proxy.ps1                       # on, 172.20.10.1:1080 (default hotspot IP)
  .\set-proxy.ps1 -Address 192.168.1.50 -Port 1080
  .\set-proxy.ps1 -Off
  .\set-proxy.ps1 -Test                 # check the phone proxy answers
#>
param(
    [string]$Address = "172.20.10.1",
    [int]$Port = 1080,
    [switch]$Off,
    [switch]$Test
)

$key = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Internet Settings"

function Update-WinInet {
    # Tell running apps the proxy settings changed so they don't need a restart.
    if (-not ("WinInet" -as [type])) {
        Add-Type -Namespace "" -Name WinInet -MemberDefinition @'
[DllImport("wininet.dll", SetLastError = true)]
public static extern bool InternetSetOption(System.IntPtr h, int opt, System.IntPtr buf, int len);
'@
    }
    [WinInet]::InternetSetOption([IntPtr]::Zero, 39, [IntPtr]::Zero, 0) | Out-Null  # SETTINGS_CHANGED
    [WinInet]::InternetSetOption([IntPtr]::Zero, 37, [IntPtr]::Zero, 0) | Out-Null  # REFRESH
}

if ($Test) {
    Write-Host "Checking ${Address}:${Port} ..."
    $ip = curl.exe -s --max-time 15 --proxy "socks5h://${Address}:${Port}" https://api.ipify.org
    if ($LASTEXITCODE -eq 0) {
        Write-Host "OK - traffic exits from $ip (should be your phone's carrier IP)" -ForegroundColor Green
    } else {
        Write-Host "No response. Is phone_proxy.py running and the PC on the phone's network?" -ForegroundColor Red
    }
    return
}

if ($Off) {
    Set-ItemProperty $key ProxyEnable 0
    Update-WinInet
    Write-Host "System proxy disabled."
    return
}

Set-ItemProperty $key ProxyServer "${Address}:${Port}"
Set-ItemProperty $key ProxyOverride "localhost;127.*;10.*;192.168.*;<local>"
Set-ItemProperty $key ProxyEnable 1
Update-WinInet
Write-Host "System proxy set to ${Address}:${Port}. Run with -Off to undo."
