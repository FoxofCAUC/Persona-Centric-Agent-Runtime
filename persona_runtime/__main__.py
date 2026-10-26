"""Storage inspection and initialization only; no model calls yet."""

import argparse
import json

from .paths import RuntimePaths


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["paths", "init-storage"])
    args = parser.parse_args()
    paths = RuntimePaths.from_environment()
    if args.command == "init-storage":
        paths.initialize()
    print(json.dumps({k: str(v) for k, v in paths.directories().items()},
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
