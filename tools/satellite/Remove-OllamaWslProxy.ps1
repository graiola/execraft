<#
.SYNOPSIS
Remove the Windows forwarding configuration for an Ollama WSL satellite.

.DESCRIPTION
Deletes the selected netsh portproxy rule, scheduled task, firewall rule, and
installed refresh script. Ollama, WSL, models, and Linux configuration are not
removed.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^\d{1,3}(\.\d{1,3}){3}$')]
    [string]$ListenAddress,

    [ValidateRange(1, 65535)]
    [int]$Port = 11434,

    [ValidateNotNullOrEmpty()]
    [string]$InstallDirectory = 'C:\ProgramData\Execraft',

    [ValidateNotNullOrEmpty()]
    [string]$TaskName = 'Execraft Ollama WSL Proxy',

    [ValidateNotNullOrEmpty()]
    [string]$FirewallRuleName = 'Execraft Ollama WSL Proxy',

    [switch]$RemoveLog
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

Assert-Administrator

& netsh.exe interface portproxy delete v4tov4 `
    "listenaddress=$ListenAddress" `
    "listenport=$Port" 2>$null | Out-Null

$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($task) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}

Get-NetFirewallRule -DisplayName $FirewallRuleName -ErrorAction SilentlyContinue |
    Remove-NetFirewallRule

$installedScript = Join-Path $InstallDirectory 'Refresh-OllamaWslProxy.ps1'
Remove-Item -LiteralPath $installedScript -Force -ErrorAction SilentlyContinue

if ($RemoveLog) {
    Remove-Item `
        -LiteralPath (Join-Path $InstallDirectory 'ollama-wsl-proxy.log') `
        -Force `
        -ErrorAction SilentlyContinue
}

Write-Output "Removed proxy ${ListenAddress}:$Port, scheduled task, firewall rule, and installed refresh script."
