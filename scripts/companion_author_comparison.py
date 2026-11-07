"""Stage 4-E.2 isolated author-revision comparison entry point."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from experiments.companion_author_comparison import main

if __name__ == '__main__':
    main()
