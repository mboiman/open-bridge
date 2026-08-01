import sys
from pathlib import Path

# skills/board-pilot is the import root → `import engine` resolves to engine/.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
