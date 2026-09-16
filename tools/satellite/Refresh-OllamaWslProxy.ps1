<#
.SYNOPSIS
Refresh the Windows TCP proxy that exposes Ollama running inside WSL 2.

.DESCRIPTION
Starts the Ollama systemd service, discovers the current WSL NAT address,
replaces the matching netsh portproxy rule, and verifies both API endpoints.
Run from an elevated PowerShell prompt. This script does not support WSL
mirrored networking.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^\d{1,3}(\.\d{1,3}){3}$')]
    [string]$ListenAddress,

    [ValidateNotNullOrEmpty()]
    [string]$Distro = 'Ubuntu',

    [ValidateRange(1, 65535)]
    [int]$Port = 11434,

    [ValidateRange(1, 120)]
    [int]$RetryCount = 30,

    [ValidateRange(1, 30)]
    [int]$RetryDelaySeconds = 2,

    [string]$LogPath = 'C:\ProgramData\Execraft\ollama-wsl-proxy.log'
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

function Assert-Administrator {
    $identity = [System.Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = [System.Security.Principal.WindowsPrincipal]::new($identity)
    if (-not $principal.IsInRole([System.Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw 'Run this script from an elevated PowerShell prompt.'
    }
}

function Assert-IPv4Address {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Address,
        [Parameter(Mandatory = $true)]
        [string]$Name
    )

    $parsed = $null
    if (
        -not [System.Net.IPAddress]::TryParse($Address, [ref]$parsed) -or
        $parsed.AddressFamily -ne [System.Net.Sockets.AddressFamily]::InterNetwork
    ) {
        throw "$Name '$Address' is not a valid IPv4 address."
    }
}

function Write-ProxyLog {
    param([Parameter(Mandatory = $true)][string]$Message)

    $directory = Split-Path -Parent $LogPath
    if ($directory) {
        New-Item -ItemType Directory -Path $directory -Force | Out-Null
    }
    $timestamp = Get-Date -Format 'yyyy-MM-dd HH:mm:ss zzz'
    Add-Content -Path $LogPath -Value "[$timestamp] $Message"
}

function Invoke-WslCommand {
    param([Parameter(Mandatory = $true)][string[]]$Arguments)

    $output = (& wsl.exe -d $Distro @Arguments 2>&1) -join [Environment]::NewLine
    if ($LASTEXITCODE -ne 0) {
        throw "WSL command failed in '$Distro': $output"
    }
    return $output.Trim()
}

function Get-WslIpv4Address {
    for ($attempt = 1; $attempt -le $RetryCount; $attempt++) {
        $rawAddresses = Invoke-WslCommand -Arguments @('--', 'hostname', '-I')
        $address = (
            $rawAddresses -split '\s+' |
            Where-Object {
                $_ -match '^\d{1,3}(\.\d{1,3}){3}$' -and
                $_ -ne $ListenAddress -and
                -not $_.StartsWith('127.')
            } |
            Select-Object -First 1
        )
        if ($address) {
            Assert-IPv4Address -Address $address -Name 'WSL address'
            return $address
        }
        Start-Sleep -Seconds $RetryDelaySeconds
    }
    throw (
        "Unable to determine a distinct IPv4 address for WSL distribution '$Distro'. " +
        'The forwarding scripts support WSL NAT mode; do not use them with mirrored networking.'
    )
}

function Wait-OllamaApi {
    param([Parameter(Mandatory = $true)][string]$Address)

    $uri = "http://${Address}:$Port/api/version"
    $lastError = $null
    for ($attempt = 1; $attempt -le $RetryCount; $attempt++) {
        try {
            return Invoke-RestMethod -Uri $uri -Method Get -TimeoutSec 10
        }
        catch {
            $lastError = $_
            Start-Sleep -Seconds $RetryDelaySeconds
        }
    }
    throw "Ollama did not answer at $uri. Last error: $($lastError.Exception.Message)"
}

try {
    Assert-Administrator
    Assert-IPv4Address -Address $ListenAddress -Name 'ListenAddress'

    $localAddress = Get-NetIPAddress `
        -AddressFamily IPv4 `
        -IPAddress $ListenAddress `
        -ErrorAction SilentlyContinue
    if (-not $localAddress) {
        throw "Listen address $ListenAddress is not assigned to this Windows host."
    }

    $distroNames = (& wsl.exe --list --quiet 2>$null) | ForEach-Object { $_.Trim([char]0).Trim() }
    if ($Distro -notin $distroNames) {
        throw "WSL distribution '$Distro' is not installed for the current Windows user."
    }

    Set-Service iphlpsvc -StartupType Automatic
    if ((Get-Service iphlpsvc).Status -ne 'Running') {
        Start-Service iphlpsvc
    }

    Invoke-WslCommand -Arguments @('-u', 'root', '--', 'systemctl', 'start', 'ollama') | Out-Null
    $wslIp = Get-WslIpv4Address
    $wslVersion = Wait-OllamaApi -Address $wslIp

    & netsh.exe interface portproxy delete v4tov4 `
        "listenaddress=$ListenAddress" `
        "listenport=$Port" 2>$null | Out-Null

    & netsh.exe interface portproxy add v4tov4 `
        "listenaddress=$ListenAddress" `
        "listenport=$Port" `
        "connectaddress=$wslIp" `
        "connectport=$Port" | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw 'netsh failed to create the Ollama port proxy.'
    }

    $proxyVersion = Wait-OllamaApi -Address $ListenAddress
    if ($proxyVersion.version -ne $wslVersion.version) {
        throw 'The WSL and Windows proxy endpoints returned different Ollama versions.'
    }

    Write-ProxyLog (
        "Proxy ready: ${ListenAddress}:$Port -> ${wslIp}:$Port; " +
        "Ollama version=$($proxyVersion.version)"
    )
    Write-Output "Ollama proxy ready: ${ListenAddress}:$Port -> ${wslIp}:$Port"
}
catch {
    $originalError = $_
    try {
        Write-ProxyLog "ERROR: $($originalError.Exception.Message)"
    }
    catch {
        Write-Warning "Unable to write proxy log: $($_.Exception.Message)"
    }
    throw $originalError
}
