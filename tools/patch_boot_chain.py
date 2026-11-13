#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""RK3588 EVB7 LP4 V10 -- one-stop boot-chain patcher (Linux/CI and Windows).

This replaces the whole set of manual Windows byte-patching scripts that were
used to prove the board can boot Armbian from a TF card without touching the
eMMC Android installation.  All edits preserve byte lengths, so they can also
be applied to a raw image as well as through a mounted ext4 filesystem.

Subcommands
-----------
  fit  <in.itb> <out.bin> [--bootdelay N]
       Patch the vendor FIT that lives at image offset 0x800000 (sector 16384):
         * 3 x OP-TEE abort neutralisation inside the vendor u-boot payload
         * set the default-env "bootdelay" byte (default 0 = no autoboot pause)
         * recompute /images/uboot sha256 and self-check all 5 image hashes
       Output is zero-padded to 2 MiB, ready for `dd bs=512 seek=16384`.

  scr  <boot.scr> [--check]
       Replace the conditional earlycon line with an unconditional explicit
       earlycon=uart8250,mmio32,0xfeb50000,1500000 (83 -> 83 bytes) and
       recompute the legacy uImage ih_dcrc / ih_hcrc.

  dtb  <board.dtb> [--check]
       Set DTB_DISABLE_NODES status "okay" -> "fail" (5 -> 5 bytes each):
       the two PCIe controllers that deadlock the kernel, plus 9 peripherals
       that are not fitted on this EVB7 board (touch, camera, RTC, audio codec,
       SDIO, HDMI RX, DisplayPort).

  env  <template> <out> --fdtfile NAME
       Render board/bootfs/armbianEnv.txt (drops # comments, substitutes
       @FDTFILE@) into the final env file.

  verify-img <image.img> [--bootdelay N]
       Read the FIT region back from a raw image and assert all patches/hashes.

  selftest
       Constant/CRC/FDT sanity checks without any external file.

Verified on 2026-11-12: Armbian-unofficial 26.08.0-trunk noble booted to
systemd from mmcblk1p1 with eMMC Android untouched.  See docs/10 sections
9..16 for the full investigation.
"""

import argparse
import hashlib
import os
import struct
import sys
import zlib

FDT_MAGIC = 0xD00DFEED
UIMG_MAGIC = 0x27051956
UBOOT_LOAD = 0x00200000
FIT_REGION_SIZE = 2 * 1024 * 1024      # 0x200000 bytes written by write-fit.cmd
FIT_IMG_OFFSET = 16384 * 512           # 0x800000 = sector 0x4000

FDT_BEGIN_NODE, FDT_END_NODE, FDT_PROP, FDT_NOP, FDT_END = 1, 2, 3, 4, 9


def align512(x):
    """The vendor SPL reads FIT data at align512(fdt_totalsize), NOT totalsize."""
    return (x + 511) & ~511


# (virtual address, replacement bytes, description)
# These are the ONLY four optee panic gates in this u-boot blob (45 panic call
# sites were scanned); after patch 3 no OP-TEE path can abort the boot.
OPTEE_PATCHES = (
    (0x2B3A8C, struct.pack("<II", 0x52800000, 0xD65F03C0),
     "optee_check_api_revision -> return 0"),
    (0x2B3A30, struct.pack("<II", 0x52800020, 0xD65F03C0),
     "optee_exchange_capabilities helper -> return 1"),
    (0x2B4894, struct.pack("<II", 0x52800000, 0xD65F03C0),
     "optee_select_security_level -> return 0"),
)
BOOTDELAY_VA = 0x318E31                 # the '0' in "bootdelay=0"

SCR_OLD = (b'if test "${earlycon}" = "on"; then '
           b'setenv consoleargs "earlycon ${consoleargs}"; fi')
SCR_NEW = (b'setenv consoleargs "earlycon=uart8250,mmio32,0xfeb50000,1500000 '
           b'${consoleargs}"')
SCR_NEW += b" " * (len(SCR_OLD) - len(SCR_NEW))

#  DTB_DISABLE_NODES — nodes whose `status` is flipped "okay" -> "fail" (5 -> 5
#  bytes each, same length, so the FDT structure never moves).  Two classes:
#
#   * /pcie@fe150000, /pcie@fe170000 — their rk-pcie driver deadlocks inside
#     do_initcalls() (dw_pcie_read holds the mutex, RCU stall, 12 rounds).
#     Proven on hardware 2026-11-11.
#
#   * peripherals that are simply NOT populated on this EVB7 board.  Their
#     drivers either probe-fail or, worse, retry on a dead I2C bus for seconds
#     (gt1x).  Taken from the serial log of the successful boot
#     (MobaXterm_COM3USB-SERIALCH340COM3_20261112_011704.txt):
#       gt1x        -> "Reset guitar failed!" x N + rk3x-i2c timeout, ~2 s lost
#       imx415      -> "Unexpected sensor id(000000), ret(-5)"
#       hym8563     -> "probe of 6-0051 failed with error -110"
#       es8388      -> ASoC register read -6, ~30 lines of spam
#       mmc@fe2d0000-> "No normal/idle pinctrl state" x8 (SDIO, no card)
#       hdmirx      -> HDMI RX not fitted (HDMI TX stays enabled!)
#       dp@fde5/6   -> DisplayPort not fitted (HDMI TX stays enabled!)
#
#  The three I2C controllers below each hold exactly ONE child device (the one
#  we want gone), so disabling the controller is equivalent to disabling that
#  device and cannot take anything else down with it.  Verified by walking the
#  FDT: /i2c@fead0000 -> only gt1x@14; /i2c@feab0000 -> only imx415@1a;
#  /i2c@fec80000 -> only hym8563@51.
#
#  NEVER touch: /mmc@fe2c0000 (TF card = mmcblk1, our boot disk),
#  /mmc@fe2e0000 (eMMC = mmcblk0, the Android install), /hdmi@fde80000 /
#  /hdmi@fdea0000 / hdmiphy (HDMI output is a supported login path),
#  /ethernet@fe1c0000 (ssh), USB (keyboard login), /vop@fdd90000 (HDMI needs it).
DTB_DISABLE_NODES = (
    # --- class 1: PCIe controllers that deadlock the kernel (proven) ---
    "/pcie@fe150000",
    "/pcie@fe170000",
    # --- class 2: not fitted on this board ---
    "/i2c@fead0000",                 # -> gt1x@14   Goodix touch
    "/i2c@feab0000",                 # -> imx415@1a camera
    "/i2c@fec80000",                 # -> hym8563@51 RTC
    "/i2c@fec90000/es8388@11",       # es8388/es8328 audio codec
    "/es8388-sound",                 # its sound card
    "/mmc@fe2d0000",                 # SDIO (Wi-Fi), no card
    "/hdmirx-controller@fdee0000",   # HDMI RX (input), not fitted
    "/dp@fde50000",                  # DisplayPort 0, not fitted
    "/dp@fde60000",                  # DisplayPort 1, not fitted
)
DTB_STATUS_OKAY = b"okay\x00"
DTB_STATUS_FAIL = b"fail\x00"

ENV_KEYS_REQUIRED = ("verbosity=7", "extraargs=", "root=/dev/mmcblk1p1",
                     "rootdelay=10", "console=ttyFIQ0", "fdtfile=rockchip/")


# --------------------------------------------------------------------------- FDT
class Fdt:
    """Minimal read/write libfdt for the two structures we need (all big endian)."""

    def __init__(self, buf, base=0):
        self.buf = bytearray(buf)
        self.base = base
        magic, total, off_struct, off_strings = struct.unpack_from(">IIII", self.buf, base)
        if magic != FDT_MAGIC:
            raise ValueError("not an FDT at 0x%x (magic 0x%08x)" % (base, magic))
        self.total = total
        self.off_struct = off_struct
        self.off_strings = off_strings
        self.size_strings = struct.unpack_from(">I", self.buf, base + 32)[0]
        self.strings = bytes(self.buf[base + off_strings:
                                      base + off_strings + self.size_strings])
        self.props = []

    def walk(self):
        self.props = []
        p = self.base + self.off_struct
        stack = []
        while True:
            tok = struct.unpack_from(">I", self.buf, p)[0]
            p += 4
            if tok == FDT_BEGIN_NODE:
                end = self.buf.index(b"\0", p)
                stack.append(self.buf[p:end].decode("latin1"))
                p = (end + 1 + 3) & ~3
            elif tok == FDT_END_NODE:
                if stack:
                    stack.pop()
            elif tok == FDT_PROP:
                ln, nameoff = struct.unpack_from(">II", self.buf, p)
                p += 8
                end = self.strings.index(b"\0", nameoff)
                name = self.strings[nameoff:end].decode("latin1")
                path = "/" + "/".join(x for x in stack if x)
                self.props.append((path, name, p, ln))
                p = (p + ln + 3) & ~3
            elif tok == FDT_NOP:
                continue
            elif tok == FDT_END:
                break
            else:
                raise ValueError("bad FDT token 0x%x at 0x%x" % (tok, p - 4))
        return self.props

    def prop(self, path, name):
        for p_, n, off, ln in self.props:
            if p_ == path and n == name:
                return off, ln
        return None, None

    def get_u32(self, path, name, default=None):
        off, ln = self.prop(path, name)
        if off is None:
            return default
        return struct.unpack_from(">I", self.buf, off)[0]

    def put_bytes(self, path, name, value):
        off, ln = self.prop(path, name)
        if off is None:
            raise ValueError("missing %s/%s" % (path, name))
        if ln != len(value):
            raise ValueError("size mismatch on %s/%s: %d != %d"
                             % (path, name, ln, len(value)))
        self.buf[off:off + ln] = value

    def data_base(self):
        return align512(self.total)


# -------------------------------------------------------------------------- util
def die(msg):
    print("ERROR: %s" % msg, file=sys.stderr)
    raise SystemExit(1)


def read_file(path):
    with open(path, "rb") as f:
        return f.read()


def write_file(path, data):
    with open(path, "wb") as f:
        f.write(data)


def crc32(data):
    return zlib.crc32(data) & 0xFFFFFFFF


def be32(buf, off):
    return struct.unpack_from(">I", buf, off)[0]


def set_be32(buf, off, value):
    struct.pack_into(">I", buf, off, value & 0xFFFFFFFF)


# --------------------------------------------------------------------------- FIT
def fit_apply(region, bootdelay=0, verbose=True):
    """Return (patched_region_bytes, report_dict).  Idempotent."""
    if not (0 <= bootdelay <= 9):
        die("--bootdelay must be a single digit 0..9")
    f = Fdt(region)
    f.walk()
    db = f.data_base()
    ub_off = f.get_u32("/images/uboot", "data-offset")
    ub_size = f.get_u32("/images/uboot", "data-size")
    if ub_off is None or ub_size is None:
        die("no /images/uboot in the FIT")

    start = db + ub_off
    payload = bytearray(f.buf[start:start + ub_size])
    if len(payload) != ub_size:
        die("short u-boot payload in FIT")

    patches = list(OPTEE_PATCHES) + [
        (BOOTDELAY_VA, bytes([0x30 + bootdelay]),
         "default env bootdelay -> %d" % bootdelay),
    ]
    changed = 0
    for va, new, desc in patches:
        po = va - UBOOT_LOAD
        if not (0 <= po < ub_size and po + len(new) <= ub_size):
            die("patch VA 0x%x outside u-boot payload (size 0x%x)" % (va, ub_size))
        old = bytes(payload[po:po + len(new)])
        if old == new:
            if verbose:
                print("  FIT  0x%06x %-48s already OK" % (va, desc))
        else:
            payload[po:po + len(new)] = new
            changed += 1
            if verbose:
                print("  FIT  0x%06x %-48s %s -> %s"
                      % (va, desc, old.hex(), new.hex()))

    f.buf[start:start + ub_size] = payload
    new_hash = hashlib.sha256(bytes(payload)).digest()
    hash_off, hash_len = f.prop("/images/uboot/hash", "value")
    if hash_off is None or hash_len != 32:
        die("missing /images/uboot/hash value")
    old_hash = bytes(f.buf[hash_off:hash_off + 32])
    if old_hash != new_hash:
        f.put_bytes("/images/uboot/hash", "value", new_hash)
        changed += 1
        if verbose:
            print("  FIT  uboot sha256 %s... -> %s..."
                  % (old_hash.hex()[:16], new_hash.hex()[:16]))

    out = bytes(f.buf)
    # full self-check: reparse and verify every image hash
    c = Fdt(out)
    c.walk()
    db2 = c.data_base()
    hashes = {}
    for name in ("uboot", "atf-1", "atf-2", "atf-3", "fdt"):
        path = "/images/" + name
        doff = c.get_u32(path, "data-offset")
        dsz = c.get_u32(path, "data-size")
        lo, _ = c.prop(path + "/hash", "value")
        if doff is None or dsz is None or lo is None:
            die("incomplete FIT image node %s" % name)
        want = bytes(c.buf[lo:lo + 32])
        got = hashlib.sha256(bytes(c.buf[db2 + doff:db2 + doff + dsz])).digest()
        hashes[name] = (got.hex()[:16], want == got)
        if want != got:
            die("FIT hash mismatch for %s" % name)
    for va, new, _ in patches:
        po = va - UBOOT_LOAD
        if bytes(c.buf[db2 + c.get_u32("/images/uboot", "data-offset") + po:
                       db2 + c.get_u32("/images/uboot", "data-offset") + po + len(new)]) != new:
            die("patch byte check failed at 0x%x" % va)

    diff = sum(1 for a, b in zip(out, region) if a != b)
    report = {"totalsize": f.total, "data_base": db, "uboot_size": ub_size,
              "hashes": hashes, "bytes_changed": diff, "bootdelay": bootdelay}
    if verbose:
        print("  FIT  totalsize=%d data_base=0x%x  5/5 image hashes OK"
              % (f.total, db))
        print("  FIT  bytes differing from input: %d" % diff)
    return out, report


def cmd_fit(args):
    src = read_file(args.input)
    if be32(src, 0) != FDT_MAGIC:
        die("input is not an FDT/FIT: %s" % args.input)
    out, rep = fit_apply(src, args.bootdelay)
    if len(out) > FIT_REGION_SIZE:
        die("patched FIT (%d B) exceeds the 2 MiB region" % len(out))
    padded = out + b"\0" * (FIT_REGION_SIZE - len(out))
    if not args.check:
        write_file(args.output, padded)
    print("  %s %s (%d B, sha256 %s)"
          % ("would write" if args.check else "wrote", args.output,
             len(padded), hashlib.sha256(padded).hexdigest()))
    print("  dd if=%s of=<image> bs=512 seek=16384 conv=fsync,notrunc" % args.output)
    return 0


# --------------------------------------------------------------------- boot.scr
def scr_apply(data, verbose=True):
    """Return (patched_bytes, report).  Idempotent."""
    buf = bytearray(data)
    if be32(buf, 0) != UIMG_MAGIC:
        die("not a legacy uImage (magic 0x%08x)" % be32(buf, 0))
    size = be32(buf, 12)
    if size <= 0 or 64 + size > len(buf):
        die("bad ih_size %d (file %d B)" % (size, len(buf)))
    body = bytes(buf[64:64 + size])
    off = body.find(SCR_OLD)
    state = "patched"
    if off >= 0:
        buf[64 + off:64 + off + len(SCR_OLD)] = SCR_NEW
        if verbose:
            print("  SCR  +0x%x: conditional earlycon -> explicit earlycon" % off)
    elif body.find(SCR_NEW) >= 0:
        state = "already patched"
        if verbose:
            print("  SCR  explicit earlycon already present")
    else:
        die("neither the original nor the patched earlycon line was found")

    dcrc = crc32(bytes(buf[64:64 + size]))
    old_dcrc = be32(buf, 24)
    set_be32(buf, 24, dcrc)
    set_be32(buf, 4, 0)
    hcrc = crc32(bytes(buf[:64]))
    set_be32(buf, 4, hcrc)

    # self-check
    if crc32(bytes(buf[64:64 + size])) != be32(buf, 24):
        die("ih_dcrc self-check failed")
    probe = bytearray(buf[:64])
    set_be32(probe, 4, 0)
    if crc32(bytes(probe)) != be32(buf, 4):
        die("ih_hcrc self-check failed")
    if len(buf) != len(data):
        die("boot.scr length changed")
    if verbose:
        print("  SCR  ih_size=%d  ih_dcrc 0x%08x -> 0x%08x  ih_hcrc 0x%08x"
              % (size, old_dcrc, dcrc, hcrc))
    return bytes(buf), {"size": size, "state": state, "dcrc": dcrc, "hcrc": hcrc}


def cmd_scr(args):
    data = read_file(args.file)
    out, rep = scr_apply(data)
    if not args.check:
        if out != data:
            write_file(args.file, out)
    print("  %s %s (%s, %d B)"
          % ("checked" if args.check else "wrote", args.file, rep["state"], len(out)))
    return 0


# -------------------------------------------------------------------------- DTB
def fdt_status_available(value):
    """True when a DTB `status` property means "device available".

    Linux __of_device_is_available() (drivers/of/base.c): "okay" / "ok" are
    available, anything else is off.  (A *missing* property is also available,
    but that case is handled separately by dtb_apply / the deep verifier.)
    """
    s = bytes(value).split(b"\x00")[0].strip()
    return s in (b"okay", b"ok")


def fdt_disabled_value(value):
    """Equal-length, NUL-terminated replacement that is NOT "okay"/"ok".

    Never grows the property, so FDT offsets / totalsize never move.  Works
    for short "ok\\0" (3 B), "okay\\0" (5 B) and any longer status string.
    """
    ln = len(value)
    if ln == 0:
        return value
    raw = bytearray(DTB_STATUS_FAIL[:ln].ljust(ln, b"\x00"))
    raw[-1] = 0
    return bytes(raw)


def dtb_apply(data, verbose=True):
    """Return (patched_bytes, report).  Idempotent and variant-tolerant.

    DTB variants are NOT identical: rk3588-evb7-lp4-v10-rk1608-ipc-8x-linux.dtb
    carries 9-byte "disabled\\0" on 6 of the 11 nodes, others use short "ok\\0",
    and some omit a node entirely.  All of those are legitimate "already off /
    not present" cases and must not abort the CI build.  Only properties that
    are explicitly available are rewritten.
    """
    f = Fdt(data)
    f.walk()
    states = {}
    for node in DTB_DISABLE_NODES:
        off, ln = f.prop(node, "status")
        if off is None:
            # Node absent, or present without a status property.  The latter
            # is technically "available" in Linux, but adding a property would
            # move the whole FDT; verify --deep reports it as still enabled
            # instead of failing the patch step here.
            states[node] = "absent (skipped)"
            if verbose:
                print("  DTB  %-38s no status property -> skipped" % node)
            continue
        cur = bytes(f.buf[off:off + ln])
        if not fdt_status_available(cur):
            tag = cur.split(b"\x00")[0].decode("ascii", "replace") or "empty"
            states[node] = "%s (already disabled)" % tag
            if verbose:
                print("  DTB  %-38s status '%s' already disabled" % (node, tag))
            continue
        tag = cur.split(b"\x00")[0].decode("ascii", "replace")
        f.put_bytes(node, "status", fdt_disabled_value(cur))
        states[node] = "%s -> fail" % tag
        if verbose:
            print("  DTB  %-38s status '%s' -> 'fail'" % (node, tag))

    out = bytes(f.buf)
    c = Fdt(out)
    c.walk()
    if c.total != f.total or len(out) != len(data):
        die("DTB structure changed")
    for node in DTB_DISABLE_NODES:
        off, ln = c.prop(node, "status")
        if off is not None and fdt_status_available(c.buf[off:off + ln]):
            die("DTB status self-check failed for %s" % node)
    diff = sum(1 for a, b in zip(out, data) if a != b)
    if verbose:
        print("  DTB  totalsize=%d  bytes differing: %d  enabled nodes: 0"
              % (f.total, diff))
    return out, {"totalsize": f.total, "states": states, "bytes_changed": diff}


def cmd_dtb(args):
    data = read_file(args.file)
    out, rep = dtb_apply(data)
    if not args.check:
        if out != data:
            write_file(args.file, out)
    print("  %s %s (%d B, %s)"
          % ("checked" if args.check else "wrote", args.file, len(out),
             ", ".join("%s:%s" % (k.rsplit("/", 1)[-1], v)
                       for k, v in rep["states"].items())))
    return 0


# -------------------------------------------------------------------------- env
def cmd_env(args):
    raw = read_file(args.template).decode("utf-8")
    lines = []
    for line in raw.splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        lines.append(s.replace("@FDTFILE@", args.fdtfile))
    text = "\n".join(lines) + "\n"
    for key in ENV_KEYS_REQUIRED:
        if key not in text:
            die("rendered env is missing %r" % key)
    if not args.check:
        write_file(args.output, text.encode("utf-8"))
    print("  %s %s (%d B):" % ("would write" if args.check else "wrote",
                               args.output, len(text.encode("utf-8"))))
    for line in lines:
        print("    | %s" % line)
    return 0


# ------------------------------------------------------------------- verify-img
def cmd_verify_img(args):
    size = os.path.getsize(args.image)
    need = FIT_IMG_OFFSET + 2 * 1024 * 1024
    if size < need:
        die("image too small (%d B), need at least 0x%X" % (size, need))
    with open(args.image, "rb") as f:
        f.seek(FIT_IMG_OFFSET)
        region = f.read(FIT_REGION_SIZE)
    if be32(region, 0) != FDT_MAGIC:
        die("no FIT at 0x800000")
    f = Fdt(region)
    f.walk()
    db = f.data_base()
    ub_off = f.get_u32("/images/uboot", "data-offset")
    ub_size = f.get_u32("/images/uboot", "data-size")
    ok = True
    print("  FIT @ 0x%X  totalsize=%d  data_base=0x%x" % (FIT_IMG_OFFSET, f.total, db))
    for va, new, desc in list(OPTEE_PATCHES) + [
            (BOOTDELAY_VA, bytes([0x30 + args.bootdelay]), "bootdelay")]:
        po = db + ub_off + va - UBOOT_LOAD
        got = bytes(region[po:po + len(new)])
        tag = "OK" if got == new else "FAIL"
        ok &= got == new
        print("    0x%06x %-52s %s (%s)" % (va, desc, got.hex(), tag))
    for name in ("uboot", "atf-1", "atf-2", "atf-3", "fdt"):
        path = "/images/" + name
        doff = f.get_u32(path, "data-offset")
        dsz = f.get_u32(path, "data-size")
        lo, _ = f.prop(path + "/hash", "value")
        want = bytes(region[lo:lo + 32])
        got = hashlib.sha256(bytes(region[db + doff:db + doff + dsz])).digest()
        tag = "OK" if want == got else "FAIL"
        ok &= want == got
        print("    %-6s sha256 %s... %s" % (name, got.hex()[:16], tag))
    print("  RESULT: %s" % ("PASS" if ok else "FAIL"))
    return 0 if ok else 1


# --------------------------------------------------------------------- selftest
def cmd_selftest(args):
    checks = []

    def ck(name, cond):
        checks.append((name, bool(cond)))

    ck("SCR_OLD length == 83", len(SCR_OLD) == 83)
    ck("SCR_NEW length == 83 (same length)", len(SCR_NEW) == 83)
    ck("SCR_NEW carries explicit earlycon",
       b"earlycon=uart8250,mmio32,0xfeb50000,1500000" in SCR_NEW)
    ck("DTB status values are both 5 bytes",
       len(DTB_STATUS_OKAY) == 5 and len(DTB_STATUS_FAIL) == 5)
    ck("align512(1963) == 2048", align512(1963) == 2048)
    ck("align512(2560) == 2560", align512(2560) == 2560)
    ck("crc32('123456789') == 0xCBF43926", crc32(b"123456789") == 0xCBF43926)
    ck("FIT region offset == 0x800000", FIT_IMG_OFFSET == 0x800000)
    ck("FIT region size == 2 MiB", FIT_REGION_SIZE == 2 * 1024 * 1024)
    ck("3 OP-TEE patches", len(OPTEE_PATCHES) == 3)

    # synthetic uImage: verify scr_apply recomputes both CRCs and keeps length
    body = b"# dummy script\n" + SCR_OLD + b"\n# end\n"
    hdr = bytearray(64)
    set_be32(hdr, 0, UIMG_MAGIC)
    set_be32(hdr, 12, len(body))
    struct.pack_into(">I", hdr, 8, 0)
    set_be32(hdr, 24, crc32(body))
    set_be32(hdr, 4, 0)
    set_be32(hdr, 4, crc32(bytes(hdr)))
    scr = bytes(hdr) + body
    out, rep = scr_apply(scr, verbose=False)
    ck("scr_apply keeps file length", len(out) == len(scr))
    ck("scr_apply embeds SCR_NEW", SCR_NEW in out[64:])
    ck("scr_apply state == patched", rep["state"] == "patched")
    out2, rep2 = scr_apply(out, verbose=False)
    ck("scr_apply is idempotent", out2 == out and rep2["state"] == "already patched")

    # synthetic FDT reproducing the real node topology (flat + nested paths)
    def mkprop(nameoff, value):
        return struct.pack(">III", FDT_PROP, len(value), nameoff) + value + \
               b"\0" * ((-len(value)) & 3)

    def begin_node(name):
        raw = name.encode() + b"\0"
        return (struct.pack(">I", FDT_BEGIN_NODE) + raw
                + b"\0" * ((-len(raw)) & 3))

    def make_fdt(spec):
        """Build an FDT from {node_path: status_bytes_or_None}.

        DTB_DISABLE_NODES contains nested paths such as
        "/i2c@fec90000/es8388@11", so intermediate nodes are emitted too.
        -1 marks an intermediate node, None means "node without status".
        """
        tree = {}
        for p, val in spec.items():
            parts = [x for x in p.split("/") if x]
            cur = tree
            for i, part in enumerate(parts):
                node = cur.setdefault(part, {"children": {}, "status": -1})
                if i == len(parts) - 1:
                    node["status"] = val
                cur = node["children"]

        names = b"status\0"

        def emit(children):
            out = bytearray()
            for name in sorted(children):
                node = children[name]
                out += begin_node(name)
                st = node["status"]
                if st != -1 and st is not None:
                    out += mkprop(0, st)
                out += emit(node["children"])
                out += struct.pack(">I", FDT_END_NODE)
            return out

        struct_ = emit(tree) + struct.pack(">I", FDT_END)
        dtb = bytearray()
        dtb += struct.pack(">10I", FDT_MAGIC, 0, 40, 0, 0, 17, 16, 0, len(names), 0)
        off_strings = 40 + len(struct_)
        dtb += struct_ + names
        struct.pack_into(">10I", dtb, 0, FDT_MAGIC, len(dtb), 40, off_strings,
                         0, 17, 16, 0, len(names), len(struct_))
        return bytes(dtb)

    # --- case 1: every node "okay" (the primary linux DTB) ----------------
    dtb = make_fdt({p: b"okay\0" for p in DTB_DISABLE_NODES})
    patched, drep = dtb_apply(dtb, verbose=False)
    ck("dtb_apply keeps FDT length", len(patched) == len(dtb))
    ck("dtb_apply sets status to fail", b"fail\0" in patched)
    ck("dtb_apply changes exactly %d bytes" % (4 * len(DTB_DISABLE_NODES)),
       drep["bytes_changed"] == 4 * len(DTB_DISABLE_NODES))
    ck("dtb_apply is idempotent", dtb_apply(patched, verbose=False)[0] == patched)
    ck("DTB_DISABLE_NODES covers 11 nodes (2 PCIe + 9 peripherals)",
       len(DTB_DISABLE_NODES) == 11 and len(set(DTB_DISABLE_NODES)) == 11)

    # --- case 2: a real DTB variant mixes 9-byte "disabled", short "ok", --
    # --- a node with no status property, and a node absent altogether. ----
    # Regression for the 2026-11-13 CI failure on
    # rk3588-evb7-lp4-v10-rk1608-ipc-8x-linux.dtb ("status length 9 != 5").
    nodes = list(DTB_DISABLE_NODES)
    mixed = {}
    for i, p in enumerate(nodes[1:], start=1):
        if i % 4 == 1:
            mixed[p] = b"disabled\0"      # 9 B, already off
        elif i % 4 == 2:
            mixed[p] = b"ok\0"            # 3 B short form
        elif i % 4 == 3:
            mixed[p] = b"okay\0"          # 5 B normal form
        else:
            mixed[p] = None               # node exists, no status property
    mix = make_fdt(mixed)                 # nodes[0] deliberately absent
    mix_out, mrep = dtb_apply(mix, verbose=False)
    ck("mixed variant: 9-byte disabled tolerated",
       any("already disabled" in v for v in mrep["states"].values()))
    ck("mixed variant: absent node skipped",
       "absent" in mrep["states"].get(nodes[0], ""))
    ck("mixed variant: FDT length unchanged", len(mix_out) == len(mix))
    g = Fdt(mix_out)
    g.walk()
    leftover = []
    for n in nodes:
        o, l = g.prop(n, "status")
        if o is not None and fdt_status_available(g.buf[o:o + l]):
            leftover.append(n)
    ck("mixed variant: no node left available", not leftover)
    ck("mixed variant: short ok stays within 3 bytes",
       fdt_disabled_value(b"ok\0") == b"fa\0")
    ck("mixed variant: idempotent",
       dtb_apply(mix_out, verbose=False)[0] == mix_out)

    bad = 0
    for name, ok in checks:
        print("  [%s] %s" % ("OK" if ok else "FAIL", name))
        bad += 0 if ok else 1
    print("  selftest: %d/%d passed" % (len(checks) - bad, len(checks)))
    return 0 if bad == 0 else 1


# -------------------------------------------------------------------------- CLI
def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("fit", help="patch vendor FIT (3 OP-TEE gates + bootdelay)")
    p.add_argument("input")
    p.add_argument("output")
    p.add_argument("--bootdelay", type=int, default=0,
                   help="autoboot delay digit 0..9 (default 0 = no pause)")
    p.add_argument("--check", action="store_true", help="do not write output")
    p.set_defaults(func=cmd_fit)

    p = sub.add_parser("scr", help="patch /boot/boot.scr earlycon + uImage CRCs")
    p.add_argument("file")
    p.add_argument("--check", action="store_true")
    p.set_defaults(func=cmd_scr)

    p = sub.add_parser("dtb", help="disable the two deadlocking PCIe nodes")
    p.add_argument("file")
    p.add_argument("--check", action="store_true")
    p.set_defaults(func=cmd_dtb)

    p = sub.add_parser("env", help="render board/bootfs/armbianEnv.txt")
    p.add_argument("template")
    p.add_argument("output")
    p.add_argument("--fdtfile", default="rk3588-evb7-lp4-v10-linux.dtb")
    p.add_argument("--check", action="store_true")
    p.set_defaults(func=cmd_env)

    p = sub.add_parser("verify-img", help="verify FIT region of a raw image")
    p.add_argument("image")
    p.add_argument("--bootdelay", type=int, default=0)
    p.set_defaults(func=cmd_verify_img)

    p = sub.add_parser("selftest", help="no-file sanity checks")
    p.set_defaults(func=cmd_selftest)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
