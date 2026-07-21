#!/bin/zsh
set -euo pipefail

SCRIPT_DIR="${0:A:h}"
DESKTOP_DIR="${SCRIPT_DIR:h}"
VERSION="$(node -p "require('${DESKTOP_DIR}/package.json').version")"
BUNDLE_DIR="${DESKTOP_DIR}/src-tauri/target/release/bundle/macos"
APP="${BUNDLE_DIR}/ReelBrain.app"
ZIP="${BUNDLE_DIR}/ReelBrain-v${VERSION}-macos-arm64.zip"
PKG="${BUNDLE_DIR}/ReelBrain-v${VERSION}-macos-arm64.pkg"
ZIP_TMP="${BUNDLE_DIR}/.ReelBrain-v${VERSION}-macos-arm64.zip"
PKG_TMP="${BUNDLE_DIR}/.ReelBrain-v${VERSION}-macos-arm64.pkg"

cd "${DESKTOP_DIR}"
npm run tauri build -- --bundles app

codesign --verify --deep --strict --verbose=2 "${APP}"

ditto -c -k --sequesterRsrc --keepParent "${APP}" "${ZIP_TMP}"
mv -f "${ZIP_TMP}" "${ZIP}"

pkgbuild \
  --component "${APP}" \
  --install-location /Applications \
  --identifier dev.reelbrain.desktop \
  --version "${VERSION}" \
  "${PKG_TMP}"
mv -f "${PKG_TMP}" "${PKG}"

shasum -a 256 "${ZIP}" "${PKG}"
