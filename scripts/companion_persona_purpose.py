"""Owned stage 4-E replay entry point."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from experiments.companion_persona_purpose import main

if __name__ == '__main__':
    main()
