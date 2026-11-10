<#
.SYNOPSIS
  Write mainline u-boot-rockchip.bin to sector 64 of a TF/SD card on Windows.

.DESCRIPTION
  Fixes the RK3588 EVB7 SD-boot blocker:
    "optee api revision is too low" / "### ERROR ### Please RESET the board ###"

  Only the bootloader area (from sector 64) is overwritten.
  The GPT, rootfs and the on-board eMMC / factory Android are NOT touched.

  Prerequisite: the card already has the Armbian image written with
  balenaEtcher (or Rufus in DD mode), so its GPT is clean.

  NOTE: This file is intentionally pure ASCII. Windows PowerShell 5.1 reads
  BOM-less .ps1 as ANSI/GBK, which corrupts non-ASCII text. Chinese docs are
  in README.md instead.

  Run from an ELEVATED PowerShell (not the WorkBuddy terminal):
    cd D:\WorkBuddy\2026-11-04-22-29-13\rk3588-evb7-armbian\windows
    Set-ExecutionPolicy -Scope Process Bypass -Force
    .\Write-UbootToSD.ps1 -ListOnly
    .\Write-UbootToSD.ps1 -UbootBin .\u-boot-rockchip.bin -DiskNumber 3

.PARAMETER ListOnly
  List USB disks and exit, without writing anything.

.PARAMETER UbootBin
  Path to mainline u-boot-rockchip.bin.

.PARAMETER DiskNumber
  Disk number from Get-Disk, i.e. \\.\PhysicalDriveN.
#>
[CmdletBinding()]
param(
    [string]$UbootBin = "",
    [int]$DiskNumber = -1,
    [switch]$ListOnly
)

$ErrorActionPreference = 'Stop'
$SECTOR = 512
$OFFSET = 64 * $SECTOR

function Show-Disks {
    Write-Host ""
    Write-Host "=== USB / removable disks ===" -ForegroundColor Cyan
    $usb = Get-CimInstance Win32_DiskDrive | Where-Object {
        $_.InterfaceType -eq 'USB' -or $_.MediaType -like '*Removable*'
    }
    if (-not $usb) {
        Write-Host "  No USB disk detected" -ForegroundColor Yellow
    }
    foreach ($d in $usb) {
        $gb = if ($d.Size) { "{0:N1} GB" -f ($d.Size / 1GB) } else { "(no media)" }
        $letters = (Get-Partition -DiskNumber $d.Index -ErrorAction SilentlyContinue |
                    Where-Object DriveLetter | ForEach-Object { "$($_.DriveLetter):" }) -join ','
        if (-not $letters) { $letters = 'none' }
        Write-Host ("  PhysicalDrive{0}  {1,-34} {2,10}  drive letters: {3}" -f `
                    $d.Index, $d.Model, $gb, $letters)
    }
    Write-Host ""
    Write-Host "=== All disks (for reference, DO NOT pick a system disk) ===" -ForegroundColor Cyan
    Get-Disk | Select-Object Number, FriendlyName, BusType, PartitionStyle,
        @{Name = 'SizeGB'; Expression = { [math]::Round($_.Size / 1GB, 1) } } |
        Format-Table -AutoSize | Out-Host
}

Show-Disks
if ($ListOnly) { exit 0 }

# Only the actual write needs elevation; -ListOnly works in a normal shell.
$principal = New-Object Security.Principal.WindowsPrincipal(
    [Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw "Writing requires an elevated PowerShell. Right-click Start -> Windows PowerShell (Admin) and rerun."
}

if (-not $UbootBin) {
    throw "Please specify -UbootBin with the path to u-boot-rockchip.bin"
}
if (-not (Test-Path $UbootBin)) {
    throw "File not found: $UbootBin"
}
$resolved = (Resolve-Path $UbootBin).Path
$blob = [System.IO.File]::ReadAllBytes($resolved)
if ($blob.Length -lt 4 -or [Text.Encoding]::ASCII.GetString($blob, 0, 4) -ne 'RKNS') {
    throw "u-boot-rockchip.bin does not start with RKNS. Make sure it is the binman output u-boot-rockchip.bin, not u-boot.itb or u-boot.bin."
}
if ($OFFSET + $blob.Length -gt 16MB) {
    throw "U-Boot blob is too large ($($blob.Length) bytes); it would overwrite the rootfs partition at 16 MiB."
}
Write-Host ""
Write-Host "  U-Boot : $resolved  ($($blob.Length) bytes, RKNS OK)" -ForegroundColor Green

if ($DiskNumber -lt 0) {
    $raw = Read-Host "Enter the disk number to write (e.g. 3 for PhysicalDrive3)"
    if ($raw -notmatch '^\d+$') { throw "Invalid disk number" }
    $DiskNumber = [int]$raw
}

$disk = Get-Disk -Number $DiskNumber -ErrorAction Stop
if ($disk.BusType -ne 'USB' -and $disk.BusType -ne 'SD') {
    Write-Host ""
    Write-Host "WARNING: Disk $DiskNumber BusType is $($disk.BusType), it does not look like a TF card / reader." -ForegroundColor Yellow
}
$letters = (Get-Partition -DiskNumber $DiskNumber -ErrorAction SilentlyContinue |
            Where-Object DriveLetter | ForEach-Object { $_.DriveLetter }) -join ','
if ($letters -match 'C') {
    throw "This disk contains the C: system volume. Refusing to write."
}

Write-Host ""
Write-Host "==================== DESTRUCTIVE ACTION ====================" -ForegroundColor Red
Write-Host "  Target    : \\.\PhysicalDrive$DiskNumber"
Write-Host "  Model     : $($disk.FriendlyName)"
Write-Host "  Size      : $([math]::Round($disk.Size / 1GB, 1)) GB"
Write-Host "  Write at  : offset $OFFSET (sector 64), length $($blob.Length) bytes"
Write-Host "  Impact    : bootloader area only. GPT and rootfs unchanged. On-board eMMC untouched."
Write-Host "  Risk      : picking the wrong disk will destroy its data."
Write-Host "==========================================================" -ForegroundColor Red
$a = Read-Host "Type disk number $DiskNumber again to confirm"
if ($a -ne "$DiskNumber") { Write-Host "Cancelled"; exit 1 }
$b = Read-Host "Type YES to start writing"
if ($b -cne 'YES') { Write-Host "Cancelled"; exit 1 }

$path = "\\.\PhysicalDrive$DiskNumber"
$fs = $null
try {
    $fs = [System.IO.File]::Open($path, [System.IO.FileMode]::Open,
                                 [System.IO.FileAccess]::ReadWrite,
                                 [System.IO.FileShare]::ReadWrite)
    [void]$fs.Seek($OFFSET, [System.IO.SeekOrigin]::Begin)
    $fs.Write($blob, 0, $blob.Length)
    $fs.Flush($true)
    Write-Host ""
    Write-Host "  Written. Reading back for verification..." -ForegroundColor Green

    [void]$fs.Seek($OFFSET, [System.IO.SeekOrigin]::Begin)
    $back = New-Object byte[] 4
    [void]$fs.Read($back, 0, 4)
    $magic = [Text.Encoding]::ASCII.GetString($back)
    Write-Host "  sector 64 magic: $magic"
    if ($magic -ne 'RKNS') { throw "Read-back verification failed" }
}
catch {
    Write-Host ""
    Write-Host "Write failed: $($_.Exception.Message)" -ForegroundColor Red
    $help = @(
        "",
        "Windows sometimes blocks raw writes to a mounted disk. Pick one:",
        "",
        "  [A] RECOMMENDED - inject into the full image, then re-flash with Etcher",
        "        python patch-bootloader.py <base.img.xz> u-boot-rockchip.bin <new.img.xz>",
        "        then write new.img.xz with balenaEtcher (also repairs the GPT CRC).",
        "",
        "  [B] Take the disk offline first, then retry this script (elevated PowerShell):",
        "        Set-Disk -Number $DiskNumber -IsOffline `$true",
        "        .\Write-UbootToSD.ps1 -UbootBin .\u-boot-rockchip.bin -DiskNumber $DiskNumber",
        "        Set-Disk -Number $DiskNumber -IsOffline `$false",
        "",
        "  [C] Use WSL2 / an Ubuntu VM:",
        "        sudo dd if=u-boot-rockchip.bin of=/dev/sdX bs=512 seek=64 conv=fsync,notrunc",
        ""
    )
    $help | ForEach-Object { Write-Host $_ -ForegroundColor Yellow }
    exit 1
}
finally {
    if ($fs) { $fs.Dispose() }
}

$done = @(
    "",
    "DONE. Safely eject the TF card and power on the board.",
    "Serial: 1500000 8N1. Expect:",
    "  - a mainline U-Boot version string (not 2017.09_armbian-...)",
    "  - NO 'optee api revision is too low' / 'Please RESET the board'",
    "  - 'Found U-Boot script /boot/boot.scr' then '## Loading init Ramdisk' then login:",
    "",
    "If SPL shows 'MMC: no card present' or 'spl: mmc init failed',",
    "the mainline SPL DTB does not match this board's SD card-detect GPIO (GPIO0_A4).",
    "Fall back to plan 2 in docs/04 (rebuild vendor U-Boot without CONFIG_OPTEE_CLIENT).",
    ""
)
$done | ForEach-Object { Write-Host $_ -ForegroundColor Green }
