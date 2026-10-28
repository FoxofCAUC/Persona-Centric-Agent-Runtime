"""Administrator-only console shutdown helper; retain the requesting console."""
import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from persona_runtime.config import atomic_json
from persona_runtime.emergency import force_stop_related, is_administrator
from persona_runtime.paths import RuntimePaths


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--home', type=Path, required=True)
    parser.add_argument('--report', type=Path, required=True)
    parser.add_argument('--controller-pid', type=int, required=True)
    args = parser.parse_args()
    paths = RuntimePaths(args.home.resolve()); report_file = args.report.resolve()
    if report_file.parent != (paths.home/'logs').resolve() or not report_file.name.startswith('console-stop-'):
        parser.error('Invalid report location')
    if not is_administrator():
        parser.error('Administrator permission required')
    # Elevated children may be parented by the elevation broker. Preserve the
    # explicit initiating console as well as this helper's actual ancestors.
    report = force_stop_related(paths, protected_pids=(args.controller_pid,))
    report['elevated'] = True
    atomic_json(report_file, report)
    return 0 if report['success'] else 2


if __name__ == '__main__':
    raise SystemExit(main())
