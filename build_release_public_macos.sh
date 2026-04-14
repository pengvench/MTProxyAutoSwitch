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
    TelethonFakeTLS \
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

# ✅ Исправлено: вызов через модуль вместо прямого вызова команды
python3 -m PyInstaller --noconfirm --clean MTProxyAutoSwitchPublic_macos.spec

mkdir -p release-macos/MTProxyAutoSwitchPublic
cp -R dist/MTProxyAutoSwitchPublic.app release-macos/MTProxyAutoSwitchPublic/
cp README.md release-macos/MTProxyAutoSwitchPublic/README.txt
cp config.json release-macos/MTProxyAutoSwitchPublic/config.json
cp mtproxy_seed.json release-macos/MTProxyAutoSwitchPublic/mtproxy_seed.json
mkdir -p release-macos/MTProxyAutoSwitchPublic/list
if [ -f list/proxy_list.txt ]; then 
    cp list/proxy_list.txt release-macos/MTProxyAutoSwitchPublic/list/proxy_list.txt
fi
if [ -f list/report.json ]; then 
    cp list/report.json release-macos/MTProxyAutoSwitchPublic/list/report.json
fi

echo "Build complete: release-macos/MTProxyAutoSwitchPublic"
