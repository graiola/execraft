<#
.SYNOPSIS
Install persistent Windows-to-WSL forwarding for an Ollama satellite.

.DESCRIPTION
Copies the refresh script to ProgramData, creates a restricted Windows firewall
rule, registers an elevated per-user logon task, and performs an immediate
proxy refresh. Run as the Windows user that owns the WSL distribution.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^\d{1,3}(\.\d{1,3}){3}$')]
    [string]$ListenAddress,

    [Parameter(Mandatory = $true)]
    [ValidatePattern('^\d{1,3}(\.\d{1,3}){3}(/\d{1,2})?$')]
    [string]$AllowedRemoteAddress,

    [ValidateNotNullOrEmpty()]
    [string]$Distro = 'Ubuntu',

    [ValidateRange(1, 65535)]
    [int]$Port = 11434,

    [ValidateNotNullOrEmpty()]
    [string]$InstallDirectory = 'C:\ProgramData\Execraft',

    [ValidateNotNullOrEmpty()]
    [string]$TaskName = 'Execraft Ollama WSL Proxy',

    [ValidateNotNullOrEmpty()]
    [string]$FirewallRuleName = 'Execraft Ollama WSL Proxy'
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

Assert-Administrator
Assert-IPv4Address -Address $ListenAddress -Name 'ListenAddress'

$sourceScript = Join-Path $PSScriptRoot 'Refresh-OllamaWslProxy.ps1'
if (-not (Test-Path -LiteralPath $sourceScript -PathType Leaf)) {
    throw "Required script not found: $sourceScript"
}

$localAddress = Get-NetIPAddress `
    -AddressFamily IPv4 `
    -IPAddress $ListenAddress `
    -ErrorAction SilentlyContinue
if (-not $localAddress) {
    throw "Listen address $ListenAddress is not assigned to this Windows host."
}

$networkProfile = Get-NetConnectionProfile `
    -InterfaceIndex $localAddress.InterfaceIndex `
    -ErrorAction SilentlyContinue
if (-not $networkProfile -or $networkProfile.NetworkCategory -ne 'Private') {
    throw (
        "The interface owning $ListenAddress must use the Windows Private network profile. " +
        'Run Set-NetConnectionProfile for the cluster adapter, then retry.'
    )
}

$distroNames = (& wsl.exe --list --quiet 2>$null) | ForEach-Object { $_.Trim([char]0).Trim() }
if ($Distro -notin $distroNames) {
    throw "WSL distribution '$Distro' is not installed for the current Windows user."
}

New-Item -ItemType Directory -Path $InstallDirectory -Force | Out-Null
$installedScript = Join-Path $InstallDirectory 'Refresh-OllamaWslProxy.ps1'
$logPath = Join-Path $InstallDirectory 'ollama-wsl-proxy.log'
Copy-Item -LiteralPath $sourceScript -Destination $installedScript -Force

Get-NetFirewallRule -DisplayName $FirewallRuleName -ErrorAction SilentlyContinue |
    Remove-NetFirewallRule
New-NetFirewallRule `
    -DisplayName $FirewallRuleName `
    -Description 'Allow the Execraft host to reach Ollama through the Windows-to-WSL proxy.' `
    -Direction Inbound `
    -Protocol TCP `
    -LocalAddress $ListenAddress `
    -LocalPort $Port `
    -RemoteAddress $AllowedRemoteAddress `
    -Action Allow `
    -Profile Private | Out-Null

$currentUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$actionArguments = (
    "-NoProfile -NonInteractive -ExecutionPolicy Bypass " +
    "-File `"$installedScript`" -ListenAddress $ListenAddress " +
    "-Distro `"$Distro`" -Port $Port -LogPath `"$logPath`""
)
$action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument $actionArguments
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $currentUser
$principal = New-ScheduledTaskPrincipal `
    -UserId $currentUser `
    -LogonType Interactive `
    -RunLevel Highest
$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 5) `
    -MultipleInstances IgnoreNew

Register-ScheduledTask `
    -TaskName $TaskName `
    -Description 'Refresh the WSL NAT port proxy for the Ollama satellite service.' `
    -Action $action `
    -Trigger $trigger `
    -Principal $principal `
    -Settings $settings `
    -Force | Out-Null

& $installedScript `
    -ListenAddress $ListenAddress `
    -Distro $Distro `
    -Port $Port `
    -LogPath $logPath

Write-Output "Installed scheduled task: $TaskName"
Write-Output "Installed firewall rule: $FirewallRuleName ($AllowedRemoteAddress -> ${ListenAddress}:$Port)"
Write-Output "Installed refresh script: $installedScript"
