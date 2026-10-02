# pytest wiring: STREAMCTL_CONF points at the shared conf fixture before any
# cf_router_local import, so tests exercise the exact Backend B conf contract.
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[2]))  # streamctl/ — imports cf_router_local
os.environ.setdefault(
    "STREAMCTL_CONF", str(HERE.parent.parent / "fixtures" / "streamctl.conf")
)
