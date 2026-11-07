"""Explicit stage-5 continuation; never reset the original started marker."""
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from experiments.companion_stage5_continue import main

if __name__ == '__main__':
    main()
