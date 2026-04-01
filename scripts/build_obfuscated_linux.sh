#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
IMAGE_NAME="osbot-obfuscate:latest"
TARGET_DOCKER_PLATFORM="linux/amd64"

docker build \
  --platform "${TARGET_DOCKER_PLATFORM}" \
  -f "${ROOT_DIR}/Dockerfile.obfuscate" \
  -t "${IMAGE_NAME}" \
  "${ROOT_DIR}"

docker run --rm \
  --platform "${TARGET_DOCKER_PLATFORM}" \
  -v "${ROOT_DIR}:/workspace" \
  "${IMAGE_NAME}"
