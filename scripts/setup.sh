#!/bin/bash
# RK3588 EVB7 Armbian 本地编译设置脚本

set -e

echo "=========================================="
echo "RK3588 EVB7 Armbian 编译环境设置"
echo "=========================================="
echo ""

# 颜色定义
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# 检查是否在正确的目录
if [ ! -f "config/boards/rk3588-evb7.csc" ]; then
    echo -e "${RED}错误: 请在仓库根目录运行此脚本！${NC}"
    exit 1
fi

echo -e "${YELLOW}[*] 检查编译依赖...${NC}"

# 检查必要工具
MISSING_PACKAGES=()

for cmd in git wget gawk gpg make gcc dtc curl bc; do
    if ! command -v $cmd &> /dev/null; then
        MISSING_PACKAGES+=($cmd)
    fi
done

if [ ${#MISSING_PACKAGES[@]} -ne 0 ]; then
    echo -e "${RED}[!] 缺少必要工具: ${MISSING_PACKAGES[*]}${NC}"
    echo -e "${YELLOW}[i] 正在安装依赖...${NC}"
    
    sudo apt-get update
    sudo apt-get install -y \
        git wget gawk gpg build-essential \
        libssl-dev zlib1g-dev uuid-dev libacl1-dev libblkid-dev \
        liblzo2-dev liblz4-dev kmod bc rsync cpio \
        parted dosfstools debootstrap xz-utils qemu-user-static \
        bison flex u-boot-tools curl xxd \
        python3 python3-pip python3-dev python3-venv \
        device-tree-compiler default-jdk \
        gcc-aarch64-linux-gnu libc6-i386 lib32stdc++6
fi

echo -e "${GREEN}[✓] 依赖检查完成${NC}"
echo ""

# 克隆 Armbian 构建框架
if [ ! -d "armbian-build" ]; then
    echo -e "${YELLOW}[*] 克隆 Armbian 构建框架...${NC}"
    git clone --depth=1 --branch=main https://github.com/armbian/build.git armbian-build
    echo -e "${GREEN}[✓] Armbian 构建框架克隆完成${NC}"
else
    echo -e "${GREEN}[✓] Armbian 构建框架已存在${NC}"
fi
echo ""

# 复制板型配置
echo -e "${YELLOW}[*] 复制板型配置文件...${NC}"
mkdir -p armbian-build/config/boards/
cp config/boards/rk3588-evb7.csc armbian-build/config/boards/
echo -e "${GREEN}[✓] 板型配置已复制${NC}"
echo ""

# 应用内核补丁
echo -e "${YELLOW}[*] 应用内核补丁...${NC}"
mkdir -p armbian-build/patch/kernel/rk35xx-vendor-6.1/ 2>/dev/null || true
if [ -d "patch/kernel/rk35xx-vendor-6.1" ]; then
    cp patch/kernel/rk35xx-vendor-6.1/*.patch armbian-build/patch/kernel/rk35xx-vendor-6.1/ 2>/dev/null || true
fi
echo -e "${GREEN}[✓] 内核补丁已应用${NC}"
echo ""

# 显示编译选项
echo "=========================================="
echo -e "${GREEN}环境设置完成！${NC}"
echo "=========================================="
echo ""
echo "编译选项："
echo ""
echo "1. 编译无桌面版本 (CLI only):"
echo "   cd armbian-build"
echo "   ./compile.sh BOARD=rk3588-evb7 BRANCH=vendor RELEASE=noble BUILD_DESKTOP=no KERNEL_BTF=no"
echo ""
echo "2. 编译 Cinnamon 桌面版本:"
echo "   cd armbian-build"
echo "   ./compile.sh BOARD=rk3588-evb7 BRANCH=vendor RELEASE=noble BUILD_DESKTOP=yes KERNEL_BTF=no DESKTOP_ENVIRONMENT=cinnamon"
echo ""
echo "3. 使用自定义配置编译:"
echo "   cd armbian-build"
echo "   ./compile.sh build BOARD=rk3588-evb7 BRANCH=vendor RELEASE=noble BUILD_DESKTOP=yes KERNEL_BTF=no"
echo ""
echo "=========================================="
echo "GitHub Actions 编译："
echo "  1. 推送代码到 GitHub"
echo "  2. 进入 Actions 标签页"
echo "  3. 选择 'Compile Armbian for RK3588 EVB7'"
echo "  4. 点击 'Run workflow'"
echo "=========================================="
echo ""

