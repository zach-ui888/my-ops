#!/bin/bash
set -euo pipefail
cd -- "$(dirname -- "$0")/.."
export PYTHONDONTWRITEBYTECODE=1
export PYTHONPYCACHEPREFIX="$PWD/.test-work/pycache"
python3 -m compileall -q app scripts
python3 scripts/preflight.py
python3 scripts/test_preflight.py
python3 scripts/test_vpn_runtime.py
python3 scripts/static_checks.py
bash -n scripts/verify.sh
if [ -f reference/source/install.sh ]; then
    bash -n reference/source/install.sh
fi
python3 scripts/release_checks.py
