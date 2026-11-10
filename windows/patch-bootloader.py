#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
patch-bootloader.py — 在 Windows / Linux 上把主线 U-Boot 注入 Armbian 镜像的 sector 64

不需要 mount、不需要管理员、不需要 Linux。纯 Python 流式处理，
只替换镜像前 16 MiB 里的 bootloader 区，其余 10.7 GiB 原样重压缩。

用法（Windows PowerShell）：
  python patch-bootloader.py `
      ..\\output\\Armbian_rk3588-evb7_sd-boot.img.xz `
      .\\u-boot-rockchip.bin `
      .\\Armbian_rk3588-evb7_sd-boot_mainline-uboot.img.xz

  python patch-bootloader.py <base.img[.xz]> <u-boot-rockchip.bin> <out.img[.xz]> [--preset 1]

参数：
  --preset   xz 压缩级别 0..9，默认 1（本地写卡用，最快）。
             若要上传 GitHub Release（单文件 < 2 GiB）用 --preset 6 或更高。
  --verify   完成后重新读取输出，校验 sector 64 魔数与 GPT。

产物用 balenaEtcher / Rufus(DD 模式) 整盘写入 TF 卡即可，
会同时把之前被 Rufus 改坏的 GPT 分区表恢复成干净状态。
"""

import argparse
import hashlib
import lzma
import os
import struct
import sys
import time

SECTOR = 512
OFFSET = 64 * SECTOR          # 0x8000，Rockchip BootROM 找 idbloader 的位置
ROOTFS_OFFSET = 16 * 1024 * 1024   # 0x1000000，rootfs 分区起点
CHUNK = 8 * 1024 * 1024


def opener(path, mode):
    if path.lower().endswith(".xz"):
        if "w" in mode:
            return lzma.open(path, "wb", format=lzma.FORMAT_XZ,
                             preset=ARGS.preset)
        return lzma.open(path, "rb")
    return open(path, mode)


def file_size(path):
    if path.lower().endswith(".xz"):
        return None
    return os.path.getsize(path)


def crc32(data):
    return zlib_crc32(data)


def zlib_crc32(data):
    import zlib
    return zlib.crc32(data) & 0xFFFFFFFF


def main():
    global ARGS
    ap = argparse.ArgumentParser(
        description="把主线 u-boot-rockchip.bin 注入 Armbian 镜像 sector 64")
    ap.add_argument("base", help="基础镜像 .img 或 .img.xz")
    ap.add_argument("uboot", help="主线 U-Boot 产物 u-boot-rockchip.bin")
    ap.add_argument("out", help="输出镜像 .img 或 .img.xz")
    ap.add_argument("--preset", type=int, default=1,
                    help="xz 压缩级别 0..9（默认 1 最快；进 Release 用 6+）")
    ap.add_argument("--verify", action="store_true", help="完成后读回校验")
    ARGS = ap.parse_args()

    for p in (ARGS.base, ARGS.uboot):
        if not os.path.isfile(p):
            sys.exit("文件不存在: " + p)

    blob = open(ARGS.uboot, "rb").read()
    if blob[:4] != b"RKNS":
        sys.exit(f"错误: {ARGS.uboot} 头部不是 RKNS，实际 {blob[:4]!r}。"
                 f"\n请确认拿的是 u-boot-rockchip.bin（binman 打包），不是 u-boot.itb / u-boot.bin。")
    if OFFSET + len(blob) > ROOTFS_OFFSET:
        sys.exit(f"错误: U-Boot 镜像 {len(blob)} B 太大，会覆盖 rootfs 分区"
                 f"（可用空间 {ROOTFS_OFFSET - OFFSET} B）")
    # binman 的 u-boot-rockchip.bin 应覆盖到 sector 16384，才会替换掉旧的、缺 BL32 的 u-boot.itb
    itb_offset = 16384 * SECTOR
    if OFFSET + len(blob) < itb_offset:
        print(f"  [WARN] bin 只有 {len(blob)} B，未覆盖到 sector 16384；"
              f"旧的 u-boot.itb 会残留在卡上。\n"
              f"         主线 SPL 从 binman 内部偏移加载，通常无害，但建议确认构建产物完整。")
    print(f"  U-Boot bin : {ARGS.uboot}  ({len(blob)} B, magic=RKNS)")
    print(f"  替换区间   : offset {hex(OFFSET)} .. {hex(OFFSET + len(blob))} "
          f"(sector 64 .. {64 + (len(blob) + SECTOR - 1) // SECTOR})")
    print(f"  rootfs 起点: {hex(ROOTFS_OFFSET)}  → 未被触碰")

    total = file_size(ARGS.base)
    fin, fout = opener(ARGS.base, "rb"), opener(ARGS.out, "wb")
    pos, replaced, t0 = 0, 0, time.time()
    end = OFFSET + len(blob)
    try:
        while True:
            buf = fin.read(CHUNK)
            if not buf:
                break
            n = len(buf)
            lo, hi = pos, pos + n
            if lo < end and hi > OFFSET:
                a = max(OFFSET, lo)
                b = min(end, hi)
                src = blob[a - OFFSET:b - OFFSET]
                i, j = a - lo, b - lo
                buf = buf[:i] + src + buf[j:]
                replaced += len(src)
            fout.write(buf)
            pos += n
            if pos % (1024 ** 3) < CHUNK:
                print(f"  ... {pos / 1024 ** 3:.1f} GiB  ({time.time() - t0:.0f}s)",
                      file=sys.stderr, flush=True)
    finally:
        fin.close()
        fout.close()

    if replaced != len(blob):
        sys.exit(f"错误: 只写入了 {replaced}/{len(blob)} 字节，镜像可能比预期短")
    print(f"\n  完成: {ARGS.out}  ({os.path.getsize(ARGS.out)} B, 解压后 {pos} B, "
          f"耗时 {time.time() - t0:.0f}s)")

    h = hashlib.sha256()
    with open(ARGS.out, "rb") as f:
        while True:
            d = f.read(8 * 1024 * 1024)
            if not d:
                break
            h.update(d)
    print("  SHA256:", h.hexdigest())
    with open(ARGS.out + ".sha256", "w", encoding="ascii") as f:
        f.write(h.hexdigest() + "  " + os.path.basename(ARGS.out) + "\n")

    if ARGS.verify:
        print("\n  --verify 读回校验")
        with opener(ARGS.out, "rb") as f:
            head = f.read(ROOTFS_OFFSET)
        magic = head[OFFSET:OFFSET + 4]
        gpt = head[SECTOR:SECTOR + 8]
        ent_lba = struct.unpack_from("<Q", head, SECTOR + 0x48)[0]
        num, esz = struct.unpack_from("<II", head, SECTOR + 0x50)
        stored = struct.unpack_from("<I", head, SECTOR + 0x58)[0]
        arr = head[ent_lba * SECTOR:ent_lba * SECTOR + num * esz]
        calc = crc32(arr)
        hcrc = bytearray(head[SECTOR:SECTOR + struct.unpack_from(
            "<I", head, SECTOR + 0x0C)[0]])
        hcrc[0x10:0x14] = b"\0\0\0\0"
        hcalc = crc32(bytes(hcrc))
        print(f"    sector 64 magic      : {magic!r}  "
              f"{'OK' if magic == b'RKNS' else 'FAIL'}")
        print(f"    GPT signature        : {gpt!r}  "
              f"{'OK' if gpt == b'EFI PART' else 'FAIL'}")
        print(f"    分区项 CRC           : header=0x{stored:08x} calc=0x{calc:08x}  "
              f"{'OK' if stored == calc else 'FAIL'}")
        hdr_stored = struct.unpack_from("<I", head, SECTOR + 0x10)[0]
        print(f"    GPT header CRC       : header=0x{hdr_stored:08x} calc=0x{hcalc:08x}  "
              f"{'OK' if hdr_stored == hcalc else 'FAIL'}")
        if magic != b"RKNS" or gpt != b"EFI PART" or stored != calc:
            sys.exit("校验失败，请不要写卡")

    print("\n>>> 下一步：用 balenaEtcher（或 Rufus 的 DD 模式）把输出镜像整盘写入 TF 卡。")
    print("    写完立刻安全弹出；Windows 若提示『需要初始化/格式化磁盘』一律点取消。")


if __name__ == "__main__":
    main()
