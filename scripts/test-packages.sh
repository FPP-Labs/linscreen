#!/usr/bin/env bash
# Install dist/*.deb and dist/*.rpm into clean containers and smoke-test them:
# the package installs with its dependencies, the app starts (offscreen) and
# the unit tests pass against the distro's Python/PyQt6.
set -euo pipefail

cd "$(dirname "$0")/.."
ENGINE=${CONTAINER_ENGINE:-$(command -v podman || command -v docker)}
DEB_IMAGES=${DEB_IMAGES:-"debian:12 ubuntu:24.04"}
RPM_IMAGES=${RPM_IMAGES:-"fedora:41"}

smoke='
set -e
python3 --version
python3 -c "import PyQt6.QtCore as c; print(\"PyQt6\", c.PYQT_VERSION_STR)"
python3 -c "import ast; ast.parse(open(\"/usr/bin/linscreen\").read())"
export QT_QPA_PLATFORM=offscreen HOME=/root XDG_RUNTIME_DIR=/tmp/rt
mkdir -p /tmp/rt && chmod 700 /tmp/rt
set +e; timeout 6 linscreen >/tmp/app.log 2>&1; code=$?; set -e
if grep -q Traceback /tmp/app.log || [ "$code" != 124 ]; then cat /tmp/app.log; echo "app exited with $code"; exit 1; fi
echo "app: running OK"
cp -r /src/tests /src/linscreen.py /tmp/ && cd /tmp && python3 -m unittest discover tests 2>&1 | tail -3
'

status=0
for image in $DEB_IMAGES; do
    echo "=== $image"
    "$ENGINE" run --rm -v "$PWD":/src:ro "$image" bash -c "
        export DEBIAN_FRONTEND=noninteractive
        apt-get update -qq >/dev/null && apt-get install -y -qq /src/dist/linscreen_*_all.deb >/dev/null
        dpkg -s linscreen | grep -E '^(Status|Version)'
        $smoke" || status=1
done
for image in $RPM_IMAGES; do
    echo "=== $image"
    "$ENGINE" run --rm -v "$PWD":/src:ro "$image" bash -c "
        dnf install -y -q /src/dist/linscreen-*.noarch.rpm >/dev/null
        rpm -q linscreen
        $smoke" || status=1
done
exit $status
