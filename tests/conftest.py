# Ajoute src/ au PYTHONPATH pour que les imports `metadata_scrubber.*` fonctionnent.
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
