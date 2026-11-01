# M7.6.3 evaluation preparation

Evaluation-only standard-library helpers. Production does not import this package. No model, network, runtime database or instance lock is accessed on import.

`development_contracts.json` contains 24 synthetic development contracts (8 finite-category ranking, 8 readiness, 8 metric boundary cases). It is not the independent 100-scene set. Labels were saved before evaluation; the CLI writes a fixture SHA256 freeze before calculating observed results.

`contracts.py` ranks observed situation categories using explicit evidence, member/character position and fixed category order. It does not change action eligibility, scores or production interpretation. Scores are not confidence probabilities. Fixed metric denominators are 40/20/100 as adopted; ties remain in the 100-review denominator.

`source_probe.py` explicitly inspects the current production `_compute` context assignment. Literal assignments show the actually available values. Dynamic or unknown code fails closed pending integration evidence; a hypothetical capability dictionary in a development test is never a production capability declaration.

Run `scripts/check_m763_readiness.py --output <new-file-in-an-existing-D-evidence-folder>`. Output files are exclusive and never overwrite old evidence. Exit 2 means paid evaluation is not allowed; passing the 24 development contracts does not override a failed readiness gate.

Run `scripts/probe_m763_context.py <new-D-folder>` only for a necessary adapter integration check. It creates artificial author sources, confirms artificial projections and reads zero-write Shadow previews. It does not compile with a model or create conversations. Existing successful probes must be reused until relevant code changes.

Current result: readiness is false. Production relation is fixed neutral and information_gap false, so required relation/uncertainty counterfactuals cannot be represented faithfully. The independent set, three-arm runner, blind-review UI and Live release have not been implemented or executed. See [acceptance and handoff](../../docs/acceptance/M7.6-独立三臂与人工盲审结论.md).
