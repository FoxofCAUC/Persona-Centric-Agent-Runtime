# Persona decision prototype v1

Standard library only; importing it performs no application I/O. This is an explicit experiment, not a production integration, free-text persona compiler or naturalness evaluation.

From the repository root, using the existing Python environment:

```powershell
& 'D:\Persona-Centric Agent Runtime\.venv\Scripts\python.exe' -B -m experiments.persona_decision.cli validate --fixture experiments/persona_decision/fixtures.json --rules experiments/persona_decision/rules.json
& 'D:\Persona-Centric Agent Runtime\.venv\Scripts\python.exe' -B -m experiments.persona_decision.cli evaluate --fixture experiments/persona_decision/fixtures.json --rules experiments/persona_decision/rules.json
```

`--output <explicit-file>` creates a new UTF-8 JSON file and refuses existing output. No runtime home, current persona, configuration, database, model or network is read. `validate` checks the corpus and declared invalid-contract examples; it does not select actions. `evaluate` returns text, neutral-without-dispositions and fixture-frame diagnostics separately. It never calls the M7 model baseline.

`fixtures.json`: 32 artificial development examples plus 24 fixed contract cases. `author_sources.json`: artificial structured source material matching the fixture source hashes; no private or real creator canon. The rule JSON is pinned to v1's canonical hash. Editing rules under the same version is rejected.

Pure interfaces are `validate_input`, `interpret`, `select_dispositions`, `decide`, and `run_case`. Inputs and outputs are frozen dataclasses with tuple members. Raw text stays in DecisionInput; DecisionRecord includes only bounded codes, IDs, positions, contributions and fingerprints. Source identity supplied by an artificial fixture is not a substitute for a future authority adapter.

[Implementation contract](../../docs/M7.6.0-合同与隔离决策原型实施计划.md) · [Results, failures and limitations](../../docs/M7.6.0-开发记录与定向自检.md)
