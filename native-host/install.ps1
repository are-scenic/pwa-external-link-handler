<#
.SYNOPSIS
    PWA External Link Handler - Windows native-host installer.

.DESCRIPTION
    Installs the Python native-messaging host and registers per-browser
    registry keys so Chromium-family browsers can discover it. Writes a
    rendered manifest per browser (with the correct allowed_origins
    extension ID) into the install directory.

    The installer runs in HKCU by default (per-user, no admin required).
    Use -SystemWide to write to HKLM instead - requires elevation.

    Browser coverage:
    * Chrome   -> HKCU\Software\Google\Chrome\NativeMessagingHosts (Chrome ID)
    * Edge     -> HKCU\Software\Microsoft\Edge\NativeMessagingHosts (Edge ID)
    * Chromium -> HKCU\Software\Chromium\NativeMessagingHosts (Chrome ID)
    * Brave/Vivaldi/Opera: NOT written separately. Per research Q4, these
      browsers on Windows resolve via Chrome's registry keys, so the
      Chrome registration above covers them.

    On Windows, Edge falls back to Chrome's registry locations if no
    Edge-scoped entry is present. To prevent Edge from picking up the
    Chrome manifest (with the wrong allowed_origins ID), this installer
    ALWAYS writes an Edge-scoped registry entry pointing at the Edge-
    specific manifest.

.PARAMETER Uninstall
    Remove all registry keys and installed files written by this
    installer. Combined with -SystemWide, removes from HKLM instead of
    HKCU. By default the user data directory is preserved; use -Purge
    to also remove it.

.PARAMETER Purge
    With -Uninstall, also remove the install directory contents
    (manifests, logs, config). Without -Uninstall, has no effect.

.PARAMETER SystemWide
    Write to HKEY_LOCAL_MACHINE instead of HKEY_CURRENT_USER. Requires
    an elevated PowerShell session.

.PARAMETER ChromeWebStoreId
    Override the Chrome Web Store extension ID. Defaults to the
    environment variable CHROME_WEB_STORE_ID, or a placeholder. Must
    match Chrome's [a-p]{32} alphabet (or be a REPLACE_WITH_*
    placeholder).

.PARAMETER EdgeAddonsId
    Override the Microsoft Edge Add-ons extension ID. Defaults to the
    environment variable EDGE_ADDONS_ID, or a placeholder.

.PARAMETER InstallPath
    Override the install directory. Defaults to %LOCALAPPDATA%\pwa-elh.

.EXAMPLE
    PS> .\install.ps1
    Install for the current user.

.EXAMPLE
    PS> .\install.ps1 -Uninstall
    Remove the current-user install (preserves user data).

.EXAMPLE
    PS> .\install.ps1 -Uninstall -Purge
    Remove the current-user install AND user data.

.EXAMPLE
    PS> .\install.ps1 -SystemWide
    Install for all users (requires elevation).
#>

[CmdletBinding()]
param(
    [switch]$Uninstall,
    [switch]$Purge,
    [switch]$SystemWide,
    [string]$ChromeWebStoreId = $env:CHROME_WEB_STORE_ID,
    [string]$EdgeAddonsId    = $env:EDGE_ADDONS_ID,
    [string]$InstallPath
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

# ---------- constants -----------------------------------------------------

$HostName     = 'com.aaharonov.pwa_elh'
$HostDesc     = 'PWA External Link Handler - opens external links in the OS default browser.'
$ScriptDir    = Split-Path -Parent $MyInvocation.MyCommand.Path
$HostSrc      = Join-Path $ScriptDir 'host.py'

if (-not $InstallPath) {
    $InstallPath = Join-Path $env:LOCALAPPDATA 'pwa-elh'
}
$HostDst      = Join-Path $InstallPath 'host.py'

if (-not $ChromeWebStoreId) { $ChromeWebStoreId = 'REPLACE_WITH_CHROME_WEB_STORE_ID' }
if (-not $EdgeAddonsId)     { $EdgeAddonsId     = 'REPLACE_WITH_EDGE_ADDONS_ID' }

# Validate extension IDs (Chrome Web Store charset is [a-p]{32}).
$ExtIdPattern = '^([a-p]{32}|REPLACE_WITH_[A-Z_]+)$'
if ($ChromeWebStoreId -notmatch $ExtIdPattern) {
    throw "Invalid ChromeWebStoreId (expected 32 lowercase a-p chars or REPLACE_WITH_*): $ChromeWebStoreId"
}
if ($EdgeAddonsId -notmatch $ExtIdPattern) {
    throw "Invalid EdgeAddonsId (expected 32 lowercase a-p chars or REPLACE_WITH_*): $EdgeAddonsId"
}

$Hive = if ($SystemWide) { 'HKLM:' } else { 'HKCU:' }

# Per design §3.4.10 and research Q4:
#   Chrome   -> HKCU\Software\Google\Chrome\NativeMessagingHosts
#   Edge     -> HKCU\Software\Microsoft\Edge\NativeMessagingHosts (scoped
#               explicitly to avoid Edge's fallback to Chrome's keys
#               loading the wrong allowed_origins ID).
#   Chromium -> HKCU\Software\Chromium\NativeMessagingHosts
# Brave/Vivaldi/Opera on Windows resolve via Chrome's keys per Q4 line 126
# ("Brave's own registry path under SOFTWARE\BraveSoftware\... is not
# officially documented"); the Chrome registration above covers them.
$BrowserMap = @(
    [pscustomobject]@{ Key = "$Hive\Software\Google\Chrome\NativeMessagingHosts\$HostName";  Manifest = 'chrome.json';   Browser = 'chrome'   }
    [pscustomobject]@{ Key = "$Hive\Software\Microsoft\Edge\NativeMessagingHosts\$HostName"; Manifest = 'edge.json';     Browser = 'edge'     }
    [pscustomobject]@{ Key = "$Hive\Software\Chromium\NativeMessagingHosts\$HostName";       Manifest = 'chromium.json'; Browser = 'chromium' }
)

# ---------- helpers -------------------------------------------------------

function Write-Log { param([string]$Message) Write-Host "install: $Message" }

# Write a UTF-8 file without BOM. Set-Content -Encoding UTF8 writes BOM on
# Windows PowerShell 5.1 but BOM-less on PS Core 7; Chromium parses both
# but our re-render byte-identity check needs determinism.
function Write-Utf8NoBom {
    param(
        [Parameter(Mandatory)] [string]$Path,
        [Parameter(Mandatory)] [string]$Content
    )
    $bytes = [System.Text.UTF8Encoding]::new($false).GetBytes($Content)
    [System.IO.File]::WriteAllBytes($Path, $bytes)
}

function New-ManifestJson {
    param(
        [Parameter(Mandatory)] [string]$HostPath,
        [Parameter(Mandatory)] [string]$ExtensionId
    )
    $origin = "chrome-extension://$ExtensionId/"
    $manifest = [ordered]@{
        name            = $HostName
        description     = $HostDesc
        path            = $HostPath
        type            = 'stdio'
        allowed_origins = @($origin)
    }
    # ConvertTo-Json with -Compress avoids version-dependent indentation.
    return ($manifest | ConvertTo-Json -Depth 4 -Compress)
}

function Install-Manifests {
    if (-not (Test-Path $HostSrc)) {
        throw "Missing host source: $HostSrc"
    }

    New-Item -ItemType Directory -Force -Path $InstallPath | Out-Null
    Copy-Item -Force -Path $HostSrc -Destination $HostDst

    foreach ($entry in $BrowserMap) {
        $extId = if ($entry.Browser -eq 'edge') { $EdgeAddonsId } else { $ChromeWebStoreId }
        $manifestPath = Join-Path $InstallPath $entry.Manifest
        $rendered = New-ManifestJson -HostPath $HostDst -ExtensionId $extId
        Write-Utf8NoBom -Path $manifestPath -Content $rendered
        Write-Log "manifest: $manifestPath"
    }

    foreach ($entry in $BrowserMap) {
        $manifestPath = Join-Path $InstallPath $entry.Manifest
        try {
            New-Item -Path $entry.Key -Force | Out-Null
            Set-ItemProperty -Path $entry.Key -Name '(Default)' -Value $manifestPath
            Write-Log "registry: $($entry.Key)"
        } catch {
            Write-Log "warning: failed to write $($entry.Key): $($_.Exception.Message)"
        }
    }
}

function Uninstall-Manifests {
    foreach ($entry in $BrowserMap) {
        if (Test-Path $entry.Key) {
            try {
                Remove-Item -Path $entry.Key -Recurse -Force
                Write-Log "removed registry: $($entry.Key)"
            } catch {
                Write-Log "warning: failed to remove $($entry.Key): $($_.Exception.Message)"
            }
        }
    }
    if (Test-Path $InstallPath) {
        # Remove the host launcher and rendered manifests; leave user data
        # (config / logs / sentinel) intact unless -Purge is set.
        Remove-Item -Path (Join-Path $InstallPath 'host.py')      -Force -ErrorAction SilentlyContinue
        Remove-Item -Path (Join-Path $InstallPath 'chrome.json')  -Force -ErrorAction SilentlyContinue
        Remove-Item -Path (Join-Path $InstallPath 'edge.json')    -Force -ErrorAction SilentlyContinue
        Remove-Item -Path (Join-Path $InstallPath 'chromium.json') -Force -ErrorAction SilentlyContinue
        if ($Purge) {
            try {
                Remove-Item -Path $InstallPath -Recurse -Force
                Write-Log "purged user data: $InstallPath"
            } catch {
                Write-Log "warning: failed to remove $InstallPath : $($_.Exception.Message)"
            }
        }
    }
}

# ---------- main ----------------------------------------------------------

if ($Uninstall) {
    Uninstall-Manifests
} else {
    Install-Manifests
}
Write-Log 'done.'
