# Armbian for RK3588 EVB7 LP4 V10 — GitHub Actions 云编译

为 **Rockchip RK3588 EVB7 LP4 V10**（原厂 Android 14 白板整机）构建可从 TF/microSD 卡启动的
Armbian。所有编译都在云端完成，本地 Windows 不需要装 Linux。

硬件：RK3588 / 16 GiB LPDDR4X @2112MHz / eMMC 256 GB / 千兆网 / HDMI1 4K60 /
AP6398S WiFi / 调试串口 UART2 **1500000 8N1**。

---

## 一、三个 workflow，别选错

| Workflow | 耗时 | 产出 | 什么时候用 |
|---|---|---|---|
| **Patch SD-boot firmware from Release** | 10–20 分钟 | 修好 `fdtfile` 的完整 `.img.xz` | **日常首选**。不重编译，基于已有 Release 修补出包 |
| **Build mainline U-Boot (RK3588 EVB7)** | 5–10 分钟 | `u-boot-rockchip.bin`（几 MiB） | **必须做一次**。修 OP-TEE 阻塞，产出的 bin 写入 TF 卡 sector 64 |
| **Compile Armbian for RK3588 EVB7** | 1–8 小时 | 全新 `.img.xz` | 只在要换内核 / 换 rootfs / 改桌面时 |

### 怎么触发

每个 workflow 都支持两种方式：

1. **网页点按钮（推荐）**：`Actions` → 选 workflow → `Run workflow` → 选 `main` → 填参数 → 再点一次。
2. **改 trigger 文件自动触发**：
   - 编辑 `trigger-mainline-uboot.txt` 并提交 → 自动跑 **Build mainline U-Boot**。
   - 新建/编辑 `trigger-full-build.txt` 并提交 → 自动跑 **Compile Armbian**（8 小时，谨慎）。

> 改 `config/`、`patch/`、`README`、文档等**不会**触发 8 小时编译，避免误烧额度。
> `workflow_dispatch` 与 push 触发共用同一套默认参数（表达式里写了 `inputs.X || '默认值'`）。

---

## 二、为什么需要两个产物（重要）

这块板的 TF 卡启动有两个独立的坑，**必须都解决**：

### 坑 1：`fdtfile` 指向不存在的 DTB（`boot.scr` 阶段）

`config/boards/rk3588-evb7.csc` 原来写的是
`BOOT_FDT_FILE="rockchip/rk3588-evb7.dtb"`，但内核包里根本没有这个文件，
`boot.scr` 的 `load ... ${prefix}dtb/${fdtfile}` 会失败。

正确值：**`rk3588-evb7-lp4-v10-linux.dtb`**（V10 + LPDDR4 + Linux 版）。
本仓库的 csc 已修正；`Patch SD-boot...` 和 `Compile Armbian` 两个 workflow 还会在
编译/打包后再次强制校正并补同名软链，双保险。

### 坑 2：U-Boot 的 OP-TEE 检查（`boot.scr` 之前，更早）

2026-11-10 实机串口日志：

```
WARNING: No OPTEE provided by BL2 boot loader
ERROR:   Error initializing runtime service opteed_fast
optee check api revision fail: -1.0
optee api revision is too low
### ERROR ### Please RESET the board ###
```

原因：Armbian vendor U-Boot 的 `board_late_init()` 强制检查 OP-TEE，
但它自己的 `u-boot.itb` 里 `/images` 只有 `uboot / atf-1 / atf-2 / atf-3 / fdt`，
**没有 BL32/OP-TEE**。这是 armbian/build 的已知问题
（[PR #6696](https://github.com/armbian/build/pull/6696)）：
「原厂 eMMC blobs + Armbian SD 卡」的组合会触发，官方解法是禁用 U-Boot 的 OPTEE
或换主线 U-Boot。

**解法**：跑 `Build mainline U-Boot`，把产物 `u-boot-rockchip.bin` 写入 TF 卡 sector 64：

```bash
# Linux
sudo dd if=u-boot-rockchip.bin of=/dev/sdX bs=512 seek=64 conv=fsync,notrunc
```

Windows 见 `windows/README.md`（`patch-bootloader.py` 注入完整镜像 + Etcher，
或 `Write-UbootToSD.ps1` 直写物理盘）。

> 主线 U-Boot 没有 vendor OP-TEE 检查，且带标准 distro boot，
> 会扫 `/boot/extlinux/extlinux.conf` 与 `/boot/boot.scr`。
> `Patch SD-boot...` 产物已内置三份 extlinux 兜底。

---

## 三、推荐完整流程

```
① Actions -> Build mainline U-Boot -> Run workflow
     ↓ Artifact: u-boot-rockchip.bin
② Actions -> Patch SD-boot firmware from Release -> Run workflow
     ↓ Artifact/Release: Armbian_..._sd-boot_fixed.img.xz
③ Windows: python windows/patch-bootloader.py <fixed.img.xz> u-boot-rockchip.bin <final.img.xz>
   或：先 Etcher 烧 fixed.img.xz，再用 Write-UbootToSD.ps1 直写 sector 64
④ balenaEtcher 把 final.img.xz 写入 TF 卡（Rufus 必须选 DD 模式）
⑤ 通电，串口 1500000 8N1
```

> 写完卡 Windows 若提示「需要初始化/格式化磁盘」——**一律点取消**。
> Rufus 非 DD 模式或 Windows 初始化会改写 GPT 分区项，SPL 会报
> `GUID Partition Table Entry Array CRC is wrong`（回退 Backup GPT 后仍可能启动，但别留着）。

**原厂 Android 完全不动**：SPL 日志实测 `Trying to boot from MMC2`（TF 卡）先于
`MMC1`（eMMC），U-Boot `boot_targets=usb0 mmc1 mmc0 ...` 也是卡优先，
不需要擦 eMMC；拔掉 TF 卡重启即回 Android。

---

## 四、验收清单

```
□ SPL: Trying to boot from MMC2，无 "no card present"
□ 无 "GUID Partition Table Entry Array CRC is wrong"
□ 出现主线 U-Boot 版本号（不再是 2017.09_armbian-...-S39cd）
□ 无 "optee api revision is too low" / "Please RESET the board"
□ "Found U-Boot script /boot/boot.scr" 或 "Boot script loaded from mmc 1:1"
□ "## Loading init Ramdisk" -> 内核解压 -> login:
□ 拔卡重启 -> 原厂 Android 14 正常
```

若主线 SPL 报 `MMC: no card present`，说明 `evb-rk3588_defconfig` 的 EVB1 DTS
与本板 SD 卡检测脚（GPIO0_A4）不匹配，需改用「重编 vendor U-Boot 并删掉
`CONFIG_OPTEE_CLIENT` / `CONFIG_OPTEE_V2`」的方案。

---

## 五、权限与 Secret

```
□ Settings -> Actions -> General -> Workflow permissions = Read and write
□ 不需要任何自定义 Repository Secret，GITHUB_TOKEN 自动注入
□ Release 单个 asset < 2 GiB（v8 是 1.90 GB，OK）
□ 仓库 .gitignore 已忽略 output/，2 GB 级镜像不要 git add，走 Release/Artifact
```

---

## 六、仓库结构

```
.github/workflows/
  build-mainline-uboot.yml    主线 U-Boot 云编译（5–10 分钟）
  patch-from-release.yml      基于 Release 修 fdtfile，分钟级出包
  compile-armbian.yml         完整重编译（1–8 小时，手动触发）
config/boards/rk3588-evb7.csc 板级配置（BOOT_FDT_FILE 已修正）
patch/kernel/rk35xx-vendor-6.1/rk3588-evb7.dts   从原厂 Android 提取的真实 DTB 源
scripts/setup.sh             本地环境准备
trigger-mainline-uboot.txt   改它 -> 自动跑主线 U-Boot 编译
windows/                     Windows 端写卡工具（patch-bootloader.py / Write-UbootToSD.ps1）
docs/                        硬件分析、实机诊断、验证报告
```

当前修正后的 `compile-armbian.yml` 相对旧版：
`ubuntu-22.04 → ubuntu-24.04`（22.04 runner 2026-09 起弃用）、
编译后强制修 fdtfile、加自包含启动结构校验、`armbian/build` 可 pin ref、
push 触发改为显式 trigger 文件。

---

## 七、相关

- 板级配置必须用 **vendor 6.1 BSP**：本板 DTB 是 Rockchip BSP 绑定
  （`rockchip,rk806` / `rockchip,rk3588-gmac` / `rockchip,rk3588-dw-mshc` / `wlan-platdata`），
  ophub 的 rk3588 主线内核包不兼容。
- 参考项目：[ophub/amlogic-s9xxx-armbian](https://github.com/ophub/amlogic-s9xxx-armbian)
  （借用了其 GPT 16MiB 偏移、bootloader `dd seek=64/16384`、板级数据库与出包组织方式）。
- MIT License.
