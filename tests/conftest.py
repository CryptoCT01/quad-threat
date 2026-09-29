import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
AG = ROOT / "agents" / "quad_threat_orchestrator"
for p in (AG, ROOT):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))
