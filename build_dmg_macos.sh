#!/usr/bin/env bash
# build_dmg_macos.sh — упаковка MTProxyAutoSwitch.app в .dmg
# Запускать после build_release_macos.sh
set -euo pipefail
cd "$(dirname "$0")"

APP_NAME="MTProxyAutoSwitch"
DMG_NAME="${APP_NAME}.dmg"
APP_BUNDLE="release-macos/${APP_NAME}/${APP_NAME}.app"
STAGING_DIR="release-macos/dmg_staging"
FINAL_DMG="release-macos/${DMG_NAME}"
VOLUME_NAME="MTProxy AutoSwitch"

if [ ! -d "${APP_BUNDLE}" ]; then
    echo "❌  App bundle not found: ${APP_BUNDLE}"
    echo "    Сначала запустите: ./build_release_macos.sh"
    exit 1
fi

echo "📦  Готовим staging..."
rm -rf "${STAGING_DIR}"
mkdir -p "${STAGING_DIR}"

# Копируем .app bundle
cp -R "${APP_BUNDLE}" "${STAGING_DIR}/"

# Симлинк на /Applications для drag-and-drop установки
ln -s /Applications "${STAGING_DIR}/Applications"

# Дополнительные файлы рядом с .app
cp README.md        "${STAGING_DIR}/README.txt"        2>/dev/null || true
cp config.template.json "${STAGING_DIR}/config.json"    2>/dev/null || true
cp mtproxy_seed.json  "${STAGING_DIR}/mtproxy_seed.json" 2>/dev/null || true
mkdir -p "${STAGING_DIR}/list"
cp list/proxy_list.txt "${STAGING_DIR}/list/" 2>/dev/null || true

echo "💿  Создаём ${FINAL_DMG}..."
rm -f "${FINAL_DMG}"
hdiutil create \
    -volname  "${VOLUME_NAME}" \
    -srcfolder "${STAGING_DIR}" \
    -ov \
    -format UDZO \
    -imagekey zlib-level=9 \
    "${FINAL_DMG}"

rm -rf "${STAGING_DIR}"

echo "✅  Готово: ${FINAL_DMG}"
echo ""
echo "Для снятия карантина перед тестом:"
echo "  xattr -dr com.apple.quarantine ${FINAL_DMG}"
