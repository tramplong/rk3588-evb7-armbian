# Armbian for RK3588 EVB7

为 **Rockchip RK3588 EVB7 LP4 V10** 开发板编译 Armbian 系统的 GitHub Actions 工作流。

## 📋 板型信息

- **SoC**: Rockchip RK3588 (8核: 4x Cortex-A76 + 4x Cortex-A55)
- **内存**: 16GB LPDDR4X @ 2112MHz
- **存储**: eMMC
- **显示**: 双 HDMI (支持 4K@60Hz)
- **网络**: 千兆以太网
- **当前系统**: Android 14.0 (内核 6.1.57)

## 🚀 快速开始

### 1. Fork 或克隆此仓库

```bash
git clone https://github.com/你的用户名/rk3588-evb7-armbian.git
cd rk3588-evb7-armbian
```

### 2. 手动触发编译

1. 进入你的 GitHub 仓库
2. 点击 **Actions** 标签页
3. 选择 **Compile Armbian for RK3588 EVB7** 工作流
4. 点击 **Run workflow** 按钮
5. 选择参数：
   - **Ubuntu Release**: `noble` (24.04) 或 `bookworm` (12)
   - **Desktop Environment**: `cinnamon`, `gnome`, 或 `xfce`
   - **Build with Desktop**: `yes` 或 `no`
6. 点击 **Run workflow** 确认

### 3. 等待编译完成

- **编译时间**: 约 1-2 小时
- **查看进度**: 在 Actions 标签页中实时查看日志
- **完成通知**: GitHub 会发送邮件通知

### 4. 下载编译好的镜像

1. 进入 **Actions** 标签页
2. 点击你的编译任务
3. 在 **Artifacts** 区域，点击下载镜像文件
4. 解压后得到 `.img` 文件

## 📥 刷机指南

### 方法 1: 使用 balenaEtcher (推荐)

1. 下载并安装 [balenaEtcher](https://www.balena.io/etcher/)
2. 插入 TF 卡 (至少 8GB)
3. 打开 balenaEtcher
4. 选择镜像文件
5. 选择 TF 卡
6. 点击 **Flash**

### 方法 2: 使用 dd 命令 (Linux/macOS)

```bash
# 查看 TF 卡设备名
lsblk

# 卸载 TF 卡分区
sudo umount /dev/sdb*

# 刷入镜像 (⚠️ 会清空 TF 卡所有数据！)
sudo dd if=Armbian_*.img of=/dev/sdb bs=4M status=progress && sync
```

### 方法 3: 使用 RKDevTool (Windows, 进入 Mask ROM 模式)

1. 下载 [RKDevTool](https://www.rock-chips.com/a/cn/downloads/)
2. 将开发板进入 Mask ROM 模式
3. 打开 RKDevTool
4. 加载镜像文件
5. 点击 **Run** 开始刷机

## 🔧 首次启动

### 1. 准备串口连接 (重要！)

- **连接**: USB-TTL 转接线连接到板子的 UART0
- **波特率**: 1500000
- **数据位**: 8
- **停止位**: 1
- **校验**: 无

### 2. 上电并观察串口输出

- 看到 U-Boot 启动信息 ✅
- 看到内核启动信息 ✅
- 看到登录提示符 ✅

### 3. 登录系统

- **用户**: `root`
- **密码**: `1234` (首次登录会要求修改)

## 📋 默认配置

- **内核**: 6.1.115-vendor-rk35xx
- **U-Boot**: 2017.09-S39cd
- **桌面**: Cinnamon (可选)
- **镜像大小**: 约 5.79 GB

## 🔍 常见问题

### 问题 1: 串口无输出

**原因**: 设备树不匹配  
**解决**: 从 Android 系统提取真实设备树并替换

### 问题 2: 内核 panic

**原因**: 内存配置不正确  
**解决**: 修改设备树中的 memory 节点

### 问题 3: 网络/USB/HDMI 不工作

**原因**: 设备树中这些节点未正确配置  
**解决**: 使用提取的 Android 设备树替换

## 📚 相关资料

- [Armbian 官方文档](https://docs.armbian.com/)
- [Rockchip 开源社区](http://opensource.rock-chips.com/)
- [RK3588 数据手册](https://www.rock-chips.com/a/en/products/RK3588/)

## 📄 许可证

MIT License

## 🙏 致谢

- [Armbian](https://www.armbian.com/) - 优秀的 ARM 开发板系统
- [ophub](https://github.com/ophub/amlogic-s9xxx-armbian) - 参考项目
