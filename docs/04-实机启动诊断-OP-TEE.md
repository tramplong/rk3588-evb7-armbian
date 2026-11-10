# 04 · 实机启动诊断（2026-11-10 串口日志）

日志：`C:/Users/zhyl/Desktop/MobaXterm_COM3USB-SERIALCH340COM3_20261110_103834.txt`（92 行 / 3761 B）
固件：`output/Armbian_rk3588-evb7_sd-boot.img.xz`（21/21 离线校验通过的那份）

---

## 一、结论

**这次 TF 卡确实被识别并接管了启动，之前"不擦 eMMC 也能 TF 卡启动"的推断被实测证实。**
但卡在了一个更深、离线校验根本看不到的位置：**Armbian 的 vendor U-Boot 强制要求 OP-TEE，而它自己的 FIT 里没有 OP-TEE**。

```
U-Boot 2017.09_armbian-2017.09-S39cd-... (Jun 28 2026)
optee check api revision fail: -1.0
optee api revision is too low
### ERROR ### Please RESET the board ###
```

这个阻塞发生在 U-Boot `board_late_init()`，**早于 `boot.scr`**，所以上次修的 `fdtfile` 还没机会生效。
它不是补丁引入的——`patch-env` 只改了 188 字节，`前 16 MiB 与基础镜像逐字节一致`；
这是 **Release v8（2026-06-28 编译）自带的 U-Boot 打包缺陷**，外加"原厂 Android eMMC + Armbian SD 卡"这个组合触发。

---

## 二、日志逐段解读

| 阶段 | 日志 | 判定 |
|---|---|---|
| DDR | `LPDDR4X, 2112MHz` ×4CH / `fwver: v1.17` | ✅ 与板上完全一致 |
| SPL | `U-Boot SPL 2017.09-ge4e124926e-230922, fwver: v1.13` | ✅ |
| SPI NOR | `unknown raw ID 0 0 0` / `unrecognized JEDEC id bytes` | ✅ 板上无 SPI NOR，正常提示 |
| **TF 卡** | `Trying to boot from MMC2` —— **没有** `MMC: no card present` | ✅ **卡被识别了**（4-25 两次 Android 日志都有 "no card present"） |
| **GPT** | `GUID Partition Table Entry Array CRC is wrong: 0x83c80496 != 0xab54d286` → `Using Backup GPT` → `No misc partition` → `spl: partition error` | ⚠ 见 §4，非致命 |
| FIT | `Trying fit image at 0x4000 sector` → atf-1 / uboot / fdt / atf-2 / atf-3 **全部 OK** | ✅ FIT 从 TF 卡 sector 16384 加载成功 |
| **缺项** | 检查列表里**没有 `## Checking optee ...`** | ❌ 关键 |
| BL31 | `v2.3-868-g040d2de11, fwver: v1.48`（2024-12-19） | 来自 TF 卡 FIT，比 Android 的 v1.45 新 |
| BL31 | `WARNING: No OPTEE provided by BL2 boot loader` / `ERROR: Error initializing runtime service opteed_fast` | ❌ 根因 |
| U-Boot | `2017.09_armbian-...-S39cd (Jun 28 2026)` / `optee check api revision fail: -1.0` / `### ERROR ### Please RESET the board ###` | ❌ 挂死 |

### 与 4-25 Android 启动日志的对照

| | Android（eMMC，4-25） | 本次（TF 卡，11-10） |
|---|---|---|
| U-Boot | `2017.09 (Sep 14 2024 -01:51:56)` | `2017.09_armbian-... (Jun 28 2026)` |
| BL31 | `v1.45` | `v1.48` |
| SPL FIT 检查项 | atf-1 / uboot / fdt / atf-2 / atf-3 / **optee** = 6 项 | 5 项，**无 optee** |
| OP-TEE | `OP-TEE version: 3.13.0-791-g185dc3c92, fwver: v1.16` | 无 |
| U-Boot 结果 | `optee api revision: 2.0` → 正常进 Android | `-1.0` → RESET |

---

## 三、离线复核（本次新增的两个实测）

### 3.1 `u-boot.itb` 的 FIT 结构

解析 `assets/bootloader/u-boot.itb`（1642496 B）的 FDT：

```
/images (5 个节点)：
   uboot, atf-1, atf-2, atf-3, fdt
含 optee / tee / bl32 的节点：❌ 无
/configurations：1 个（conf）
```

对照 Android 日志的 `## Checking optee 0x08400000 ... OK` —— **Armbian 打包时根本没把 BL32 放进 FIT**。

### 3.2 社区已知问题（完全吻合）

- **armbian/build PR #6696**（`u-boot-radxa-rk3588/legacy ... fix boot from SD when combined with from-factory eMMC blobs (TEE mismatch)`）：
  > "Armbian itself doesn't ship TEE blobs — when combining from-factory eMMC blobs with an Armbian SD card,
  > ... proceeds to fail with `optee api revision is too low` — **disable OPTEE in defconfig fixes it,
  > TEE isn't used in any way by Armbian u-boot**."
  - 该 PR 给 `nanopc_cm3588_defconfig` / `nanopc_t6_defconfig` **不写任何 `CONFIG_OPTEE*` 行**即视为禁用；
  - 启用时的行是 `CONFIG_OPTEE_CLIENT=y` + `CONFIG_OPTEE_V2=y`（+ 可选 `CONFIG_OPTEE_ALWAYS_USE_SECURITY_PARTITION=y`）。
  - PR 讨论还确认：`WARNING: No OPTEE provided by BL2 ...` 来自 BL31 blob，禁用 U-Boot 的 OPTEE 代码后这条 warning 仍在但**无害**。
- **armbian/build PR #6834** 同类问题，解法是 `Fix: firefly-itx-3588j: Use mainline u-boot`。
- **Rockchip TEE SDK 开发指南**：`optee check api revision fail` = "U-Boot 与 TEE 版本不匹配，U-Boot 版本高于 TEE 版本"，解法是回退 U-Boot 或 revert `optee_client` 相关提交。
- Armbian 论坛 NanoPi R6S 案例：换成最新/主线 U-Boot 后，日志变成
  `Scanning bootdev 'mmc@fe2c0000.bootdev': 1 script ready` → `Boot script loaded from mmc 1:1` → 正常读 uInitrd/Image/dtb。

> 也就是说：**这是"原厂 Android eMMC + Armbian SD 卡"的经典坑，不是这块板独有，也不是我们改坏的。**

---

## 四、次生问题：GPT 分区项 CRC 被改写

日志：`0x83c80496 != 0xab54d286`。
实测镜像本身：

```
GPT header 记录 PartitionEntryArrayCRC = 0x83c80496
实际计算 LBA2..33 (128×128 B)        = 0x83c80496   ✅ 一致
GPT header CRC                        = 0xd34f4cfe   ✅ 一致
```

U-Boot `part_efi.c` 的打印格式是 `header存储值 != 实际计算值`，
所以 **header 没变（还是 0x83c80496），是 TF 卡上 LBA2..33 的分区项内容被改写了**（算出来 0xab54d286）。

典型元凶：
- Rufus 用了 **ISO 模式**而不是 **DD 模式**（会自己重建分区表）；
- 写完后 Windows 弹出"需要初始化磁盘"/在磁盘管理里扩展了卷；
- 某些写卡工具写完后"修正" GPT（把 p1 结束 LBA 改成卡容量）。

目前**非致命**（SPL 回退 Backup GPT 后仍成功加载了 FIT），但会让 U-Boot/Linux 看到不一致的分区表，必须一并修掉。

`No misc partition` / `spl: partition error`：SPL 在找 Android 的 `misc` 分区判断启动模式，TF 卡上当然没有，属预期噪声。

---

## 五、修复方案（按推荐度）

### ★ 方案 1：换主线 U-Boot（推荐，改一处、约 10 分钟）

主线 U-Boot 没有 vendor 的 `optee_api_revision()` 检查，且自带标准 distro boot，
会扫 `/boot/extlinux/extlinux.conf` / `/boot/boot.scr`。官方构建方式（docs.u-boot.org）：

```bash
export BL31=../rkbin/bin/rk35/rk3588_bl31_v1.48.elf          # 与本次日志 BL31 版本一致
export ROCKCHIP_TPL=../rkbin/bin/rk35/rk3588_ddr_lp4_2112MHz_lp5_2736MHz_v1.17.bin  # 板上 fwver v1.17
make evb-rk3588_defconfig
make CROSS_COMPILE=aarch64-linux-gnu- -j$(nproc)
# 产物 u-boot-rockchip.bin（TPL+SPL+U-Boot FIT 合一，binman 打包）
sudo dd if=u-boot-rockchip.bin of=/dev/sdX bs=512 seek=64 conv=fsync,notrunc
```

`scripts/06-mainline-uboot.sh` 已把这套流程（依赖、clone、版本自动探测、编译、magic 校验、写卡）封装好。

> 注意：写 `seek=64` 时，binman 会把 U-Boot FIT 自然落在 **sector 16384**，
> 正好覆盖掉旧的、缺 OP-TEE 的 `u-boot.itb`，不需要单独处理 sector 16384。
> 主线 U-Boot 起来后建议卡上有 `/boot/extlinux/extlinux.conf`（`01-fix-sd-boot.sh` 会补三份兜底）。

### 方案 2：重编 vendor U-Boot，禁用 OPTEE（贴原厂，但要重新编译）

在 armbian/build 的 u-boot legacy defconfig 里**删掉**：

```
CONFIG_OPTEE_CLIENT=y
CONFIG_OPTEE_V2=y
CONFIG_OPTEE_ALWAYS_USE_SECURITY_PARTITION=y     # 若有
```

或显式写 `# CONFIG_OPTEE_CLIENT is not set`，然后只重编 u-boot 包并替换 TF 卡 sector 16384。
准确做法见 PR #6696（`nanopc_cm3588_defconfig` 就是"不写这些行"的范例）。
缺点：要走 armbian/build 的 u-boot 编译链，比方案 1 慢且容易踩版本坑。

### 方案 3：给 FIT 补 BL32（不推荐）

从 rkbin 取 `bin/rk35/rk3588_bl32_v1.19.bin`（载入地址 `0x08400000`），
用 `dumpimage` 拆出现有 FIT 各 image、改 ITS 加 `optee` 节点并加入 `loadables`、`mkimage -f` 重打包。
风险：BL31 v1.48 与 BL32 版本、以及 vendor U-Boot 的 OPTEE_CLIENT 三者要严格匹配；
Rockchip 文档指出版本不匹配同样会报 `optee api revision fail`。投入产出比最差。

### 方案 4：借用 eMMC 上 Android 的完整 FIT（仅作快速验证）

Android 的 FIT 含 OP-TEE 且在板上验证过。可以 dump 出来写到 TF 卡 sector 16384 看能否过这一关：

```bash
# 在 Android adb shell 或任何能读 eMMC 的环境
dd if=/dev/mmcblk1 of=android-uboot.itb bs=512 skip=16384 count=16384
# PC 上
sudo dd if=android-uboot.itb of=/dev/sdX bs=512 seek=16384 conv=fsync,notrunc
```

⚠ 但 Android 的 U-Boot 2017.09 默认 `bootcmd` 是 `boot_android`，**很可能不扫 boot.scr**，
所以即使过了 OP-TEE 也不一定会进 Armbian。只适合用来确认"OP-TEE 是不是唯一阻塞"。

---

## 六、写卡顺序（很重要）

```
1. 重新用纯 DD 模式烧完整镜像（03-write-sd-card.sh / Rufus DD 模式 / Etcher）
      ↓  写完立即 sync 拔卡，不要在 Windows 里初始化/挂载/扩展卷  ← 修 GPT CRC
2. 把主线 u-boot-rockchip.bin 写到同一张卡的 sector 64
      ↓
3. 确认卡上 /boot/extlinux/extlinux.conf 存在（没有就跑 01-fix-sd-boot.sh 重新生成镜像）
      ↓
4. 插卡通电
```

如果只是补 U-Boot、不想重烧 10 GiB 镜像，也可以在 Linux 上直接修 GPT CRC：

```bash
sudo sgdisk -e /dev/sdX       # 把备份 GPT 移到盘尾并重算 CRC
sudo sgdisk -v /dev/sdX       # 验证
```

---

## 七、下一轮验收清单

```
□ SPL 日志仍为 "Trying to boot from MMC2"，无 "no card present"
□ 不再出现 "GUID Partition Table Entry Array CRC is wrong"
□ 出现主线 U-Boot 版本号（不再是 2017.09_armbian-...）
□ 不再出现 "optee api revision is too low" / "Please RESET the board"
□ 出现 "Found U-Boot script /boot/boot.scr" 或 "Boot script loaded from mmc 1:1"
□ 出现 "## Loading init Ramdisk" / "## Flattened Device Tree" / booti
□ 内核起来、串口出现 login:（ttyS2 1500000 8N1）
□ 此时才轮到验证上次修的 fdtfile —— 若报 dtb 加载失败再回头看 armbianEnv.txt
```

---

## 八、对既有文档的修正

- `docs/03-全面验证报告.md` 的 21 项离线校验**结论仍然成立**，但它只覆盖"镜像结构"，
  无法预见运行期的 OP-TEE 依赖。已在本文件补充这一层。
- `README.md` 里"拔卡即回 Android"仍然成立：TF 卡启动全程**没有写 eMMC**。
- `assets/bootloader/` 里的 vendor `idbloader.img` / `u-boot.itb` 保留作为回滚用，
  新方案产出的是 `output/u-boot-rockchip.bin`（主线），两者不要混用。
