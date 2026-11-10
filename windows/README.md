# Windows 上编译并写入主线 U-Boot

> 目标：修掉 2026-11-10 实机日志里的
> `optee api revision is too low / ### ERROR ### Please RESET the board ###`。
> 详见 `docs/04-实机启动诊断-OP-TEE.md`。

---

## 0. 本机实测环境（2026-11-10）

| 项 | 状态 |
|---|---|
| Python | ✅ 3.13.14 |
| PowerShell | ✅ 5.1，`Get-Disk` 可用 |
| WSL | ⚠ WorkBuddy 沙箱内 `wsl.exe` 被策略拦截；**你自己开的管理员 PowerShell 里不受此限** |
| Docker | ❌ 未安装 |
| aarch64 交叉编译器 | ❌ 无 |
| TF 卡 | ✅ 已识别：`PhysicalDrive3` = `Lexar RW310 USB Device` 31.4 GB（USB） |

**结论：Windows 原生编译不了 U-Boot**（缺 GNU make、交叉工具链、大小写敏感文件系统、
`python3-pyelftools`、OpenSSL 头文件等）。别在 MSYS2/MinGW 上硬试，坑比收益大。
正确做法二选一：**云端编译** 或 **WSL2 只负责编译**；**写卡始终在 Windows 侧完成**。

---

## 1. 三条编译路径（任选一条，产出同一个 `u-boot-rockchip.bin`）

### 路径 A：GitHub Actions 云编译 ★ 推荐（零本地 Linux）

你的仓库 `tramplong/rk3588-evb7-armbian` 已经有 Actions。我准备好了 workflow：

```
github-relink/.github/workflows/build-mainline-uboot.yml
```

步骤：

1. 把这个文件推到仓库的 `.github/workflows/`（用 `github-relink/scripts/01-link-repo.sh`，
   或 GitHub 网页 Add file → Create new file）。
   > ⚠ 推到 `main` 会**顺带触发**原有的 `Compile Armbian`（8 小时编译）。
   > 推完立刻到 Actions 页把那次 run 取消；或给 `compile-armbian.yml` 的 `push` 加
   > `paths-ignore: ['.github/workflows/build-mainline-uboot.yml']`。
2. Actions → **Build mainline U-Boot (RK3588 EVB7)** → Run workflow。
   默认参数：`uboot_ref=v2025.04`、`defconfig=evb-rk3588_defconfig`、
   `ddr_version=v1.17`、`bl31_version=v1.48`（后两个正好对上日志里的 DDR/BL31 版本）。
3. 约 5–10 分钟跑完，下载 Artifact `rk3588-evb7-mainline-uboot`，解压得到：
   - `u-boot-rockchip.bin`（几 MiB，头部 `RKNS`）
   - `u-boot-rockchip.bin.sha256`
   - `build-info.txt`（含 commit、rkbin 版本，留档用）
4. 把 `u-boot-rockchip.bin` 放到本目录：`rk3588-evb7-armbian/windows/`。

### 路径 B：WSL2 本地编译（不想用云 / 想自己改 U-Boot）

管理员 PowerShell 里执行一次（只需一次，之后会提示重启）：

```powershell
wsl --install -d Ubuntu-24.04
```

重启、设好用户名密码后，进 WSL：

```bash
sudo apt update && sudo apt install -y \
  git make gcc bc bison flex swig device-tree-compiler \
  gcc-aarch64-linux-gnu binutils-aarch64-linux-gnu \
  python3 python3-dev python3-setuptools python3-pyelftools \
  libssl-dev libgnutls28-dev libncurses-dev u-boot-tools

cd /mnt/d/WorkBuddy/2026-11-04-22-29-13/rk3588-evb7-armbian
./scripts/06-mainline-uboot.sh --build
```

产物：`output/u-boot-rockchip.bin`。
**WSL2 只用来编译**，不要试图在 WSL 里直接 dd 到 USB 读卡器（默认不直通）；
写卡回到 Windows，用下面的第 2 节。

### 路径 C：之前那台 Ubuntu 24.04 虚拟机还开着的话

直接把整个 `rk3588-evb7-armbian/` 拷进去，跑 `./scripts/06-mainline-uboot.sh --build`，
再把 `output/u-boot-rockchip.bin` 拷回 Windows。其余同第 2 节。

> 想完全不编译？理论上可以从 Armbian 官方为**使用主线 U-Boot 的 RK3588 板**
> （如 NanoPC-T6）发布的镜像里提取 sector 64 起的 bootloader，但 DDR bin / SD 卡检测脚
> 不保证与 EVB7 一致，风险自负，不作为主推。

---

## 2. 两条写入路径

### 路径 A：注入完整镜像 + Etcher 重烧 ★ 最稳（顺带修 GPT CRC）

Rufus 那次把卡上 GPT 分区项改坏了（日志 `0x83c80496 != 0xab54d286`）。
重烧整盘能一次性把它恢复干净，同时换掉 U-Boot。

在 **普通 PowerShell**（不需要管理员）里：

```powershell
cd D:\WorkBuddy\2026-11-04-22-29-13\rk3588-evb7-armbian\windows

python patch-bootloader.py `
    ..\output\Armbian_rk3588-evb7_sd-boot.img.xz `
    .\u-boot-rockchip.bin `
    .\Armbian_rk3588-evb7_sd-boot_mainline-uboot.img.xz `
    --preset 1 --verify
```

- 纯 Python 流式处理，只改前 16 MiB 的 bootloader 区，其余 10.7 GiB 原样重压；
- 约 5–8 分钟；不需要管理员、不需要挂载、不需要 Linux；
- `--verify` 会读回校验 `sector 64 = RKNS`、GPT 头、分区项 CRC；
- `--preset 6+` 才适合上传 GitHub Release（单文件 < 2 GiB），本地写卡用 1 即可。

然后用 **balenaEtcher**（或 Rufus 一定选 **DD 模式**）把
`Armbian_rk3588-evb7_sd-boot_mainline-uboot.img.xz` 整盘写入 TF 卡。

> 写完立刻安全弹出。Windows 若弹「需要初始化磁盘 / 需要格式化」——**一律点取消**，
> 那正是上次把 GPT 改坏的元凶。

### 路径 B：PowerShell 直写 sector 64（快，卡已重烧过时用）

适合"卡已经用 Etcher 重烧干净、只想换 U-Boot"的情况，秒级完成，不用重压 10 GiB。

`-ListOnly` 用普通 PowerShell 即可；**真正写入要用管理员 PowerShell**
（不是 WorkBuddy 内置终端，脚本会自己检查提权并给出提示）：

```powershell
cd D:\WorkBuddy\2026-11-04-22-29-13\rk3588-evb7-armbian\windows
Set-ExecutionPolicy -Scope Process Bypass -Force
.\Write-UbootToSD.ps1 -ListOnly        # 先确认磁盘号，不需要管理员
# 另开一个管理员 PowerShell，再执行：
.\Write-UbootToSD.ps1 -UbootBin .\u-boot-rockchip.bin -DiskNumber 3
```

> 脚本内容刻意写成**纯 ASCII 英文**：Windows PowerShell 5.1 会用 GBK 读取无 BOM 的
> `.ps1`，中文会乱码甚至导致语法错误。中文说明都在本 README 里。

`-ListOnly` 在本机实测输出（2026-11-10）：

```
=== USB / removable disks ===
  PhysicalDrive3  Lexar RW310 USB Device    29.3 GB  drive letters: none
  PhysicalDrive2  Lexar RW310 USB Device  (no media) drive letters: none
=== All disks ===
  Number 1  WD_BLACK SN770 1TB      NVMe  GPT  931.5 GB   <- 系统盘，别选
  Number 0  WOOKING CE980Y38900-1TB NVMe  GPT  953.9 GB   <- 系统盘，别选
  Number 2  Lexar RW310              USB   RAW    0 GB    <- 读卡器空槽
  Number 3  Lexar RW310              USB   GPT   29.3 GB  <- 目标 TF 卡
```

脚本会要求两次确认、拒绝含 `C:` 的盘，写入后读回 sector 64 校验 `RKNS`。
`PhysicalDrive3` 显示 `GPT` 说明卡上确实是烧过镜像的分区表。
**每次插卡都要重新用 `-ListOnly` 确认，不要照抄 3**。

若 Windows 锁了物理盘写入，脚本会打印降级方案（改用路径 A，或 `Set-Disk -IsOffline $true` 后重试）。

---

## 3. 完整顺序（推荐照抄）

```
① 云端编译 or WSL2 编译  →  得到 u-boot-rockchip.bin
② Windows: python patch-bootloader.py ... --verify   （生成注入后的完整镜像）
③ balenaEtcher 整盘写入 TF 卡                       （同时修好 GPT）
④ 安全弹出，Windows 弹格式化一律取消
⑤ 插卡通电，串口 1500000 8N1 观察
```

> 若选了写入路径 B，则顺序改为：先 Etcher 重烧**原镜像** → 再 PowerShell 直写 U-Boot。

---

## 4. 下一轮串口验收

```
□ SPL 仍为 "Trying to boot from MMC2"，无 "no card present"
□ 不再出现 "GUID Partition Table Entry Array CRC is wrong"
□ 出现主线 U-Boot 版本号（不再是 2017.09_armbian-...-S39cd）
□ 不再出现 "optee check api revision fail" / "Please RESET the board"
□ 出现 "Found U-Boot script /boot/boot.scr" 或 "Boot script loaded from mmc 1:1"
□ "## Loading init Ramdisk from Legacy Image" → 内核解压 → login:
```

只有走到 `boot.scr` 之后，之前修的 `fdtfile=rk3588-evb7-lp4-v10-linux.dtb` 才会开始起作用。

### 若主线 SPL 读不到卡

主线 `evb-rk3588_defconfig` 用的是 EVB1 的 DTS，SD 卡检测脚未必与 EVB7
（反编译 DTS 里是 `sdmmc-det` = GPIO0_A4）一致。若出现
`MMC: no card present` / `spl: mmc init failed`，说明 SPL 阶段卡就没识别到，
此时改用 `docs/04-实机启动诊断-OP-TEE.md` 的**方案 2**：
重编 vendor U-Boot，在 defconfig 里删掉 `CONFIG_OPTEE_CLIENT` / `CONFIG_OPTEE_V2`
（Armbian PR #6696 的做法），保留已验证能读卡的 vendor SPL。

---

## 5. 目录

```
windows/
├── README.md                 本文件
├── patch-bootloader.py       把 bin 注入完整 .img.xz（纯 Python，跨平台）
└── Write-UbootToSD.ps1       管理员 PowerShell 直写 TF 卡 sector 64

github-relink/.github/workflows/
└── build-mainline-uboot.yml  云端编译 workflow（推到仓库后手动触发）
```

相关：`scripts/06-mainline-uboot.sh`（WSL2 / Ubuntu VM 里编译）、
`docs/04-实机启动诊断-OP-TEE.md`（根因与四个方案对比）。
