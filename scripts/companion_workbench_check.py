"""Run the artificial-only normal-workbench acceptance fixture."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from experiments.companion_workbench_check import main

if __name__ == '__main__':
    main()
