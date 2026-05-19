<#
.SYNOPSIS
    PWA External Link Handler - Windows native-host uninstaller.

.DESCRIPTION
    Thin wrapper around `install.ps1 -Uninstall`. Pass -SystemWide to
    remove the HKLM install (requires elevation). Pass -Purge to also
    remove the install directory contents.

.PARAMETER SystemWide
    Remove the HKLM install instead of the HKCU install.

.PARAMETER Purge
    Also remove the install directory (config, logs, sentinel).

.EXAMPLE
    PS> .\uninstall.ps1
    Remove the current-user install (preserves user data).

.EXAMPLE
    PS> .\uninstall.ps1 -Purge
    Remove the current-user install AND user data.
#>

[CmdletBinding()]
param(
    [switch]$SystemWide,
    [switch]$Purge
)

$ErrorActionPreference = 'Stop'
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
& (Join-Path $scriptDir 'install.ps1') -Uninstall -SystemWide:$SystemWide -Purge:$Purge
