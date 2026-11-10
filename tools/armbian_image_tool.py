#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
armbian_image_tool.py — RK3588 EVB7 LP4 V10 / Armbian TF 卡镜像离线校验与资产提取工具

不依赖 mount / losetup / debugfs，纯 Python 直接解析：
  - GPT 分区表
  - Rockchip bootloader (sector 64 / 16384)
  - ext4（superblock / group descriptor / inode / extent / directory entry）
  - /boot 启动配置（armbianEnv.txt / boot.cmd / boot.scr）与 dtb 目录

支持 .img 与 .img.xz 两种输入；Windows / Linux / macOS 均可运行。

用法:
  python3 armbian_image_tool.py verify    <image.img[.xz]> [--deep] [--json]
  python3 armbian_image_tool.py extract   <image.img[.xz]> --out <dir>
  python3 armbian_image_tool.py patch-env <image.img[.xz]> <out.img[.xz]> \
                                          --fdtfile <name> [--env-file <file>]

说明:
  verify 的 --deep 需要流式解压到镜像 ~8GB 偏移处读取 /boot 配置文件，
  .xz 输入大约需要 1 分钟；不加 --deep 只检查前 256MB，秒级完成。
"""

import argparse
import json
import lzma
import os
import struct
import sys

SECTOR = 512
BOOT_REGION = 16 * 1024 * 1024          # ophub rockchip: skip_mb=16
LBA_IDBLOADER = 64                     # 0x8000
LBA_UBOOT_ITB = 16384                  # 0x800000
FDT_MAGIC = bytes.fromhex("d00dfeed")
RKNS_MAGIC = b"RKNS"
EXT4_MAGIC = 0xEF53


# --------------------------------------------------------------------------
# 镜像读取层
# --------------------------------------------------------------------------
class ImageReader:
    """对 .img 使用随机访问；对 .img.xz 使用一次性流式扫描 + 区间收集。"""

    def __init__(self, path, progress=True):
        self.path = path
        self.is_xz = path.lower().endswith(".xz")
        self.progress = progress
        self._fh = None
        if not self.is_xz:
            self._fh = open(path, "rb")
            self.size = os.path.getsize(path)
        else:
            self.size = None

    def close(self):
        if self._fh:
            self._fh.close()
            self._fh = None

    def prefix(self, n):
        """读取镜像前 n 字节（.xz 时流式解压前 n 字节）。"""
        if not self.is_xz:
            self._fh.seek(0)
            return self._fh.read(n)
        with lzma.open(self.path, "rb") as f:
            return read_exact(f, n)

    def read_at(self, off, size):
        if not self.is_xz:
            self._fh.seek(off)
            return self._fh.read(size)
        raise RuntimeError("read_at() requires stream_ranges() for .xz input")

    def stream_ranges(self, ranges):
        """一次性流式解压，收集 [(label, offset, size), ...] 区间内容。"""
        if not self.is_xz:
            out = {}
            for label, off, size in ranges:
                self._fh.seek(off)
                out[label] = self._fh.read(size)
            return out
        lo = min(off for _, off, _ in ranges)
        hi = max(off + size for _, off, size in ranges)
        pending = sorted(ranges, key=lambda x: x[1])
        out = {}
        pos = 0
        chunk = 8 * 1024 * 1024
        with lzma.open(self.path, "rb") as f:
            while pos < hi:
                want = min(chunk, hi - pos)
                buf = f.read(want)
                if not buf:
                    break
                end = pos + len(buf)
                for label, off, size in pending:
                    if off >= end or off + size <= pos:
                        continue
                    a = max(off, pos)
                    b = min(off + size, end)
                    out.setdefault(label, bytearray())
                    out[label] += buf[a - pos:b - pos]
                pos = end
                if self.progress and pos % (1024 ** 3) < chunk:
                    print(f"  ... {pos / 1024 ** 3:.1f} GiB / {hi / 1024 ** 3:.1f} GiB",
                          file=sys.stderr, flush=True)
        return {k: bytes(v) for k, v in out.items()}


def read_exact(f, n):
    parts, got = [], 0
    while got < n:
        b = f.read(min(8 * 1024 * 1024, n - got))
        if not b:
            break
        parts.append(b)
        got += len(b)
    return b"".join(parts)


# --------------------------------------------------------------------------
# GPT
# --------------------------------------------------------------------------
def parse_gpt(head):
    r = {"present": False, "partitions": []}
    if head[512:520] != b"EFI PART":
        return r
    r["present"] = True
    hdr = head[512:604]
    r["first_usable_lba"] = struct.unpack_from("<Q", hdr, 40)[0]
    r["last_usable_lba"] = struct.unpack_from("<Q", hdr, 48)[0]
    ent_lba = struct.unpack_from("<Q", hdr, 72)[0]
    num, sz = struct.unpack_from("<II", hdr, 80)
    for i in range(min(num, 32)):
        off = ent_lba * SECTOR + i * sz
        ent = head[off:off + sz]
        if len(ent) < 128 or ent[:16] == b"\x00" * 16:
            continue
        name = ent[56:128].decode("utf-16-le", "replace").rstrip("\x00")
        st, en = struct.unpack_from("<QQ", ent, 32)
        r["partitions"].append({
            "index": i + 1,
            "name": name,
            "start_lba": st,
            "end_lba": en,
            "size_mib": round((en - st + 1) * SECTOR / 1048576, 1),
            "type_guid": ent[:16].hex(),
            "unique_guid": "%08x-%04x-%04x-%s" % (
                struct.unpack_from("<I", ent, 16)[0],
                struct.unpack_from("<H", ent, 20)[0],
                struct.unpack_from("<H", ent, 22)[0],
                ent[24:32].hex()),
        })
    return r


def parse_uuid_from_guid(guid_le_bytes):
    """GPT unique GUID (little-endian 前 3 段) → 标准 UUID 字符串。"""
    a, b, c = struct.unpack_from("<IHH", guid_le_bytes, 0)
    return f"{a:08x}-{b:04x}-{c:04x}-{guid_le_bytes[8:10].hex()}-{guid_le_bytes[10:16].hex()}"


# --------------------------------------------------------------------------
# ext4（只读，无 mount）
# --------------------------------------------------------------------------
class Ext4:
    def __init__(self, raw, part_off):
        self.raw = raw
        self.part = part_off
        sb = raw[part_off + 1024:part_off + 2048]
        if len(sb) < 1024 or struct.unpack_from("<H", sb, 56)[0] != EXT4_MAGIC:
            raise ValueError("ext4 superblock magic not found at partition +0x400")
        self.bs = 1024 << struct.unpack_from("<I", sb, 24)[0]
        self.first_data = struct.unpack_from("<I", sb, 20)[0]
        self.ipg = struct.unpack_from("<I", sb, 40)[0]
        self.isize = struct.unpack_from("<H", sb, 88)[0]
        self.dsize = struct.unpack_from("<H", sb, 254)[0] or 32
        self.gdt = (self.first_data + 1) * self.bs
        # ext4 s_uuid（大写三段为小端，后两段为字节序原样）；root=UUID= 用的是它，
        # 不是 GPT 分区的 UniquePartitionGUID（root=PARTUUID= 才用后者）
        u = sb[0x68:0x78]
        a, b, c = struct.unpack_from(">IHH", u, 0)
        self.uuid = f"{a:08x}-{b:04x}-{c:04x}-{u[8:10].hex()}-{u[10:16].hex()}"
        self.label = sb[0x78:0x88].split(b"\x00")[0].decode("utf8", "replace")

    def _inode_raw(self, num):
        g, i = (num - 1) // self.ipg, (num - 1) % self.ipg
        gd = self.raw[self.part + self.gdt + g * self.dsize:
                      self.part + self.gdt + g * self.dsize + self.dsize]
        tab = struct.unpack_from("<I", gd, 8)[0]
        o = self.part + tab * self.bs + i * self.isize
        return self.raw[o:o + self.isize]

    def extents(self, num):
        raw = self._inode_raw(num)
        magic, ents, _mx, depth, _gen = struct.unpack_from("<HHHHI", raw, 40)
        out = []
        if magic == 0xF30A and depth == 0:
            for k in range(min(ents, 64)):
                eb, ln, hi, lo = struct.unpack_from("<IHHI", raw, 52 + k * 12)
                out.append(((hi << 32) | lo, ln if ln < 32768 else ln - 32768))
        return out, depth, raw

    def file_info(self, num):
        raw = self._inode_raw(num)
        mode = struct.unpack_from("<H", raw, 0)[0]
        size = struct.unpack_from("<I", raw, 4)[0] | (struct.unpack_from("<I", raw, 108)[0] << 32)
        ex, depth, _ = self.extents(num)
        ftype = {0x8000: "REG", 0xA000: "LNK", 0x4000: "DIR"}.get(mode & 0xF000, hex(mode))
        blocks = [(self.part + s * self.bs, c * self.bs) for s, c in ex]
        link = raw[40:40 + size].decode("utf8", "replace") if ftype == "LNK" else ""
        return {"mode": ftype, "size": size, "blocks": blocks, "link": link}

    def listdir(self, num):
        info = self.file_info(num)
        if info["mode"] != "DIR":
            return []
        buf = b"".join(self.raw[o:o + n] for o, n in info["blocks"] if o + n <= len(self.raw))
        out, p = [], 0
        while p + 8 <= len(buf):
            i, rec, nl, _ft = struct.unpack_from("<IHBB", buf, p)
            if rec < 8:
                break
            if i and nl:
                out.append((i, buf[p + 8:p + 8 + nl].decode("utf8", "replace")))
            p += rec
        return out

    def child(self, num, name):
        for i, n in self.listdir(num):
            if n == name:
                return i
        raise KeyError(name)

    def content(self, num, data=None):
        """data 为 {offset: bytes} 的延迟加载区；未提供时从 self.raw 读取。"""
        info = self.file_info(num)
        if info["mode"] == "LNK":
            return info["link"].encode()
        parts = []
        for o, n in info["blocks"]:
            if o + n <= len(self.raw):
                parts.append(self.raw[o:o + n])
        return b"".join(parts)[:info["size"]]


# --------------------------------------------------------------------------
# 校验
# --------------------------------------------------------------------------
def verify(path, deep=False, quiet=False):
    r = {"image": os.path.abspath(path), "checks": [], "warnings": [], "errors": []}

    def check(name, ok, detail=""):
        r["checks"].append({"name": name, "ok": bool(ok), "detail": detail})
        if not ok:
            r["errors"].append(f"{name}: {detail}")
        return ok

    def warn(name, detail):
        r["warnings"].append(f"{name}: {detail}")

    rd = ImageReader(path, progress=not quiet)
    head = rd.prefix(256 * 1024 * 1024)

    gpt = parse_gpt(head)
    r["gpt"] = gpt
    check("GPT 分区表存在", gpt["present"], "LBA1 未发现 'EFI PART'")
    if gpt["present"]:
        p1 = gpt["partitions"][0] if gpt["partitions"] else None
        r["partition1"] = p1
        check("首个分区从 16MiB(LBA 32768) 开始",
              bool(p1 and p1["start_lba"] == 32768),
              f"实际 start_lba={p1['start_lba'] if p1 else 'N/A'}")

    idb = head[LBA_IDBLOADER * SECTOR:LBA_IDBLOADER * SECTOR + 16]
    check("sector 64 存在 RKNS idbloader", idb[:4] == RKNS_MAGIC,
          f"magic={idb[:4]!r}")
    itb = head[LBA_UBOOT_ITB * SECTOR:LBA_UBOOT_ITB * SECTOR + 16]
    check("sector 16384 存在 u-boot.itb (FIT)", itb[:4] == FDT_MAGIC,
          f"magic={itb[:4].hex(' ') if itb else 'empty'}")
    check("bootloader 区未侵入第一个分区",
          LBA_UBOOT_ITB * SECTOR < BOOT_REGION,
          "u-boot.itb 起始 8MiB < 16MiB 分区起点")

    # U-Boot 内嵌默认环境：确认 SD(mmc1) 在 eMMC(mmc0) 之前被扫描
    bt = head.find(b"boot_targets=")
    boot_targets = ""
    if bt >= 0:
        boot_targets = head[bt:head.find(b"\x00", bt)].decode("ascii", "replace")
    r["boot_targets"] = boot_targets
    check("U-Boot 启动顺序 SD(mmc1) 优先于 eMMC(mmc0)",
          "mmc1" in boot_targets and "mmc0" in boot_targets
          and boot_targets.index("mmc1") < boot_targets.index("mmc0"),
          f"boot_targets='{boot_targets}'")

    part_off = 32768 * SECTOR
    try:
        fs = Ext4(head, part_off)
    except ValueError as e:
        check("rootfs ext4 可解析", False, str(e))
        rd.close()
        return r
    check("rootfs ext4 可解析", True, f"blocksize={fs.bs} label={fs.label} uuid={fs.uuid}")
    r["ext4_uuid"] = fs.uuid

    try:
        bootino = fs.child(2, "boot")
        entries = dict((n, i) for i, n in fs.listdir(bootino))
    except Exception as e:
        check("/boot 目录可读", False, str(e))
        rd.close()
        return r
    r["boot_files"] = sorted(entries)
    check("/boot 目录可读", True, f"{len(entries)} 项")
    for f in ("Image", "uInitrd", "dtb", "boot.scr", "armbianEnv.txt", "boot.cmd"):
        check(f"/boot/{f} 存在", f in entries)

    # dtb 目录清单（早期块内可读）
    dtb_dirs = [n for n in entries if n.startswith("dtb-")]
    r["dtb_dirs"] = dtb_dirs
    dtb_names = []
    try:
        for d in dtb_dirs:
            rk = fs.child(fs.child(bootino, d), "rockchip")
            dtb_names = [n for _, n in fs.listdir(rk)]
            break
    except Exception as e:
        warn("DTB 目录", str(e))
    r["dtb_count"] = len(dtb_names)
    r["evb7_dtbs"] = sorted(n for n in dtb_names if "rk3588-evb7" in n)

    # armbianEnv.txt 与 boot.cmd 位于镜像高偏移，deep 模式再取
    env_ino = entries.get("armbianEnv.txt")
    cmd_ino = entries.get("boot.cmd")
    scr_ino = entries.get("boot.scr")
    r["deep"] = deep
    if deep:
        ranges = []
        for label, ino in (("env", env_ino), ("cmd", cmd_ino), ("scr", scr_ino)):
            if not ino:
                continue
            info = fs.file_info(ino)
            for off, size in info["blocks"]:
                ranges.append((f"{label}:{off}", off, min(size, 65536)))
        if ranges:
            got = rd.stream_ranges(ranges)
            def assemble(label, ino):
                info = fs.file_info(ino)
                return b"".join(got.get(f"{label}:{o}", b"")
                                for o, _ in info["blocks"])[:info["size"]]
            env = assemble("env", env_ino).decode("utf8", "replace") if env_ino else ""
            cmd = assemble("cmd", cmd_ino).decode("utf8", "replace") if cmd_ino else ""
            r["armbianEnv.txt"] = env
            r["boot.cmd.head"] = cmd[:600]
            vals = dict(l.split("=", 1) for l in env.splitlines()
                        if "=" in l and not l.startswith("#"))
            r["env_kv"] = vals
            fdt = vals.get("fdtfile", "")
            rel = fdt.split("rockchip/", 1)[-1] if fdt.startswith("rockchip/") else fdt
            r["fdtfile"] = fdt
            check("fdtfile 指向的 DTB 真实存在", bool(rel) and rel in dtb_names,
                  f"fdtfile='{fdt}'，目录内共 {len(dtb_names)} 个 dtb；"
                  f"可用 evb7 变体: {r['evb7_dtbs']}")
            check("rootdev 使用 UUID（不依赖 mmcblk 编号）",
                  vals.get("rootdev", "").startswith("UUID="),
                  f"rootdev={vals.get('rootdev')}")
            check("rootdev UUID 与 ext4 文件系统 UUID 一致",
                  vals.get("rootdev", "").upper() == ("UUID=" + fs.uuid).upper(),
                  f"ext4={fs.uuid} / armbianEnv={vals.get('rootdev')}")
            check("串口 console 为 ttyS2,1500000",
                  "ttyS2,1500000" in cmd, "boot.cmd 未设置 RK3588 UART2 控制台")
            check("boot.cmd 以 booti 启动 arm64 内核", "booti " in cmd)
            check("boot.cmd 从 U-Boot 当前设备加载内核（不写死 eMMC）",
                  "${devtype} ${devnum}:${distro_bootpart}" in cmd,
                  "启动脚本写死了存储设备，SD 卡启动会失败")
            check("fdtfile 使用 Linux 版 DTB（非 Android 版）",
                  "-linux.dtb" in fdt,
                  f"fdtfile={fdt}，Android 版 DTB 含 Android bootargs")
            # GPT partition GUID 用于 root=PARTUUID= 备用方案
            if gpt["present"] and gpt["partitions"]:
                ent = head[2 * SECTOR:2 * SECTOR + 128]
                r["partition_uuid"] = parse_uuid_from_guid(ent[16:32])
    # extlinux 双保险（可选，缺失仅告警）
    try:
        fs.child(bootino, "extlinux")
        r["has_extlinux"] = True
    except KeyError:
        r["has_extlinux"] = False
        warn("缺少 /boot/extlinux/extlinux.conf",
             "建议补上，U-Boot distro boot 在 SD 上多一条命中路径")
    if not deep:
        warn("深度检查未执行", "使用 --deep 校验 fdtfile / rootdev / console")

    rd.close()
    r["summary"] = {
        "total": len(r["checks"]),
        "passed": sum(1 for c in r["checks"] if c["ok"]),
        "failed": len(r["errors"]),
        "warnings": len(r["warnings"]),
    }
    return r


# --------------------------------------------------------------------------
# 资产提取
# --------------------------------------------------------------------------
def extract(path, outdir):
    rd = ImageReader(path)
    # 目录项 / inode 表可能位于分区前 256MiB 内，需一次多读一些
    head = rd.prefix(256 * 1024 * 1024)
    os.makedirs(os.path.join(outdir, "bootloader"), exist_ok=True)
    os.makedirs(os.path.join(outdir, "dtb"), exist_ok=True)
    os.makedirs(os.path.join(outdir, "boot"), exist_ok=True)
    made = []

    def last_nonzero(buf, start, end):
        p = end
        while p > start and buf[p - 1] == 0:
            p -= 1
        return p

    # idbloader: sector 64 开始，到第一个全 0 块结束（向上取整到 4KiB）
    s = LBA_IDBLOADER * SECTOR
    e = last_nonzero(head, s, LBA_UBOOT_ITB * SECTOR)
    e = ((e + 4095) // 4096) * 4096
    p = os.path.join(outdir, "bootloader", "idbloader.img")
    open(p, "wb").write(head[s:e]); made.append((p, e - s))

    # u-boot.itb: sector 16384 开始，到 16MiB 分区起点前
    s2 = LBA_UBOOT_ITB * SECTOR
    e2 = last_nonzero(head, s2, BOOT_REGION)
    e2 = ((e2 + 4095) // 4096) * 4096
    p = os.path.join(outdir, "bootloader", "u-boot.itb")
    open(p, "wb").write(head[s2:e2]); made.append((p, e2 - s2))

    fs = Ext4(head, 32768 * SECTOR)
    bootino = fs.child(2, "boot")
    entries = dict((n, i) for i, n in fs.listdir(bootino))
    d = next(n for n in entries if n.startswith("dtb-"))
    rk = fs.child(fs.child(bootino, d), "rockchip")

    targets, ranges = [], []
    for num, name in fs.listdir(rk):
        if "rk3588-evb7" in name and name.endswith(".dtb"):
            info = fs.file_info(num)
            o, n = info["blocks"][0]
            targets.append(("dtb:" + name, name, info["size"]))
            ranges.append(("dtb:" + name, o, n))
    for label, ino, name in (("cfg:armbianEnv.txt", entries.get("armbianEnv.txt"), "armbianEnv.txt"),
                             ("cfg:boot.cmd", entries.get("boot.cmd"), "boot.cmd"),
                             ("cfg:boot.scr", entries.get("boot.scr"), "boot.scr")):
        if not ino:
            continue
        info = fs.file_info(ino)
        o, n = info["blocks"][0]
        targets.append((label, name, info["size"]))
        ranges.append((label, o, n))

    got = rd.stream_ranges(ranges)
    for label, name, size in targets:
        kind = "dtb" if label.startswith("dtb:") else "boot"
        p = os.path.join(outdir, kind, name)
        open(p, "wb").write(got[label][:size])
        made.append((p, size))
    rd.close()
    return made


# --------------------------------------------------------------------------
# 流式就地修补 armbianEnv.txt（无需 loop mount / 无需改 ext4 元数据）
# --------------------------------------------------------------------------
def patch_env(path, out, fdtfile, env_file=None, preset=0):
    """把 /boot/armbianEnv.txt 的 fdtfile 改成真实存在的 DTB，等长覆写。

    关键点：新内容与原文件【字节数完全相同】，因此 ext4 的 i_size / 校验和
    都不用动，只需在解压流里替换该文件所在的 4 KiB 数据块前 N 字节，
    再重新压缩成 .xz 即可。Windows 上也能产出成品镜像。
    """
    rd = ImageReader(path, progress=False)
    head = rd.prefix(256 * 1024 * 1024)
    rd.close()
    fs = Ext4(head, 32768 * SECTOR)
    bootino = fs.child(2, "boot")
    entries = dict((n, i) for i, n in fs.listdir(bootino))
    info = fs.file_info(entries["armbianEnv.txt"])
    off, size = info["blocks"][0]
    assert size >= len(info["blocks"]) * 4096, "配置文件跨块，本工具暂不支持"
    assert off % 4096 == 0, "配置文件不是块对齐"

    if env_file:
        original = open(env_file, "rb").read()
    else:
        # 需要从高偏移再流式取一次
        got = ImageReader(path, progress=True).stream_ranges([("env", off, 4096)])
        original = got["env"][:info["size"]]
    assert len(original) == info["size"], "读取的 armbianEnv.txt 长度与 inode 不符"

    lines = original.decode("utf8").splitlines(keepends=True)
    out_lines, dropped = [], False
    for ln in lines:
        if ln.startswith("fdtfile="):
            out_lines.append(f"fdtfile=rockchip/{fdtfile}\n")
        elif ln.startswith("bootlogo=") and not dropped:
            # 删掉这一行，恰好抵消 fdtfile 变长的 14 字节，保持总长不变
            dropped = True
            continue
        else:
            out_lines.append(ln)
    new = "".join(out_lines).encode("utf8")
    diff = len(original) - len(new)
    if diff > 0:
        new += (b"#" + b"x" * (diff - 2) + b"\n")[:diff] if diff >= 2 else b" " * diff
    elif diff < 0:
        raise ValueError("新配置比原文件长，无法等长覆写；请改用 01-fix-sd-boot.sh")
    assert len(new) == len(original), (len(new), len(original))
    assert not any(b"@" in l for l in new.splitlines())

    CHUNK = 8 * 1024 * 1024
    assert off // CHUNK == (off + len(new) - 1) // CHUNK, "补丁跨越 chunk 边界，请调整 CHUNK"

    def opener(p, mode):
        if p.lower().endswith(".xz"):
            if "w" in mode:
                return lzma.open(p, "wb", format=lzma.FORMAT_XZ, preset=preset)
            return lzma.open(p, "rb")
        return open(p, mode)

    fin, fout, pos, patched = opener(path, "rb"), opener(out, "wb"), 0, False
    try:
        while True:
            buf = fin.read(CHUNK)
            if not buf:
                break
            if pos <= off and off + len(new) <= pos + len(buf):
                i = off - pos
                buf = buf[:i] + new + buf[i + len(new):]
                patched = True
            fout.write(buf)
            pos += len(buf)
            if pos % (1024 ** 3) < CHUNK:
                print(f"  ... {pos / 1024 ** 3:.1f} GiB", file=sys.stderr, flush=True)
    finally:
        fin.close()
        fout.close()
    if not patched:
        raise RuntimeError("未命中补丁区间")
    return {"offset": off, "size": len(new), "before": original.decode(),
            "after": new.decode(), "out": out, "bytes": pos}


# --------------------------------------------------------------------------
def print_report(r, as_json=False):
    if as_json:
        print(json.dumps(r, ensure_ascii=False, indent=2))
        return
    print("=" * 78)
    print("RK3588 EVB7 LP4 V10 — Armbian TF 卡镜像验证报告")
    print("=" * 78)
    print("镜像:", r["image"])
    for c in r["checks"]:
        print(f"  [{'PASS' if c['ok'] else 'FAIL'}] {c['name']}"
              + (f"  -> {c['detail']}" if not c["ok"] and c["detail"] else ""))
    print("-" * 78)
    if r.get("gpt", {}).get("partitions"):
        for p in r["gpt"]["partitions"]:
            print(f"  GPT p{p['index']}: {p['name']}  "
                  f"LBA {p['start_lba']}..{p['end_lba']}  {p['size_mib']} MiB")
    if r.get("fdtfile") is not None:
        print("  fdtfile      :", r["fdtfile"])
        print("  partition UUID:", r.get("partition_uuid"))
        print("  evb7 dtb 变体:", ", ".join(r["evb7_dtbs"]))
    for w in r["warnings"]:
        print("  [WARN]", w)
    for e in r["errors"]:
        print("  [ERROR]", e)
    s = r["summary"]
    print("-" * 78)
    print(f"  合计 {s['total']} 项：通过 {s['passed']}，失败 {s['failed']}，警告 {s['warnings']}")
    print("=" * 78)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    v = sub.add_parser("verify", help="校验镜像启动结构")
    v.add_argument("image")
    v.add_argument("--deep", action="store_true", help="深度校验 /boot 配置（.xz 约需 1 分钟）")
    v.add_argument("--json", action="store_true")
    v.add_argument("--quiet", action="store_true")
    e = sub.add_parser("extract", help="提取 bootloader / dtb / boot 配置")
    e.add_argument("image")
    e.add_argument("--out", required=True)
    p = sub.add_parser("patch-env", help="等长流式修补 fdtfile，直接产出成品镜像（无需 mount）")
    p.add_argument("image")
    p.add_argument("out")
    p.add_argument("--fdtfile", default="rk3588-evb7-lp4-v10-linux.dtb")
    p.add_argument("--env-file", default=None,
                   help="原始 armbianEnv.txt（默认从镜像高偏移再流式读一次）")
    p.add_argument("--preset", type=int, default=0, help="xz 压缩级别 0..9，默认 0 最快")
    args = ap.parse_args()

    if not os.path.isfile(args.image):
        print("文件不存在:", args.image, file=sys.stderr)
        return 2
    if args.cmd == "verify":
        r = verify(args.image, deep=args.deep, quiet=args.quiet)
        print_report(r, args.json)
        return 1 if r["summary"]["failed"] else 0
    if args.cmd == "extract":
        made = extract(args.image, args.out)
        for p, n in made:
            print(f"  {p}  ({n} bytes)")
        print(f"共提取 {len(made)} 个文件 -> {args.out}")
        return 0
    if args.cmd == "patch-env":
        r = patch_env(args.image, args.out, args.fdtfile,
                      env_file=args.env_file, preset=args.preset)
        print("补丁偏移:", hex(r["offset"]), f"({r['size']} bytes, 等长)")
        print("--- before ---"); print(r["before"], end="")
        print("--- after  ---"); print(r["after"], end="")
        print("--- output ---")
        print(f"  {r['out']}  ({os.path.getsize(r['out'])} bytes, 解压后 {r['bytes']} bytes)")
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
