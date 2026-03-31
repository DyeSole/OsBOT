#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
IMAGE_NAME="osbot-obfuscate:latest"

docker build \
  -f "${ROOT_DIR}/Dockerfile.obfuscate" \
  -t "${IMAGE_NAME}" \
  "${ROOT_DIR}"

docker run --rm \
  -v "${ROOT_DIR}:/workspace" \
  "${IMAGE_NAME}"
