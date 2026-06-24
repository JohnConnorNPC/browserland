import sys
from pathlib import Path

# Run tests against the in-tree package without installation.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
