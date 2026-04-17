#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

APP_NAME="MTProxyAutoSwitch"
APP_BUNDLE="release-macos/${APP_NAME}.app"
PKG_ROOT="release-macos/pkgroot"
PKG_PATH="release-macos/${APP_NAME}.pkg"
IDENTIFIER="com.mtproxyautoswitch"
VERSION="1.2"

if [ ! -d "${APP_BUNDLE}" ]; then
    echo "App bundle not found: ${APP_BUNDLE}"
    echo "Run ./build_release_macos.sh first."
    exit 1
fi

if ! command -v pkgbuild >/dev/null 2>&1; then
    echo "pkgbuild not found. Install Xcode Command Line Tools."
    exit 1
fi

rm -rf "${PKG_ROOT}" "${PKG_PATH}"
mkdir -p "${PKG_ROOT}/Applications"
cp -R "${APP_BUNDLE}" "${PKG_ROOT}/Applications/${APP_NAME}.app"

pkgbuild \
    --root "${PKG_ROOT}" \
    --identifier "${IDENTIFIER}" \
    --version "${VERSION}" \
    --install-location "/" \
    "${PKG_PATH}"

rm -rf "${PKG_ROOT}"
