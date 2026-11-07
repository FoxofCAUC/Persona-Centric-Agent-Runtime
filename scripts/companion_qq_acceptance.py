"""Stage 5-C entry point. Only listen enables real QQ and generation."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from experiments.companion_qq_acceptance import main

if __name__ == '__main__':
    main()
