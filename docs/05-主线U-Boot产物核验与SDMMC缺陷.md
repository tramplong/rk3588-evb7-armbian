# 主线 U-Boot 产物核验：发现比 OP-TEE 更靠前的 SDMMC 缺陷

> 2026-11-10 22:2x。对象：`windows/u-boot-rockchip.bin`
> （GitHub Actions run `42763228917` / Artifact `rk3588-evb7-mainline-uboot`）
> SHA256 `60a88eec956233d2345176f137157693cca940f6feceb8f708d1c58a04fffb31`

## 结论先行

**这个 bin 结构与偏移全部正确，但先不要写卡。**

`evb-rk3588_defconfig` 用的是 Rockchip **EVB1** 的板级设备树，而 EVB1 没有 TF 卡槽：
它把 SDMMC 控制器（TF 卡所在的 `mmc@fe2c0000`）置为 `disabled`，并且
`u-boot,spl-boot-order` 里只有 eMMC（`&sdhci`）。EVB7 却是从 TF 卡启动的。
这会在 **SPL 阶段**（早于 `boot.scr`，更早于 OP-TEE 检查）导致找不到启动介质。

修复只需在编译前给 EVB1 的 `-u-boot.dtsi` 追加一小段覆盖：启用 `&sdmmc`、
把它加回 SPL 启动顺序。引脚无需改动（EVB1 与 EVB7 的 `sdmmc-det` 都是 GPIO0_A4）。

**修复已写入 `github-relink/upload/.github/workflows/build-mainline-uboot.yml`**，
并新增编译后门禁：从产物 FIT 里解出控制 DTB，断言 `mmc@fe2c0000 status == "okay"`
且出现在 `u-boot,spl-boot-order` 中，否则 CI 直接失败，杜绝再产出坏 bin。

---

## 一、产物解剖（全部通过）

文件 9,494,016 B = `0x90DE00`。

| 位置 | 内容 | 判定 |
|---|---|---|
| `0x000000` | `RKNS`（idbloader / TPL+SPL） | ✅ |
| `0x7F8000` | FIT magic `d0 0d fe ed`，totalsize 2560 B | ✅ `CONFIG_SPL_PAD_TO` |
| `0x7F8A00` | 外置数据区起点（`CONFIG_FIT_EXTERNAL_OFFSET = 0xA00`） | ✅ |

外置数据段（`data-offset` 相对 `FIT + 0xA00`）：

| 段 | 文件区间 | 大小 | 说明 |
|---|---|---|---|
| `u-boot` | `0x7F8A00..0x8BC248` | 800,840 B | U-Boot proper |
| `atf-1` | `0x8BC400..0x8E0ACC` | 149,196 B | BL31 (TF-A) |
| `atf-2` | `0x8E0C00..0x8E6C00` | 24,576 B | BL31 split-elf |
| `atf-3` | `0x8E6C00..0x8EFC00` | 36,864 B | BL31 split-elf |
| `fdt-1` | `0x8EFC00..0x90DCC8` | 123,080 B | 控制 DTB |

- 全部落在文件内，段与段之间仅 512 B 对齐，无越界。
- 文件尾 `0x90DE00` < rootfs 分区起点 `0x1000000`（16 MiB），
  `dd seek=64` 不会污染 rootfs。
- `/images` 共 5 个节点：`u-boot / atf-1 / atf-2 / atf-3 / fdt-1`，
  **没有 `optee` / `tee` / `bl32`** → 主线 U-Boot 不做 vendor 的 OP-TEE API 检查，
  这正是我们要的（原阻塞 `optee api revision is too low` 被绕开）。
- `/configurations/config-1`：`firmware=atf-1`，
  `loadables=u-boot, atf-2, atf-3`，`fdt=fdt-1`。

**写卡偏移再次实证**：
`u-boot-rockchip.bin` 内 FIT 在 `0x7F8000`；`dd seek=64`（`0x8000`）后
→ `0x8000 + 0x7F8000 = 0x800000` = sector 16384，正好覆盖旧的、缺 BL32 的
`u-boot.itb`。零问题。

---

## 二、缺陷：控制 DTB 是 EVB1，TF 卡控制器被关掉

从 `fdt-1` 解出的控制 DTB：

```
description = fdt-rockchip/rk3588-evb1-v10
config-1 description = rockchip/rk3588-evb1-v10.dtb
```

关键节点（解析 DTB 属性得到）：

| 节点 | 主线 EVB1 DTB | EVB7 原厂 DTS |
|---|---|---|
| `mmc@fe2c0000`（SDMMC / TF 卡） | **`status = "disabled"`** | `status = "okay"`, `supports-sd`, `bus-width=4`, `sd-uhs-sdr104` |
| `mmc@fe2e0000`（sdhci / eMMC） | `status = "okay"`, `bus-width=8` | `status = "okay"`, `no-sdio` |
| `chosen/stdout-path` | `serial2:1500000n8` | 一致 ✅ |
| `chosen/u-boot,spl-boot-order` | `"same-as-spl", /mmc@fe2e0000` | — |
| `pinctrl/sdmmc/sdmmc-det` | `rockchip,pins = <0 4 ...>` (GPIO0_A4) | `<0 4 ...>` (GPIO0_A4) ✅ |

源码佐证（U-Boot v2025.04）：

- `arch/arm/dts/rk3588s-u-boot.dtsi`：
  `chosen { u-boot,spl-boot-order = "same-as-spl", &sdmmc, &sdhci; };`
  且 `&sdmmc { bootph-pre-ram; ... }`。
- `arch/arm/dts/rk3588-evb1-v10-u-boot.dtsi` **覆盖**为：
  `u-boot,spl-boot-order = "same-as-spl", &sdhci;` —— 把 `&sdmmc` 删掉了。
- `dts/upstream/.../rk3588-evb1-v10.dts` 中根本没有 `&sdmmc { ... }` 覆盖段，
  只有 `&sdhci { status = "okay"; }`，所以 sdmmc 继承 dtsi 的 `disabled`。

**好消息**：`sdmmc-det` 的 pinctrl 在 EVB1 与 EVB7 完全一致（GPIO0_A4），
所以不需要改引脚，只要把节点打开即可。

**风险判定**：`same-as-spl` 理论上会让 SPL 继续从 BootROM 加载它的那个设备读 FIT，
但 SPL 的设备树里 `mmc@fe2c0000` 为 `disabled`，`boot_devices[]` 映射到的
DT path 无法 probe，且启动顺序里连 `&sdmmc` 都没有。典型表现：

```
Trying to boot from MMC2
MMC: no card present        /  spl: mmc init failed
```
或直接回落到 eMMC、加载原厂 Android 的 FIT，行为不可预测。
这不是 100% 必然（不同 U-Boot 版本对 `bootph-pre-ram` 节点的 status 处理有差异），
但**重编译成本只有 2 分钟，没有任何理由带着这个不确定性去写卡**。

---

## 三、次生偏差：DDR / BL31 blob 版本与板载实测不符

`build-info.txt`：

```
DDR bin : rk3588_ddr_lp4_2112MHz_lp5_2400MHz_v1.24.bin
BL31    : rk3588_bl31_v1.56.elf
```

而 4-25 / 11-10 串口日志里板子实际是 **DDR v1.17 / BL31 v1.48**。
原因不是脚本写错，而是上游 rkbin 已删除旧 blob：

| 版本 | 当前 rkbin `bin/rk35/` | 结论 |
|---|---|---|
| DDR `rk3588_ddr_lp4_2112MHz*` | 仅 3 个 `v1.24`（含 eyescan / single_vdd2 变体） | v1.17 已删 |
| BL31 `rk3588_bl31_*` | 仅 `v1.56.elf` | v1.48 已删 |

脚本里的 `grep -F 'v1.17'` / `-iname '*v1.48*'` 必然落空，兜底 `sort -V | tail -1`
取了最新。这属于**正常降级**，新 blob 通常向后兼容，且主线 U-Boot v2025.04
配新 BL31 反而更合适；风险低于 SDMMC 缺陷。

如需复现板载组合，可 pin rkbin（已加到 workflow 的 `rkbin_ref` 输入，注意两个版本
不在同一个 commit 上，见下表）：

| 想要的 blob | 最后一个含它的 commit | 说明 |
|---|---|---|
| DDR v1.17 | `3e5f2374e` | `b9183559c`（"update ddr bin to v1.18"）的父提交 |
| BL31 v1.48 | `feab2172b` | `09c922d81`（"update bl31 to v1.49"）的父提交 |

修复版 workflow 还顺手排除了 `eyescan` / `single_vdd2` 变体，
避免 `sort -V | tail -1` 选到不适合正常启动的 DDR bin。

---

## 四、已做的修复

`github-relink/upload/.github/workflows/build-mainline-uboot.yml`（rev2）：

1. 新增 `sd_fix` 输入（默认 `yes`）：编译前往
   `arch/arm/dts/rk3588-evb1-v10-u-boot.dtsi` 追加：
   ```dts
   &sdmmc {
       status = "okay";
       bus-width = <4>;
       no-sdio;
       no-mmc;
       cap-sd-highspeed;
       disable-wp;
   };
   &chosen {
       u-boot,spl-boot-order = "same-as-spl", &sdmmc, &sdhci;
   };
   ```
   `-u-boot.dtsi` 是最后被 include 的，覆盖一定生效。
2. 新增 `rkbin_ref` 输入，可 pin 旧 blob。
3. DDR 选择排除 `eyescan` / `single` 变体。
4. **编译后门禁**：解析产物 FIT → 找到控制 DTB → 断言
   `mmc@fe2c0000 status == "okay"` 且 `spl-boot-order` 含 `mmc@fe2c0000`，
   同时校验 `RKNS`、FIT@`0x7F8000`、各数据段边界、`< 16 MiB`。
   该门禁已用**当前这个坏 bin 实测验证**，能正确报
   `SDMMC disabled: SPL cannot find the FIT on the TF card`。
5. `trigger-mainline-uboot.txt` bump 到 `build: 2`。

另修 `push-to-github.py --download` 一个小问题：artifact 自带的
`.sha256` 里写的是 runner 路径 `out/u-boot-rockchip.bin`，会覆盖本地生成的
裸文件名校验和，导致 `sha256sum -c` 找不到文件。现在只复制 `build-info.txt`。
本地 `windows/u-boot-rockchip.bin.sha256` 已重写，`sha256sum -c` 通过。

---

## 五、下一步（按顺序）

```powershell
cd D:\WorkBuddy\2026-11-04-22-29-13\github-relink

# 1. 推送修复版 workflow + trigger(build:2)，自动触发云编译（约 2 分钟）
.\run.cmd --cancel-stale

# 2. 下载修复版 bin（会覆盖 windows/u-boot-rockchip.bin）
.\run.cmd --download
```

**检查点**：`build-info.txt` 必须出现 `sd_fix : yes`，SHA256 必须与 rev1 的
`60a88eec956233d2345176f137157693cca940f6feceb8f708d1c58a04fffb31` 不同，
否则说明 rev2 编译失败、`--download` 又取回了旧 artifact。

### ③ 写卡：推荐「直写 sector 64」，省掉 14 分钟重压

`patch-bootloader.py` 生成 `final.img.xz` 实测耗时 **14 分钟**（重压 10.7 GiB），
但 bin 只占 sector 64 起 9.5 MB、rootfs 完全不变。所以拿到 rev2 bin 后**不必重新
生成整份镜像**：

```powershell
# a) 先用 Etcher 把现有的 final.img.xz（rev1 bin）写入 TF 卡
#    写完后 Windows 弹「初始化/格式化」一律取消
# b) 管理员 PowerShell：把 rev2 bin 直接覆盖卡的 sector 64
cd D:\WorkBuddy\2026-11-04-22-29-13\rk3588-evb7-armbian\windows
Set-ExecutionPolicy -Scope Process Bypass -Force
.\Write-UbootToSD.ps1 -ListOnly        # 确认磁盘号，本机曾是 PhysicalDrive3
.\Write-UbootToSD.ps1 -UbootBin .\u-boot-rockchip.bin -DiskNumber 3
```

脚本会校验 `RKNS`、拒绝超过 16 MiB 的 blob、两次确认、拒绝含盘符的系统盘，
写入后读回 sector 64 验证。已实测语法与 `-ListOnly` 可用。

> **等价性说明**：`patch-bootloader.py` 只做 `sector 64 .. 18607` 的等长替换，
> 其余字节流式透传。先用 rev1 镜像写卡、再直写 rev2 bin，与「用 rev2 bin 重新
> 生成整份镜像再写卡」在卡上**逐字节等价**。这样 rev1 那份 2.3 GB 成品不浪费。

### 备选：重新生成整份镜像（多花 14 分钟）

仅在需要把成品镜像分发/上传 Release 时才需要做：

```powershell
cd D:\WorkBuddy\2026-11-04-22-29-13\rk3588-evb7-armbian
& "$env:USERPROFILE\.workbuddy\binaries\python\versions\3.13.12\python.exe" .\windows\patch-bootloader.py `
    .\output\Armbian_rk3588-evb7_sd-boot.img.xz `
    .\windows\u-boot-rockchip.bin .\output\Armbian_rk3588-evb7_final.img.xz --preset 1 --verify
```

### 本轮已完成的结构验证（rev1 样本）

`output/Armbian_rk3588-evb7_final.img.xz` = 2,373,549,020 B（解压 10,716,446,720 B），
SHA256 `47df095d19f08df88b2398becbc5b91c77f42270d943b32dd590c147e4e72d0d`。

`--verify` 与独立交叉验证均通过：

```
sector 64    : RKNS                          OK
sector 16384 : d0 0d fe ed (= bin 内 0x7f8000) OK
整个 bin 替换区 0x8000..0x915e00 逐字节一致   OK
GPT signature EFI PART / 分区项 CRC 0x83c80496 / header CRC 0xd34f4cfe  OK
rootfs 起点 0x1000000 未被触碰                OK
```

> 这份 rev1 成品**不要直接通电启动**（SDMMC 缺陷仍在）；它只是证明了
> 注入管线、`dd seek=64` 偏移与 GPT 完整性都正确，等 rev2 bin 直写覆盖即可用。

若重编后 SPL 仍报 `MMC: no card present`，回退到
`docs/04-实机启动诊断-OP-TEE.md` 的方案 2：重编 **vendor** U-Boot 并删掉
`CONFIG_OPTEE_CLIENT` / `CONFIG_OPTEE_V2`，保留已实测能读 TF 卡的 vendor SPL
（以及板载匹配的 DDR v1.17 / BL31 v1.48）。
