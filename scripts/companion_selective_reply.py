"""Explicit isolated entry for stage 5-B.1."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from experiments.companion_selective_reply import main

if __name__ == '__main__':
    main()
