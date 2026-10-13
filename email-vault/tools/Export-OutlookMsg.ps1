<#
.SYNOPSIS
  Save Outlook emails as .msg files into the vault's 00-inbox folder.

.DESCRIPTION
  Uses the classic Outlook desktop app via COM (Windows only; the "new Outlook" has no COM API).
  No Python needed. For incremental exports on a timer, prefer export_outlook_msg.py, which
  remembers what it already exported.
    -Selection      save the emails currently selected in Outlook
    -Days N         save emails received in the last N days from -FolderPath (default: Inbox)
  -FolderPath accepts "Inbox", "Inbox\Sub\Sub", "Sent Items", or "<Mailbox name>\Folder".
  Re-exported emails are de-duplicated by the converter (Message-ID / file hash).

.EXAMPLE
  .\tools\Export-OutlookMsg.ps1 -Selection
  .\tools\Export-OutlookMsg.ps1 -Days 7
  .\tools\Export-OutlookMsg.ps1 -Days 30 -FolderPath "Inbox\Projects\Q3"
  .\tools\Export-OutlookMsg.ps1 -Days 7 -FolderPath "Sent Items"
#>
param(
  [switch]$Selection,
  [double]$Days = 1,
  [string]$FolderPath = "Inbox",
  [string]$Vault = (Split-Path -Parent $PSScriptRoot)
)

$ErrorActionPreference = "Stop"
$olMSGUnicode = 9
$olMailItem = 43   # MailItem class id (skips meeting requests, reports, tasks)
$defaultFolders = @{ "inbox" = 6; "sent items" = 5; "sent" = 5; "deleted items" = 3; "drafts" = 16; "junk email" = 23 }
$inbox = Join-Path $Vault "00-inbox"
New-Item -ItemType Directory -Force -Path $inbox | Out-Null

$outlook = New-Object -ComObject Outlook.Application
$ns = $outlook.GetNamespace("MAPI")

function Get-SafeName([string]$s) {
  $s = ($s -replace '[\\/:*?"<>|#^\[\]\x00-\x1f]', ' ') -replace '\s+', ' '
  $s = $s.Trim(' ', '.')
  if ($s.Length -gt 100) { $s = $s.Substring(0, 100).TrimEnd(' ', '.') }
  if (-not $s) { $s = "untitled" }
  return $s
}

function Resolve-Folder([string]$path) {
  $parts = @($path -split '[\\/]' | Where-Object { $_.Trim() })
  $head = $parts[0].Trim().ToLower()
  if ($defaultFolders.ContainsKey($head)) {
    $folder = $ns.GetDefaultFolder($defaultFolders[$head])
  } else {
    $folder = $ns.Folders.Item($parts[0])   # mailbox / PST display name
  }
  if ($parts.Count -gt 1) {
    foreach ($p in $parts[1..($parts.Count - 1)]) { $folder = $folder.Folders.Item($p) }
  }
  return $folder
}

$script:saved = 0; $script:failed = 0
function Save-Item($item) {
  if ($item.Class -ne $olMailItem) { return }
  try {
    $stamp = $item.ReceivedTime.ToString("yyyy-MM-dd HHmm")
    $name = Get-SafeName "$stamp $($item.SenderName) - $($item.Subject)"
    $path = Join-Path $inbox "$name.msg"
    if (Test-Path $path) { return }
    $item.SaveAs($path, $olMSGUnicode)
    $script:saved++
  } catch {
    $script:failed++
    Write-Warning "Could not save '$($item.Subject)': $($_.Exception.Message)"
  }
}

if ($Selection) {
  foreach ($item in $outlook.ActiveExplorer().Selection) { Save-Item $item }
} else {
  $folder = Resolve-Folder $FolderPath
  $cutoff = (Get-Date).AddDays(-$Days)
  $items = $folder.Items
  $items.Sort("[ReceivedTime]", $true)    # newest first; stop at the cutoff (avoids locale-dependent Restrict filters)
  foreach ($item in $items) {
    try { $received = $item.ReceivedTime } catch { continue }
    if ($received -lt $cutoff) { break }
    Save-Item $item
  }
}
Write-Host "Saved $($script:saved) message(s) to $inbox; failed $($script:failed)"
