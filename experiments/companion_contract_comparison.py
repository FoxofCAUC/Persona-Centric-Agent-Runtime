"""Contract-language entry point over the existing isolated comparison runner.

The frozen experiment kind chooses the transformation, namespace and version.
No global constants, production policies or runtime functions are replaced.
"""
from __future__ import annotations

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from experiments import companion_order_comparison as runner
from persona_runtime.paths import DEFAULT_HOME

EXPERIMENT_KIND = 'contract_language'
VERSION = runner.CONTRACT_VERSION


async def prepare(source_home=DEFAULT_HOME, *, approve_current_provider=False, development_material=False):
    return await runner.prepare(source_home, approve_current_provider=approve_current_provider,
                                development_material=development_material, experiment_kind=EXPERIMENT_KIND)


def load_plan(root):
    return runner.load_plan(root, expected_kind=EXPERIMENT_KIND)


def verify(root):
    return runner.verify(root, expected_kind=EXPERIMENT_KIND)


async def run_position(root, position_id, *, fake=False, client=None):
    return await runner.run_position(root, position_id, fake=fake, client=client, expected_kind=EXPERIMENT_KIND)


def report(root):
    return runner.report(root, expected_kind=EXPERIMENT_KIND)


if __name__ == '__main__':
    runner.main(experiment_kind=EXPERIMENT_KIND)
