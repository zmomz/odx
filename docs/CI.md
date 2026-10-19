# odx in CI

`odx check` is the CI client (RFC §13): the same `Diagnostic` that is a CLI row
and an MCP item becomes a PR annotation. No server, no database.

```bash
odx check --format github      # GitHub Actions annotations on the PR diff
odx check --format sarif > odx.sarif   # GitHub code scanning / any SARIF consumer
odx check --format json        # anything else
odx check --fail-on warning    # exit 1 at/above this severity (default: error)
```

Context paths matter: point `.odx.toml` at the Odoo source and your OCA
dependencies so `_inherit` chains and xml-ids close; anything the engine cannot
see resolves DYNAMIC and is never accused.

```toml
[odoo]
version = "17.0"
addons_paths = ["./addons"]          # your modules — the ones that get checked
source_path = "./.odoo"              # odoo/odoo checkout (context only)
context_paths = ["./.oca/web"]       # OCA dependencies (context only)
```

## GitHub Actions

```yaml
name: odx
on: [pull_request]
jobs:
  check:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/checkout@v4
        with: {repository: odoo/odoo, ref: "17.0", path: .odoo, fetch-depth: 1}
      - uses: actions/setup-python@v5
        with: {python-version: "3.11"}
      - run: pip install "odx @ git+https://github.com/zmomz/odx"
      - run: odx check --format github           # inline annotations
      - run: odx check --format sarif > odx.sarif
        if: always()
      - uses: github/codeql-action/upload-sarif@v3
        if: always()
        with: {sarif_file: odx.sarif}
```

`odx report --format json` gives a score/grade to track over time;
`odx upgrade check --to 18.0 --format github` annotates migration work on a
porting branch. Auto-PRs from `odx fix` / `odx upgrade apply` are `EditPlan`s:
run them in a job and open a PR with your usual action (the plan is applied by
the client; the engine never writes).
