#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

case "${MTPROXY_TARGET_ARCH:-$(uname -m)}" in
    arm64|aarch64)
        export MTPROXY_TARGET_ARCH=arm64
        ;;
    x86_64|amd64)
        export MTPROXY_TARGET_ARCH=x86_64
        ;;
    universal2)
        export MTPROXY_TARGET_ARCH=universal2
        ;;
    *)
        unset MTPROXY_TARGET_ARCH
        ;;
esac

echo "Using macOS target arch: ${MTPROXY_TARGET_ARCH:-default}"

python3 -m pip install --upgrade \
    pyinstaller \
    customtkinter \
    darkdetect \
    pystray \
    qrcode \
    telethon \
    certifi \
    cryptography \
    pillow \
    imageio \
    imageio-ffmpeg \
    pyobjc-core \
    pyobjc-framework-Cocoa \
    pyobjc-framework-Quartz

rm -rf build dist release-macos

python3 -m PyInstaller --noconfirm --clean MTProxyAutoSwitch_macos.spec

mkdir -p release-macos
cp -R dist/MTProxyAutoSwitch.app release-macos/MTProxyAutoSwitch.app
cp README.md release-macos/README.txt
cp config.template.json release-macos/config.template.json
mkdir -p release-macos/list
if [ -f list/proxy_list.txt ]; then
    cp list/proxy_list.txt release-macos/list/proxy_list.txt
fi
if [ -f list/report.json ]; then
    cp list/report.json release-macos/list/report.json
fi

bash "$(dirname "$0")/build_pkg_macos.sh"

echo "Build complete:"
echo "  release-macos/MTProxyAutoSwitch.app"
echo "  release-macos/MTProxyAutoSwitch.pkg"
