#!/usr/bin/env bash
# Build .deb and .rpm packages into dist/ using nfpm in a container.
# Usage: scripts/build-packages.sh            (needs docker or podman)
#        NFPM_IMAGE=goreleaser/nfpm:vX.Y.Z scripts/build-packages.sh
set -euo pipefail

cd "$(dirname "$0")/.."
VERSION=$(sed -n 's/^APP_VERSION *= *"\(.*\)"/\1/p' linscreen.py)
[ -n "$VERSION" ] || { echo "APP_VERSION not found in linscreen.py" >&2; exit 1; }
ENGINE=${CONTAINER_ENGINE:-$(command -v podman || command -v docker)}
IMAGE=${NFPM_IMAGE:-goreleaser/nfpm:v2.40.0}

mkdir -p dist
for packager in deb rpm; do
    "$ENGINE" run --rm -e VERSION="$VERSION" -v "$PWD":/src -w /src "$IMAGE" \
        package --config nfpm.yaml --packager "$packager" --target dist/
done
ls -l dist/
