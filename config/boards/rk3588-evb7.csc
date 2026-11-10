# Rockchip RK3588 EVB7 LP4 V10 Board
# -----------------------------------------------------------------------------
# 修正版 / 直接覆盖仓库里的 config/boards/rk3588-evb7.csc
#
# 唯一必须改的一行是 BOOT_FDT_FILE：
#   原值  BOOT_FDT_FILE="rockchip/rk3588-evb7.dtb"        ← 文件根本不存在
#   新值  BOOT_FDT_FILE="rockchip/rk3588-evb7-lp4-v10-linux.dtb"
#
# 证据：Release v8 的镜像 Armbian-unofficial_26.08.0-trunk_Rk3588-evb7_...
#       /boot/armbianEnv.txt 里写的是 fdtfile=rockchip/rk3588-evb7.dtb，
#       而 /boot/dtb-6.1.115-vendor-rk35xx/rockchip/ 下 466 个 dtb 中
#       没有这个名字，导致 boot.scr 的 load ... dtb/${fdtfile} 失败，
#       TF 卡启动卡在 U-Boot。
# -----------------------------------------------------------------------------
BOARD_NAME="RK3588 EVB7 LP4 V10"
BOARD_VENDOR="rockchip"
BOARDFAMILY="rockchip-rk3588"
BOARD_MAINTAINER=""
INTRODUCED="2026"
BOOTCONFIG="rk3588_defconfig"
KERNEL_TARGET="vendor"
FULL_DESKTOP="yes"
BOOT_LOGO="desktop"
BOOT_FDT_FILE="rockchip/rk3588-evb7-lp4-v10-linux.dtb"
BOOT_SCENARIO="spl-blobs"
BOOT_SOC="rk3588"
IMAGE_PARTITION_TABLE="gpt"

# --- 新增：确保串口与内核命令行正确（可选但推荐）---------------------------
# 调试串口是 UART2 @0xfeb50000，1500000 8N1；ttyFIQ0 是 Android 侧的，不要用。
CONSOLE="ttyS2,1500000"
EXTRA_CMDLINE="cma=256M"
