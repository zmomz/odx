"""generators — module/model/field/view/... -> EditPlan (RFC §10, §16, §19 Phase 2).

Generators never write (C3): they *describe* edits as an ``EditPlan`` that a
client chooses to apply (CLI writes + prints; MCP proposes; LSP maps to a
WorkspaceEdit). They read the current state of the tree only through an
injected ``read: Callable[[Path], str | None]`` (None = file absent), so they
are pure and trivially testable against in-memory trees.

EditPlan semantics (the contract every applier implements)
----------------------------------------------------------
``FileEdit.file`` is the path to edit (absolute when the generator was given an
absolute module dir / addons path; an applier may resolve relative paths
against a root it chooses).

``EditKind.CREATE``  (``range`` is None)
    Create a *new* file whose full content is ``new_text`` (parent directories
    are created). Fail-safe: a generator never emits CREATE for a file that
    exists — it emits REPLACE — so an applier must refuse a CREATE whose target
    already exists (it means the tree changed since planning).

``EditKind.REPLACE`` with ``range=None``
    Replace the WHOLE file content with ``new_text``. This is how generators
    edit existing files (manifests, ``__init__.py``, ``ir.model.access.csv``,
    XML data files, model ``.py`` files): read the current text, compute the
    full new text with a conservative text transform, emit it. Simple, exact,
    and diffable by any client (unified diff of read(file) vs new_text).

``EditKind.REPLACE`` with a ``range``
    Replace the text spanning ``range`` (``Location``: 1-based lines, 0-based
    character columns; end exclusive) with ``new_text``.

``EditKind.INSERT`` (``range`` required)
    Insert ``new_text`` at ``(range.start_line, range.start_col)`` (1-based
    line, 0-based column). Generators avoid INSERT (they prefer whole-file
    REPLACE); it exists for future fixers / LSP code actions.

A plan holds at most one edit per file and edits are independent; applying them
in list order is always correct. Generators are idempotent: re-planning against
the result of an applied plan yields no duplicate imports, ACL rows, manifest
entries or XML records (often an empty plan).

Failure: a generator that cannot produce a *correct* plan raises
``GeneratorError`` (a ``ValueError``) with an action-oriented message.

Modules
-------
``fields``    the §16 field-type matrix, ``guess_type``, ``validate_spec``,
              ``render_field``.
``naming``    model name -> class / file / xml id helpers.
``scaffold``  ``module_plan``, ``model_plan``, ``field_plan``, ``view_plan``,
              ``menu_plan``, ``wizard_plan``, ``report_plan``, ``controller_plan``.
``base``      reader type, ``GeneratorError``, Jinja2 env, text editors.
"""

from odx.engine.generators.base import GeneratorError, ReadFn, disk_read
from odx.engine.generators.fields import (
    FIELD_MATRIX,
    guess_type,
    normalize_type,
    render_field,
    validate_spec,
)
from odx.engine.generators.scaffold import (
    ControllerSpec,
    ReportSpec,
    controller_plan,
    field_plan,
    menu_plan,
    model_plan,
    module_plan,
    report_plan,
    view_plan,
    wizard_plan,
)

__all__ = [
    "FIELD_MATRIX", "ControllerSpec", "GeneratorError", "ReadFn", "ReportSpec",
    "controller_plan", "disk_read", "field_plan", "guess_type", "menu_plan",
    "model_plan", "module_plan", "normalize_type", "render_field", "report_plan",
    "validate_spec", "view_plan", "wizard_plan",
]
