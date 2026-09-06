#!/usr/bin/env bash
# Rebuild the isolated interpreter that runs moomoo's V2 screen.
#
# The weekly universe refresh (scripts/universe_refresh.py, screen_backend =
# "v2_turnover") shells out to scripts/v2_screen.py under THIS interpreter,
# because the V2 decoder needs protobuf < 5 while the rest of the box runs
# 7.35.1. See deploy/v2env-requirements.txt for the full reason.
#
# This exists so v2env is a REPRODUCIBLE build artefact rather than an
# unexplained hand-made directory: v2env/ is git-ignored, so a rebuilt droplet
# has no universe screen until this is run.
#
#   deploy/setup_v2env.sh          # build/refresh ./v2env
#
# Override the location with AGENTIC_V2_PYTHON if it must live elsewhere.
set -euo pipefail
cd "$(dirname "$0")/.."
PY=/usr/bin/python3          # 3.10 on this box; the SDK supports 3.8+
[ -d v2env ] || "$PY" -m venv v2env
./v2env/bin/pip install --quiet --upgrade pip
./v2env/bin/pip install --quiet -r deploy/v2env-requirements.txt
echo "v2env ready:"
./v2env/bin/python - <<'PYEOF'
import google.protobuf as p, moomoo, sys
print(f"  python {sys.version.split()[0]} | moomoo-api {moomoo.__version__} | protobuf {p.__version__}")
assert int(p.__version__.split('.')[0]) < 5, "protobuf must be < 5 or V2 screening fails"
print("  protobuf < 5 confirmed — get_stock_screen (V2) will decode")
PYEOF
