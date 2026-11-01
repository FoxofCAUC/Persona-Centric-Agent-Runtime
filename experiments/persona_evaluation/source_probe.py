"""Explicit static inspection of the production adapter; never opens runtime DBs."""
import ast
import hashlib
from pathlib import Path

from .contracts import readiness


def inspect_adapter(path):
    path = Path(path)
    data = path.read_bytes()
    tree = ast.parse(data)
    cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == 'ShadowService')
    method = next(node for node in cls.body if isinstance(node, ast.FunctionDef) and node.name == '_compute')
    nodes = [node for node in ast.walk(method) if isinstance(node, ast.Assign)
             and any(isinstance(target, ast.Name) and target.id == 'raw' for target in node.targets)]
    report = {'adapter': 'ShadowService._compute', 'sha256': hashlib.sha256(data).hexdigest(),
              'method_line': method.lineno, 'inspection': 'static_literal_assignment_not_runtime_inference'}
    if len(nodes) != 1 or not isinstance(nodes[0].value, ast.Dict):
        return report | {'ready': False, 'allow_paid_generation': False, 'diagnostic': 'adapter_contract_requires_review'}
    context = next((v for k, v in zip(nodes[0].value.keys, nodes[0].value.values)
                    if isinstance(k, ast.Constant) and k.value == 'context'), None)
    if context is None:
        return report | {'ready': False, 'allow_paid_generation': False, 'diagnostic': 'adapter_contract_requires_review'}
    if not isinstance(context, ast.Dict):
        return report | {'ready': False, 'allow_paid_generation': False, 'diagnostic': 'dynamic_adapter_needs_integration_evidence'}
    try:
        observed = ast.literal_eval(context)
    except (ValueError, TypeError):
        # A future dynamic adapter needs evidence from its own integration probe.
        return report | {'ready': False, 'allow_paid_generation': False, 'diagnostic': 'dynamic_adapter_needs_integration_evidence'}
    capabilities = {key: [value] for key, value in observed.items()}
    return report | {'context_line': context.lineno, 'fixed_context': observed,
                     'capabilities': capabilities, **readiness(capabilities)}
