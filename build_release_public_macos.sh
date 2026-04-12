#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

python3 -m pip install --upgrade \
  pyinstaller \
  customtkinter \
  darkdetect \
  pystray \
  qrcode \
  TelethonFakeTLS \
  telethon \
  cryptography \
  pillow \
  imageio \
  imageio-ffmpeg \
  pyobjc-core \
  pyobjc-framework-Cocoa \
  pyobjc-framework-Quartz

rm -rf build dist release-macos

pyinstaller --noconfirm --clean MTProxyAutoSwitchPublic.macos.spec

mkdir -p release-macos/MTProxyAutoSwitchPublic
cp -R dist/MTProxyAutoSwitchPublic.app release-macos/MTProxyAutoSwitchPublic/
cp README.md release-macos/MTProxyAutoSwitchPublic/README.txt
cp config.json release-macos/MTProxyAutoSwitchPublic/config.json
mkdir -p release-macos/MTProxyAutoSwitchPublic/list
if [ -f list/proxy_list.txt ]; then cp list/proxy_list.txt release-macos/MTProxyAutoSwitchPublic/list/proxy_list.txt; fi

echo "Build complete: release-macos/MTProxyAutoSwitchPublic"
