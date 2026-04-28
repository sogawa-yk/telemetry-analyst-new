#!/usr/bin/env bash
# 使用例: ./scripts/build_and_push.sh v0.2.0
# Docker イメージをビルドして OCIR へプッシュする.
# レジストリ: kix.ocir.io/nr3c2r62ocsa/telemetry-analyst/api[-ui]
set -euo pipefail

TAG="${1:?タグを指定してください (例: v0.2.0)}"

API_IMAGE="kix.ocir.io/nr3c2r62ocsa/telemetry-analyst/api:${TAG}"
UI_IMAGE="kix.ocir.io/nr3c2r62ocsa/telemetry-analyst/ui:${TAG}"

BUILD_FLAGS="${BUILD_FLAGS:-}"

echo "==> Building API image: ${API_IMAGE}"
docker build ${BUILD_FLAGS} -f Dockerfile    -t "${API_IMAGE}" .

echo "==> Building UI image: ${UI_IMAGE}"
docker build ${BUILD_FLAGS} -f Dockerfile.ui -t "${UI_IMAGE}"  .

echo "==> Pushing API image"
docker push "${API_IMAGE}"

echo "==> Pushing UI image"
docker push "${UI_IMAGE}"

echo "==> Done."
echo "    API: ${API_IMAGE}"
echo "    UI : ${UI_IMAGE}"
