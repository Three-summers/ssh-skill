import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
LIB = SCRIPTS / "lib"

for path in (SCRIPTS, LIB):
    value = str(path)
    if value not in sys.path:
        sys.path.insert(0, value)
