"""runtime — infrastructure that talks to a live Odoo/DB/docker (RFC §12).

A SIBLING of the engine, never a dependency of it (C7). Responsibilities:
rpc (XML-RPC/JSON-RPC), shell (drive `odoo shell`), docker/postgres (lifecycle,
DB create/drop/dump/restore). Composed — not owned — by API methods needing
both sides (diff_against_db, doctor). Lands in Phase 1 (wrappers) / Phase 6.

Note: modules here MAY import from odx.engine.contracts (shared data types), but
the engine must never import from here. test_arch/ asserts the one-way rule.
"""


def detect_runtime(project_root) -> str | None:
    """Best-effort detection of how Odoo is run for a project.

    Returns one of: "docker" | "venv" | "source" | "odoo.sh" | None.
    Used by `odx init` to pre-fill config; init always confirms interactively
    (RFC §21 — never silently assumes).
    """
    from pathlib import Path

    root = Path(project_root)

    # docker: a compose file mentioning an odoo service is the strongest signal.
    for name in ("docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml"):
        f = root / name
        if f.is_file():
            try:
                text = f.read_text(encoding="utf-8", errors="ignore").lower()
            except OSError:
                text = ""
            if "odoo" in text:
                return "docker"
            return "docker"

    # source checkout: odoo-bin at the root (or a cloned odoo/ dir).
    if (root / "odoo-bin").is_file() or (root / "odoo" / "odoo-bin").is_file():
        return "source"

    # venv: a virtualenv with odoo installed.
    for venv in (".venv", "venv", "env"):
        vdir = root / venv
        if vdir.is_dir():
            return "venv"

    # odoo.sh: the platform drops a .odoo.sh marker / config in some layouts.
    if (root / ".odoo.sh").exists() or (root / "odoo.sh").exists():
        return "odoo.sh"

    return None
