"""Re-evaluate one saved role-card response locally and archive its previous projection."""
import argparse
import json
from pathlib import Path

from persona_runtime.config import ConfigStore
from persona_runtime.corpus_store import outside_repository
from persona_runtime.import_card_job import CardJobs
from persona_runtime.import_library import ImportLibrary
from persona_runtime.paths import RuntimePaths


def readmit(home, document, previous_sha256):
    paths = RuntimePaths(outside_repository(home))
    jobs = CardJobs(ImportLibrary(paths), ConfigStore(paths))
    return jobs.readmit(document, previous_sha256)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", type=Path, required=True)
    parser.add_argument("--document", required=True)
    parser.add_argument("--previous-sha256", required=True)
    args = parser.parse_args()
    audit = readmit(args.home, args.document, args.previous_sha256)
    print(json.dumps(audit, ensure_ascii=False))
