#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="/workspace"
OUT_DIR="${ROOT_DIR}/dist/obfuscated-linux"
BUILD_DIR="${ROOT_DIR}/dist/.obfuscate-build"

rm -rf "${OUT_DIR}" "${BUILD_DIR}"
mkdir -p "${OUT_DIR}" "${BUILD_DIR}"

cd "${ROOT_DIR}"

pyarmor gen \
  --platform linux.x86_64 \
  --recursive \
  --output "${BUILD_DIR}" \
  bot.py \
  app

cp -R "${BUILD_DIR}/." "${OUT_DIR}/"

for path in prompts requirements.txt deploy.sh scripts/run.sh; do
  if [ -e "${path}" ]; then
    dest="${OUT_DIR}/${path}"
    mkdir -p "$(dirname "${dest}")"
    cp -R "${path}" "${dest}"
  fi
done

if [ -d "docs" ]; then
  cp -R "docs" "${OUT_DIR}/docs"
fi

echo "Obfuscated Linux build ready at: ${OUT_DIR}"
