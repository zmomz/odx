# Phase 0.5 — Resolver Spike

A minimal, rough resolver pointed at real Odoo modules to validate the RFC's
central bet (§2, §3): *can we statically reproduce enough of Odoo's semantics
without booting Odoo?* It produces a **scoreboard** (orientation) and a **tagged
failure catalog** (the real deliverable), and it is kept permanently as a
re-runnable correctness harness (RFC §22).

**→ Read [RESULTS.md](RESULTS.md) (Python side) and [XML_RESULTS.md](XML_RESULTS.md)
(XML view-inheritance side) for the findings and the GO decisions.**

Two halves, both validated:
- **Phase 0.5** — model / field / `_inherit` resolution (`run.py`).
- **Phase 0.6** — XML view inheritance: `inherit_id` xml-ids + `xpath`/`position`
  locators (`run_xml.py`).

This is *not* the Phase 3 engine. It graduates into `odx/engine/` once the
approach is validated — which it now is, for both halves.

## Layout

| file | role |
|---|---|
| `parser.py` | `.py` file → raw Odoo model/field defs via `ast`; flags dynamic patterns; never crashes on a file |
| `resolver.py` | raw defs → registry; resolves `_inherit` / `_inherits` / effective fields |
| `catalog.py` | failure tagging (`fixable` / `hard` / `out-of-scope` / `boundary`) |
| `run.py` | the harness: walk corpus → scoreboard + catalog → `out/` |
| `test_spike.py` | regression test: asserts thresholds hold (skips if no corpus) |
| `out/` | generated: `scoreboard.json`, `catalog.md`, `failures.jsonl` (gitignored) |

## Run it

Clone a corpus into `tests/corpus/` (gitignored):

```bash
cd tests/corpus
git clone --depth 1 -b 17.0 https://github.com/odoo/odoo.git odoo
git clone --depth 1 -b 17.0 https://github.com/OCA/server-tools.git server-tools
git clone --depth 1 -b 17.0 https://github.com/OCA/account-financial-tools.git account-financial-tools
git clone --depth 1 -b 17.0 https://github.com/OCA/web.git web
```

Then, from the repo root:

```bash
python -m tests.spike.run                 # default corpus: tests/corpus
python -m tests.spike.run /path/to/addons # any addons tree
```

Output is written to `tests/spike/out/` and summarised on stdout.

## As a regression harness

`pytest tests/spike/test_spike.py` re-runs the spike and asserts the resolution
thresholds still hold and the out-of-scope set stays bounded. It **skips**
cleanly when no corpus is present, so CI without the corpus stays green.
