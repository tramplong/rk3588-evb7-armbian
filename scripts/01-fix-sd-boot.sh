#!/usr/bin/env bash
# =============================================================================
# 01-fix-sd-boot.sh
#   参照 ophub/amlogic-s9xxx-armbian 的 Rockchip 镜像规范，把已有的
#   Armbian vendor 6.1.115 镜像修成「插 TF 卡即可启动」的固件。
#   全部补丁由 tools/patch_boot_chain.py 实现（纯 Python 标准库、幂等）。
#   2026-11-12 实机验证：Armbian 26.08 noble 从 mmcblk1p1 起到 systemd，
#   eMMC 上的 Android 完全未动。完整论证见 docs/10 §9..§16。
#
#   修复内容：
#     1. fdtfile  -> rk3588-evb7-lp4-v10-linux.dtb（原值指向不存在的文件）
#     2. 补同名软链 rk3588-evb7.dtb（双保险）
#     3. 补 /boot/extlinux/extlinux.conf + mmc0_extlinux/ + mmc1_extlinux/ + uEnv.txt
#     4. 写 /etc/ophub-release 板级标识
#     5. armbianEnv.txt（最终 4 行）：root=/dev/mmcblk1p1 + rootdelay=10 +
#        console=ttyFIQ0 + verbosity=7。uInitrd 无 udev，PARTUUID 必然解析失败。
#     6. boot.scr：83 字节等长替换，把条件 earlycon 改成显式
#        earlycon=uart8250,mmio32,0xfeb50000,1500000，并重算 ih_dcrc/ih_hcrc。
#     7. 板级 DTB：/pcie@fe150000 /pcie@fe170000 的 status okay->fail（8 字节），
#        绕开 rk-pcie 在 do_initcalls() 里的 mutex 死锁。
#     8. sector 16384（0x800000）写 vendor FIT：3 处 OP-TEE 中止点补丁，
#        bootdelay 保持 0，重算 /images/uboot sha256，补零到 2 MiB。
#        sector 64 仍写 idbloader.img（BootROM 实际只从 eMMC 取 SPL，
#        这里写卡只是保持镜像结构完整 / 兼容 ophub 规范）。
#     9. 全量 verify（含 verify-img 对 FIT 区 5/5 hash 的复核）
#
#   用法：
#     sudo ./scripts/01-fix-sd-boot.sh <input.img|input.img.xz> [output.img]
#     sudo ./scripts/01-fix-sd-boot.sh ... --compress        # 额外生成 .img.xz
#     sudo ./scripts/01-fix-sd-boot.sh ... --skip-verify     # 跳过最终校验（不推荐）
#
#   环境：Ubuntu 22.04/24.04 x86_64，root，需 loop 设备，约 25 GB 空闲空间
# =============================================================================
set -Eeuo pipefail

KIT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
IN="${1:-}"
OUT="${2:-}"
COMPRESS=0
SKIP_VERIFY=0
for a in "$@"; do
    case "$a" in
        --compress)    COMPRESS=1 ;;
        --skip-verify) SKIP_VERIFY=1 ;;
    esac
done
[[ "$OUT" == --* ]] && OUT=""
[[ -n "$OUT" ]] || OUT="${KIT}/output/Armbian_rk3588-evb7_sd-boot_$(date +%Y%m%d).img"

[[ -n "$IN" && -f "$IN" ]] || { echo "用法: $0 <input.img|input.img.xz> [output.img] [--compress]"; exit 2; }
[[ $EUID -eq 0 ]] || { echo "需要 root（loop 挂载）"; exit 2; }

for c in losetup blkid mount umount dd sync sed sha256sum python3 parted; do
    command -v "$c" >/dev/null || { echo "缺少命令: $c"; exit 2; }
done
if [[ "$IN" == *.xz ]]; then command -v xz >/dev/null || { echo "缺少 xz-utils"; exit 2; }; fi
if [[ $COMPRESS -eq 1 ]]; then command -v xz >/dev/null || { echo "缺少 xz-utils"; exit 2; }; fi

mkdir -p "$(dirname "$OUT")" "${KIT}/output" "${KIT}/work"
WORK="${KIT}/work/rk3588-evb7-build.img"
MNT="$(mktemp -d "${KIT}/work/mnt.XXXXXX")"
LOOP=""
cleanup() {
    set +e
    [[ -n "$LOOP" ]] && { umount "$MNT" 2>/dev/null; losetup -d "$LOOP" 2>/dev/null; }
    mountpoint -q "$MNT" && umount "$MNT"
    rmdir "$MNT" 2>/dev/null
}
trap cleanup EXIT

log() { echo -e "\n\033[1;36m>>> $*\033[0m"; }
die() { echo -e "\033[1;31mERROR: $*\033[0m" >&2; exit 1; }

# ---------------------------------------------------------------- 1. 展开镜像
log "[1/7] 展开基础镜像 -> ${WORK}"
rm -f "$WORK"
if [[ "$IN" == *.xz ]]; then
    echo "  解压中（约 10.7 GB，视磁盘速度 1–3 分钟）..."
    xz -dc "$IN" > "$WORK"
else
    cp --reflink=auto "$IN" "$WORK"
fi
[[ -s "$WORK" ]] || die "展开后的镜像为空"

# ---------------------------------------------------------------- 2. 前置校验
log "[2/7] 校验输入镜像的启动结构"
python3 "${KIT}/tools/armbian_image_tool.py" verify "$WORK" --quiet || \
    die "输入镜像不是预期的 RK3588 Armbian 单分区布局"

# ---------------------------------------------------------------- 3. 挂载 rootfs
log "[3/7] 挂载 rootfs（分区起点 16 MiB / LBA 32768）"
LOOP="$(losetup -P -f --show "$WORK")"
echo "  loop = $LOOP"
# 等分区节点出现
for i in $(seq 1 20); do [[ -b "${LOOP}p1" ]] && break; sleep 0.2; done
[[ -b "${LOOP}p1" ]] || die "未找到 ${LOOP}p1"
FS_UUID="$(blkid -s UUID -o value "${LOOP}p1")"
PART_UUID="$(blkid -s PARTUUID -o value "${LOOP}p1" || true)"
echo "  ext4 UUID      = $FS_UUID"
echo "  GPT PARTUUID   = $PART_UUID"
[[ -n "$FS_UUID" ]] || die "读不到 ext4 文件系统 UUID"
mount "${LOOP}p1" "$MNT"

# ---------------------------------------------------------------- 4. 修启动配置
log "[4/7] 修正 /boot 启动配置并打启动链补丁（armbianEnv / boot.scr / DTB）"
PBC="python3 ${KIT}/tools/patch_boot_chain.py"
ENV="${MNT}/boot/armbianEnv.txt"
[[ -f "$ENV" ]] || die "缺少 /boot/armbianEnv.txt"
cp -a "$ENV" "${ENV}.rk3588bak" 2>/dev/null || true

FDT="rk3588-evb7-lp4-v10-linux.dtb"
DTBDIR="${MNT}/boot/dtb/rockchip"
[[ -f "${DTBDIR}/${FDT}" ]] || die "找不到 ${DTBDIR}/${FDT}，请确认基础镜像内核包完整"

# 4.1 armbianEnv.txt：最终 4 行（不再有 rootdev=；uInitrd 无 udev，
#     PARTUUID/UUID 会解析失败）。@FDTFILE@ 在这里渲染。
$PBC env "${KIT}/board/bootfs/armbianEnv.txt" "$ENV" --fdtfile "$FDT" || \
    die "armbianEnv.txt 渲染失败"
echo "  最终 armbianEnv.txt:"; sed 's/^/    /' "$ENV"

# 4.2 boot.scr：显式 earlycon + 重算 legacy uImage 的两个 CRC。
#     vendor U-Boot 的 source 会校验 ih_dcrc，漏掉这步会 "Bad data crc"。
SCR="${MNT}/boot/boot.scr"
[[ -f "$SCR" ]] || die "缺少 /boot/boot.scr"
$PBC scr "$SCR" || die "boot.scr 补丁失败"

# 4.3 板级 DTB：禁用 11 个节点——2 个会死锁的 PCIe 控制器（rk-pcie 在
#     do_initcalls() 里持 mutex 不放，RCU stall 12 轮）+ 9 个板上根本没焊的
#     外设（Goodix 触摸屏 / imx415 摄像头 / hym8563 RTC / es8388 音频 codec /
#     SDIO / HDMI RX / 两个 DP）。全部是等长 5 字节替换 okay\0 -> fail\0，
#     FDT 结构一个字节都不动。HDMI 输出、TF 卡、eMMC、以太网、USB 全部保留。
#     覆盖 /boot 下所有 dtb* 目录里的 evb7 linux 变体，保证无论 fdtfile
#     指向哪个都干净。清单见 tools/patch_boot_chain.py 的 DTB_DISABLE_NODES。
DTB_PATCHED=0
for d in "${MNT}"/boot/dtb*/rockchip; do
    [[ -d "$d" ]] || continue
    for f in "$d"/rk3588-evb7*-linux.dtb; do
        [[ -f "$f" ]] || continue
        $PBC dtb "$f" || die "DTB 补丁失败: $f"
        DTB_PATCHED=$((DTB_PATCHED + 1))
    done
done
[[ $DTB_PATCHED -gt 0 ]] || die "没找到任何 evb7 linux DTB 可补丁"
echo "  已补丁 DTB 副本数: $DTB_PATCHED"

# extlinux / uEnv 仍用 @ROOTDEV@=UUID=<ext4 s_uuid> 作兜底启动路径
render() { sed -e "s|@FDTFILE@|${FDT}|g" -e "s|@ROOTDEV@|UUID=${FS_UUID}|g" "$1"; }

# 4.4 同名软链：即便某个 boot.scr 仍读到旧 fdtfile 也能解析
ln -sfn "${FDT}" "${DTBDIR}/rk3588-evb7.dtb"

# 4.5 extlinux.conf：ophub / U-Boot distro boot 的多设备兜底
EXTLINUX="${MNT}/boot/extlinux"
mkdir -p "$EXTLINUX" "${MNT}/boot/mmc0_extlinux" "${MNT}/boot/mmc1_extlinux"
render "${KIT}/board/bootfs/extlinux.conf" > "${EXTLINUX}/extlinux.conf"
# ophub 给 QuarkPi-CA2 用的同名技巧：某些厂 U-Boot 只认 mmcN_extlinux/
for d in mmc0_extlinux mmc1_extlinux; do
    sed "s|(TF/SD)|($d)|" "${EXTLINUX}/extlinux.conf" > "${MNT}/boot/${d}/extlinux.conf"
done

# 4.6 uEnv.txt（ophub 风格，部分 boot.scr 会读）
render "${KIT}/board/bootfs/uEnv.txt" > "${MNT}/boot/uEnv.txt"

# 4.7 板级标识（ophub 用 /etc/ophub-release）
mkdir -p "${MNT}/etc"
cat > "${MNT}/etc/ophub-release" <<EOF
BOARD='rk3588-evb7'
MODEL='Rockchip RK3588 EVB7 LP4 V10 Board'
SOC='rk3588'
FDTFILE='${FDT}'
PLATFORM='rockchip'
FAMILY='rk3588'
BOOT_CONF='armbianEnv.txt'
MAINLINE_UBOOT='/usr/lib/u-boot/u-boot.itb'
BOOTLOADER_IMG='/usr/lib/u-boot/idbloader.img'
KERNEL_TAGS='rk3588/6.1.y'
BUILDER='rk3588-evb7-armbian-kit'
EOF

# 4.8 把匹配本板的 bootloader 也放进 rootfs，便于日后 armbian-update / 写 eMMC
mkdir -p "${MNT}/usr/lib/u-boot"
cp -f "${KIT}/assets/bootloader/idbloader.img" "${MNT}/usr/lib/u-boot/"
cp -f "${KIT}/assets/bootloader/u-boot.itb"   "${MNT}/usr/lib/u-boot/"

sync
umount "$MNT"
losetup -d "$LOOP"; LOOP=""
# ---------------------------------------------------------------- 5. 重写 bootloader
log "[5/7] 写 bootloader：sector 64 idbloader + sector 16384 已打补丁的 vendor FIT"
IDB="${KIT}/assets/bootloader/idbloader.img"
ITB="${KIT}/assets/bootloader/u-boot.itb"     # 这个文件本身就是 vendor FIT
[[ -s "$IDB" && -s "$ITB" ]] || die "assets/bootloader 缺失，请先运行 extract"
[[ "$(head -c4 "$IDB")" == "RKNS" ]] || die "idbloader.img  magic 不是 RKNS"

# 3 处 OP-TEE 中止点补丁；bootdelay 保持 0（不额外等待 autoboot）。
# 输出补零到 2 MiB，正好覆盖 0x800000..0xA00000（在 16 MiB 分区起点之前）。
FITOUT="${KIT}/work/vendor-fit-patched.bin"
$PBC fit "$ITB" "$FITOUT" --bootdelay 0 || die "vendor FIT 补丁失败"

dd if="$IDB"    of="$WORK" bs=512 seek=64    conv=fsync,notrunc status=none
dd if="$FITOUT" of="$WORK" bs=512 seek=16384 conv=fsync,notrunc status=none
echo "  sector 64     <- idbloader.img                  ($(stat -c%s "$IDB") B)"
echo "  sector 16384  <- vendor FIT (3 OP-TEE patches)  ($(stat -c%s "$FITOUT") B)"

# ---------------------------------------------------------------- 6. 输出
log "[6/7] 输出成品"
# 用 mv 而非 cp：避免在磁盘上同时存在两份 10.7 GiB 镜像（CI 环境磁盘紧张）
mv -f "$WORK" "$OUT"
WORK=""
sync
if [[ $COMPRESS -eq 1 ]]; then
    echo "  压缩中（xz -T0 -3）..."
    xz -T0 -3 -f -k "$OUT"
fi
echo "  成品: $OUT ($(du -h "$OUT" | cut -f1))"
sha256sum "$OUT" | tee "${OUT}.sha256"

# ---------------------------------------------------------------- 7. 全量校验
log "[7/7] 成品全量校验（FIT 区 + 镜像结构）"
if [[ $SKIP_VERIFY -eq 1 ]]; then
    echo "  --skip-verify 已跳过"
else
    REPORT="${OUT%.img}_verify.txt"
    {
        echo "=== FIT region @ 0x800000 (patch_boot_chain.py verify-img) ==="
        $PBC verify-img "$OUT" --bootdelay 0
        echo
        echo "=== image structure (armbian_image_tool.py verify --deep) ==="
        python3 "${KIT}/tools/armbian_image_tool.py" verify "$OUT" --deep --quiet
    } > "$REPORT" 2>&1 || true
    cat "$REPORT"
    if grep -qE '\[FAIL\]|RESULT: FAIL|ERROR:' "$REPORT"; then
        die "成品仍有 FAIL 项，请看 ${REPORT}"
    fi
fi

log "完成 ✅"
echo "  下一步: sudo ${KIT}/scripts/03-write-sd-card.sh ${OUT}"
echo "  串口: 1500000 8N1 / UART2 (0xfeb50000) / 内核 console=ttyFIQ0（RX 常不通）"
echo "  登录: ssh root@rk3588-evb7.local（Armbian 首次 root/1234）或 HDMI + USB 键盘"
echo "  eMMC 上的 Android 不受影响；本镜像为 TF 卡启动专用"
