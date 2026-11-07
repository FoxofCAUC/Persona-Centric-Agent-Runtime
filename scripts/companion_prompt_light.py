"""Stage 4-E.5 artificial prompt-bundle comparison entry point."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from experiments.companion_prompt_light import main

if __name__ == '__main__':
    main()
