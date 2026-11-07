"""Explicit stage-5 entrypoint, isolated from normal application startup."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from experiments.companion_stage5 import main

if __name__ == '__main__':
    main()
