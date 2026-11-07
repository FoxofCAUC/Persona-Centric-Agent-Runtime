"""Stage 4-E.3 isolated context-rule consumption comparison."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from experiments.companion_context_rules import main

if __name__ == '__main__':
    main()
