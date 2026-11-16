"""Create a protected schema-28 compatibility database after normal shutdown."""
import argparse,json,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from persona_runtime.paths import RuntimePaths
from persona_runtime.recall_rollback import prepare_snapshot

def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--home',required=True)
    args=parser.parse_args();print(json.dumps(prepare_snapshot(RuntimePaths(Path(args.home).resolve())),ensure_ascii=False,indent=2))

if __name__=='__main__':main()
