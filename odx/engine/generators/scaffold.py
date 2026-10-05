"""Scaffolding generators (RFC §19 Phase 2): spec -> EditPlan. Pure functions.

Each generator takes a spec, the target module directory where relevant, and an
injected ``read`` (see ``odx.engine.generators`` for the EditPlan semantics).
None of them touches disk; they raise ``GeneratorError`` when no correct plan
exists. All of them are idempotent: re-planning against the result of applying
a plan yields no duplicate imports, ACL rows, manifest entries or records.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, replace
from pathlib import Path

from odx.engine.contracts import (
    EditPlan,
    FieldSpec,
    FileEdit,
    MenuSpec,
    ModelSpec,
    ModuleSpec,
    ViewSpec,
)
from odx.engine.generators.base import (
    ACCESS_CSV_HEADER,
    GeneratorError,
    Overlay,
    ReadFn,
    access_row,
    csv_add_row,
    init_add_import,
    manifest_add_data,
    put,
    render,
    xml_append,
    xml_fragment,
    xml_has_id,
)
from odx.engine.generators.fields import (
    X2MANY_TYPES,
    has_errors,
    normalize_type,
    render_compute_stub,
    render_field,
    validate_spec,
)
from odx.engine.generators.naming import (
    class_name,
    humanize,
    is_identifier,
    is_model_name,
    model_xmlid,
    slug,
    snake,
)

MODEL_KINDS = ("Model", "TransientModel", "AbstractModel")
SECURITY_CSV = "security/ir.model.access.csv"
MANIFEST = "__manifest__.py"
DEFAULT_VERSION = "17.0"


# --------------------------------------------------------------------------- #
# extra specs for generators the core contracts don't (yet) name
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class ReportSpec:
    name: str                      # e.g. "book_card" -> report/book_card_report.xml
    module: str
    model: str                     # the report's model (docs)
    title: str = ""


@dataclass(frozen=True)
class ControllerSpec:
    name: str                      # e.g. "library" -> LibraryController
    module: str
    route: str | None = None       # default "/<module>/<name>"
    auth: str = "user"             # user | public | none


# --------------------------------------------------------------------------- #
# version-dependent conventions
# --------------------------------------------------------------------------- #
def _major(version: str | None) -> int:
    try:
        return int(str(version or DEFAULT_VERSION).split(".")[0])
    except ValueError:
        return int(DEFAULT_VERSION.split(".")[0])


def list_tag(version: str | None = DEFAULT_VERSION) -> str:
    """``<tree>`` up to Odoo 17, ``<list>`` from 18."""
    return "list" if _major(version) >= 18 else "tree"


def _view_mode(version: str | None) -> str:
    return f"{list_tag(version)},form"


def _view_id(model: str, kind: str) -> str:
    return f"view_{snake(model)}_{kind}"


def _action_id(model: str) -> str:
    return f"action_{snake(model)}"


def _require_module(module_dir: Path, read: ReadFn) -> str:
    text = read(module_dir / MANIFEST)
    if text is None:
        raise GeneratorError(f"{module_dir} is not an Odoo module (no {MANIFEST})")
    return text


def _field_errors(specs: tuple[FieldSpec, ...] | list[FieldSpec]) -> None:
    msgs = []
    for f in specs:
        diags = validate_spec(f)
        if has_errors(diags):
            msgs += [f"{f.name}: {d.message} [{d.code}]" for d in diags
                     if d.severity.value == "error"]
    if msgs:
        raise GeneratorError("invalid field spec:\n  " + "\n  ".join(msgs))


def _ensure_manifest(edits: list[FileEdit], module_dir: Path, ov: Overlay,
                     entries: list[str]) -> None:
    if not entries:
        return
    path = module_dir / MANIFEST
    ov.put(edits, path, manifest_add_data(_require_module(module_dir, ov), entries))


def _ensure_import(edits: list[FileEdit], init: Path, ov: Overlay, name: str) -> None:
    ov.put(edits, init, init_add_import(ov(init), name))


# =========================================================================== #
# module
# =========================================================================== #
def module_plan(spec: ModuleSpec, read: ReadFn) -> EditPlan:
    """A new, installable, empty module under ``spec.addons_path``."""
    if not is_identifier(spec.name):
        raise GeneratorError(f"module name '{spec.name}' must be a Python identifier "
                             f"(snake_case)")
    module_dir = Path(spec.addons_path) / spec.name
    if read(module_dir / MANIFEST) is not None or read(module_dir / "__openerp__.py") is not None:
        raise GeneratorError(f"module '{spec.name}' already exists at {module_dir}")
    title = humanize(spec.name)
    edits: list[FileEdit] = []
    put(edits, module_dir / "__init__.py", "from . import models\n", read)
    put(edits, module_dir / MANIFEST,
        render("manifest.py.j2", spec=spec, title=title, data=[SECURITY_CSV]), read)
    put(edits, module_dir / "models" / "__init__.py", "", read)
    put(edits, module_dir / "security" / "ir.model.access.csv", ACCESS_CSV_HEADER + "\n", read)
    put(edits, module_dir / "README.rst", render("readme.rst.j2", spec=spec, title=title), read)
    put(edits, module_dir / "static" / "description" / "index.html",
        render("description.html.j2", spec=spec, title=title), read)
    return EditPlan(edits=edits, summary=f"new module '{spec.name}' in {spec.addons_path}")


# =========================================================================== #
# model / wizard
# =========================================================================== #
def _render_model_file(spec: ModelSpec, fields: list[FieldSpec], inherit_only: bool,
                       extra_methods: list[str]) -> str:
    stubs = [render_compute_stub(f, "    ") for f in fields if f.compute]
    needs_api = any(f.compute and f.depends for f in fields)
    if inherit_only:
        inherit: object = spec.inherit[0] if len(spec.inherit) == 1 else list(spec.inherit)
    else:
        inherit = list(spec.inherit) if spec.inherit else None
    return render(
        "model.py.j2",
        imports=(["api"] if needs_api else []) + ["fields", "models"],
        cls=class_name(spec.name),
        kind=spec.kind,
        name=None if inherit_only else spec.name,
        description=None if inherit_only else (spec.description or humanize(spec.name)),
        inherit=inherit,
        fields_src="".join(render_field(f, "    ") for f in fields),
        methods=stubs + extra_methods,
    )


_WIZARD_CONFIRM = (
    "    def action_confirm(self):\n"
    "        self.ensure_one()\n"
    "        # TODO: implement what the wizard does\n"
    "        return {'type': 'ir.actions.act_window_close'}\n"
)


def _model_plan(spec: ModelSpec, module_dir: Path, read: ReadFn, version: str,
                subdir: str, wizard: bool) -> EditPlan:
    if not is_model_name(spec.name):
        raise GeneratorError(f"'{spec.name}' is not a valid model name (e.g. library.book)")
    if spec.kind not in MODEL_KINDS:
        raise GeneratorError(f"model kind must be one of {', '.join(MODEL_KINDS)}")
    _require_module(module_dir, read)
    fields = [f if f.model == spec.name else replace(f, model=spec.name) for f in spec.fields]
    _field_errors(fields)

    inherit_only = bool(spec.inherit) and spec.name == spec.inherit[0]
    concrete = not inherit_only and spec.kind != "AbstractModel"
    if not fields and concrete and not wizard:
        fields = [FieldSpec(model=spec.name, name="name", type="Char", required=True)]

    ov = Overlay(read)
    edits: list[FileEdit] = []
    x = snake(spec.name)
    notes: list[str] = []

    pyfile = module_dir / subdir / f"{x}.py"
    if ov(pyfile) is None:
        ov.put(edits, pyfile, _render_model_file(
            spec, fields, inherit_only, [_WIZARD_CONFIRM] if wizard else []))
    else:
        notes.append(f"{subdir}/{x}.py exists (left untouched)")
    _ensure_import(edits, module_dir / subdir / "__init__.py", ov, x)
    _ensure_import(edits, module_dir / "__init__.py", ov, subdir)

    data: list[str] = []
    if concrete and spec.with_access:
        csv_path = module_dir / "security" / "ir.model.access.csv"
        ov.put(edits, csv_path, csv_add_row(ov(csv_path), access_row(spec.name)))
        data.append(SECURITY_CSV)
    if concrete and spec.with_views:
        rel = f"{'wizard' if wizard else 'views'}/{x}_views.xml"
        vpath = module_dir / rel
        cur = ov(vpath)
        title = spec.description or humanize(spec.name)
        flds = [(f.name, normalize_type(f.type) or f.type) for f in fields]
        frags: list[tuple[str, str]] = []
        transient = spec.kind == "TransientModel"
        frags.append((_view_id(spec.name, "form"), xml_fragment(
            "form_view", _view_id(spec.name, "form"), spec.name, title, flds,
            wizard=transient, confirm="action_confirm" if wizard else None)))
        if not transient:
            tag = list_tag(version)
            frags.append((_view_id(spec.name, tag), xml_fragment(
                "list_view", _view_id(spec.name, tag), spec.name, title, flds, tag)))
            frags.append((_view_id(spec.name, "search"), xml_fragment(
                "search_view", _view_id(spec.name, "search"), spec.name, title, flds)))
        frags.append((_action_id(spec.name), xml_fragment(
            "action", _action_id(spec.name), title, spec.name,
            "form" if transient else _view_mode(version),
            target="new" if transient else None)))
        if spec.with_menu and not transient:
            frags.append((f"menu_{x}", xml_fragment(
                "menuitem", f"menu_{x}", title, None, _action_id(spec.name))))
        new = [f for rid, f in frags if not xml_has_id(cur, rid)]
        if new:
            ov.put(edits, vpath, xml_append(cur, new))
        data.append(rel)
    _ensure_manifest(edits, module_dir, ov, data)

    what = "wizard" if wizard else ("extension of" if inherit_only else "model")
    summary = f"{what} '{spec.name}' in {module_dir.name}"
    if notes:
        summary += " (" + "; ".join(notes) + ")"
    return EditPlan(edits=edits, summary=summary)


def model_plan(spec: ModelSpec, module_dir: Path, read: ReadFn,
               version: str = DEFAULT_VERSION) -> EditPlan:
    """A model in ``models/<snake>.py`` plus its import, ACL row, views, action,
    menu and manifest entries. Inherit-only specs (``name == inherit[0]``) get
    just the ``_inherit`` class; AbstractModels get no ACL/views."""
    return _model_plan(spec, Path(module_dir), read, version, "models", wizard=False)


def wizard_plan(spec: ModelSpec, module_dir: Path, read: ReadFn,
                version: str = DEFAULT_VERSION) -> EditPlan:
    """A TransientModel in ``wizard/<snake>.py`` with ``action_confirm``, a
    footer-button form, an ``target=new`` action, ACL row and manifest entries."""
    spec = replace(spec, kind="TransientModel", with_menu=False)
    return _model_plan(spec, Path(module_dir), read, version, "wizard", wizard=True)


# =========================================================================== #
# field
# =========================================================================== #
def _is_field_assign(stmt: ast.stmt) -> bool:
    if not isinstance(stmt, ast.Assign) or not isinstance(stmt.value, ast.Call):
        return False
    fn = stmt.value.func
    return isinstance(fn, ast.Attribute) and isinstance(fn.value, ast.Name) \
        and fn.value.id == "fields"


def _assigned_names(cls: ast.ClassDef) -> set[str]:
    out = set()
    for s in cls.body:
        if isinstance(s, ast.Assign):
            out |= {t.id for t in s.targets if isinstance(t, ast.Name)}
        elif isinstance(s, ast.AnnAssign) and isinstance(s.target, ast.Name):
            out.add(s.target.id)
    return out


def _class_attr_strs(cls: ast.ClassDef, attr: str) -> list[str]:
    for s in cls.body:
        if isinstance(s, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == attr for t in s.targets):
            v = s.value
            if isinstance(v, ast.Constant) and isinstance(v.value, str):
                return [v.value]
            if isinstance(v, (ast.List, ast.Tuple)):
                return [e.value for e in v.elts
                        if isinstance(e, ast.Constant) and isinstance(e.value, str)]
    return []


def find_model_class(tree: ast.Module, model: str,
                     hint: str | int | None = None) -> ast.ClassDef:
    """Locate the class contributing to ``model``: by explicit class name or line
    (``hint``), else ``_name == model``, else ``_inherit`` naming it."""
    classes = [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]
    if isinstance(hint, int):
        for c in classes:
            if c.lineno <= hint <= (c.end_lineno or c.lineno):
                return c
        raise GeneratorError(f"no class at line {hint}")
    if isinstance(hint, str) and hint:
        for c in classes:
            if c.name == hint:
                return c
        raise GeneratorError(f"no class named {hint}")
    for c in classes:
        if model in _class_attr_strs(c, "_name"):
            return c
    for c in classes:
        if not _class_attr_strs(c, "_name") and model in _class_attr_strs(c, "_inherit")[:1]:
            return c
    raise GeneratorError(f"no class for model '{model}' (by _name or _inherit)")


def _insert_lines(text: str, after_line: int, chunk: str) -> str:
    lines = text.splitlines(keepends=True)
    if after_line > 0 and lines and not lines[after_line - 1].endswith("\n"):
        lines[after_line - 1] += "\n"
    lines[after_line:after_line] = [chunk]
    return "".join(lines)


def _ensure_api_import(text: str) -> str:
    tree = ast.parse(text)
    odoo_imp = None
    last_import = None
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            last_import = node
            if isinstance(node, ast.ImportFrom) and node.module == "odoo" and node.level == 0:
                if any(a.name == "api" and not a.asname for a in node.names):
                    return text
                odoo_imp = odoo_imp or node
    if odoo_imp is not None and odoo_imp.lineno == odoo_imp.end_lineno \
            and odoo_imp.col_offset == 0:
        lines = text.splitlines(keepends=True)
        line = lines[odoo_imp.lineno - 1]
        if line.strip().startswith("from odoo import") and ";" not in line \
                and "#" not in line and "(" not in line:
            names = [a.name + (f" as {a.asname}" if a.asname else "") for a in odoo_imp.names]
            names = sorted(set(names) | {"api"})
            lines[odoo_imp.lineno - 1] = f"from odoo import {', '.join(names)}\n"
            return "".join(lines)
    after = (last_import.end_lineno if last_import is not None else 0)
    return _insert_lines(text, after, "from odoo import api\n")


def add_field_to_source(text: str, spec: FieldSpec, hint: str | int | None = None) -> str:
    """Pure text transform: insert ``spec``'s field (and compute stub, and the
    ``api`` import when needed) into the class for ``spec.model``."""
    try:
        tree = ast.parse(text)
    except SyntaxError as e:
        raise GeneratorError(f"cannot parse model file: {e.msg} (line {e.lineno})")
    cls = find_model_class(tree, spec.model, hint)
    if spec.name in _assigned_names(cls):
        raise GeneratorError(f"field '{spec.name}' already exists on class {cls.name}")
    if spec.compute and any(isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef))
                            and s.name == spec.compute for s in cls.body):
        stub_needed = False
    else:
        stub_needed = bool(spec.compute)

    indent = " " * cls.body[0].col_offset
    first_fn = next((i for i, s in enumerate(cls.body)
                     if isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))),
                    None)
    attrs = cls.body if first_fn is None else cls.body[:first_fn]
    # a lone docstring / `pass` doesn't count as an attribute block
    attrs = [s for s in attrs if not (isinstance(s, ast.Expr)
                                      and isinstance(s.value, ast.Constant))
             and not isinstance(s, ast.Pass)]
    field_src = render_field(spec, indent)
    if attrs:
        anchor = attrs[-1].end_lineno
        sep = "" if _is_field_assign(attrs[-1]) else "\n"
        chunk = sep + field_src
    elif first_fn is not None:
        fn = cls.body[first_fn]
        start = min([fn.lineno] + [d.lineno for d in getattr(fn, "decorator_list", [])])
        anchor = start - 1
        chunk = field_src + "\n"
    else:
        anchor = cls.body[-1].end_lineno
        chunk = field_src
    text = _insert_lines(text, anchor, chunk)

    if stub_needed:
        cls = find_model_class(ast.parse(text), spec.model, cls.name)
        text = _insert_lines(text, cls.end_lineno or cls.lineno,
                             "\n" + render_compute_stub(spec, indent))
        if spec.depends:
            text = _ensure_api_import(text)
    return text


def field_plan(spec: FieldSpec, model_file: Path, class_name_or_line: str | int | None,
               read: ReadFn) -> EditPlan:
    """REPLACE ``model_file`` with the field inserted after the last attribute
    of the target class (before its first method), plus a compute stub."""
    _field_errors([spec])
    text = read(model_file)
    if text is None:
        raise GeneratorError(f"model file {model_file} does not exist")
    new = add_field_to_source(text, spec, class_name_or_line)
    edits: list[FileEdit] = []
    put(edits, Path(model_file), new, read, current=text, known=True)
    return EditPlan(edits=edits,
                    summary=f"field '{spec.name}' ({normalize_type(spec.type)}) on "
                            f"'{spec.model}' in {Path(model_file).name}")


# =========================================================================== #
# view
# =========================================================================== #
_KIND_ALIASES = {"tree": "list"}
VIEW_KINDS = ("form", "list", "kanban", "search")


def _inherit_expr(kind: str, version: str) -> tuple[str, str]:
    if kind == "form":
        return "//sheet", "inside"
    if kind == "list":
        return f"//{list_tag(version)}", "inside"
    if kind == "kanban":
        return "//templates", "before"
    return "//search", "inside"


def view_plan(spec: ViewSpec, module_dir: Path, read: ReadFn,
              fields: list[tuple[str, str]], version: str = DEFAULT_VERSION) -> EditPlan:
    """A view record (or an inheriting one when ``spec.inherit_xmlid``) in
    ``views/<snake>_views.xml``, created or extended, plus the manifest entry.

    ``fields`` is the model's ``(name, type)`` list the engine knows; when
    ``spec.fields`` is given it selects (and orders) from it."""
    module_dir = Path(module_dir)
    _require_module(module_dir, read)
    kind = _KIND_ALIASES.get(spec.kind.lower(), spec.kind.lower())
    if kind not in VIEW_KINDS:
        raise GeneratorError(f"view kind must be one of {', '.join(VIEW_KINDS)}")
    if not is_model_name(spec.model or ""):
        raise GeneratorError(f"'{spec.model}' is not a valid model name")
    x = snake(spec.model)
    rel = f"views/{x}_views.xml"
    path = module_dir / rel
    ov = Overlay(read)
    cur = ov(path)
    title = humanize(spec.model)

    if spec.inherit_xmlid:
        ref = spec.inherit_xmlid
        rid = f"{snake(ref)}_inherit_{kind}"
        expr, position = _inherit_expr(kind, version)
        frag = xml_fragment("inherit_view", rid, spec.model, ref, expr, position,
                            list(spec.fields))
        what = f"inherited {kind} view of {ref}"
    else:
        known = dict(fields)
        if spec.fields:
            sel = [(n, known.get(n, "")) for n in spec.fields]
        else:
            sel = [(n, t) for n, t in fields if n not in ("id", "display_name")]
        tag = list_tag(version) if kind == "list" else kind
        rid = _view_id(spec.model, tag)
        if kind == "form":
            frag = xml_fragment("form_view", rid, spec.model, title, sel)
        elif kind == "list":
            frag = xml_fragment("list_view", rid, spec.model, title, sel, tag)
        elif kind == "search":
            frag = xml_fragment("search_view", rid, spec.model, title, sel)
        else:
            frag = xml_fragment("kanban_view", rid, spec.model, title, sel,
                                _major(version) >= 18)
        what = f"{kind} view for '{spec.model}'"
    if xml_has_id(cur, rid):
        raise GeneratorError(f"view '{rid}' already exists in {rel}")
    edits: list[FileEdit] = []
    ov.put(edits, path, xml_append(cur, [frag]))
    _ensure_manifest(edits, module_dir, ov, [rel])
    return EditPlan(edits=edits, summary=f"{what} in {module_dir.name}/{rel}")


# =========================================================================== #
# menu
# =========================================================================== #
def menu_plan(spec: MenuSpec, module_dir: Path, read: ReadFn,
              version: str = DEFAULT_VERSION) -> EditPlan:
    """A ``menuitem`` (plus a window action when ``spec.model`` is given and no
    action exists for it yet) in ``views/menus.xml`` + manifest entry."""
    module_dir = Path(module_dir)
    _require_module(module_dir, read)
    rel = "views/menus.xml"
    path = module_dir / rel
    ov = Overlay(read)
    cur = ov(path)
    mid = f"menu_{slug(spec.label)}"
    if xml_has_id(cur, mid):
        raise GeneratorError(f"menu '{mid}' already exists in {rel}")
    frags: list[str] = []
    action = spec.action
    if spec.model and not action:
        if not is_model_name(spec.model):
            raise GeneratorError(f"'{spec.model}' is not a valid model name")
        aid = _action_id(spec.model)
        views_text = ov(module_dir / "views" / f"{snake(spec.model)}_views.xml")
        if not (xml_has_id(views_text, aid) or xml_has_id(cur, aid)):
            frags.append(xml_fragment("action", aid, spec.label, spec.model,
                                      _view_mode(version)))
        action = aid
    frags.append(xml_fragment("menuitem", mid, spec.label, spec.parent, action))
    edits: list[FileEdit] = []
    ov.put(edits, path, xml_append(cur, frags))
    _ensure_manifest(edits, module_dir, ov, [rel])
    return EditPlan(edits=edits, summary=f"menu '{spec.label}' in {module_dir.name}/{rel}")


# =========================================================================== #
# report
# =========================================================================== #
def report_plan(spec: ReportSpec, module_dir: Path, read: ReadFn,
                model_module: str | None = None) -> EditPlan:
    """A QWeb PDF report: ``ir.actions.report`` + template in
    ``report/<snake>_report.xml``. ``model_module`` (the module defining the
    model, if known) enables the Print-menu binding."""
    module_dir = Path(module_dir)
    _require_module(module_dir, read)
    if not is_identifier(snake(spec.name)):
        raise GeneratorError(f"report name '{spec.name}' is not usable as an identifier")
    if not is_model_name(spec.model or ""):
        raise GeneratorError("a report needs a model (--model library.book)")
    x = snake(spec.name)
    rel = f"report/{x}_report.xml"
    path = module_dir / rel
    ov = Overlay(read)
    cur = ov(path)
    rid, tid = f"action_report_{x}", f"report_{x}"
    if xml_has_id(cur, rid):
        raise GeneratorError(f"report '{rid}' already exists in {rel}")
    binding = None
    if model_module:
        mx = model_xmlid(spec.model)
        binding = mx if model_module == module_dir.name else f"{model_module}.{mx}"
    frag = xml_fragment("report", rid, tid, module_dir.name,
                        spec.title or humanize(spec.name), spec.model, binding)
    edits: list[FileEdit] = []
    ov.put(edits, path, xml_append(cur, [frag]))
    _ensure_manifest(edits, module_dir, ov, [rel])
    return EditPlan(edits=edits, summary=f"report '{x}' for '{spec.model}' in "
                                         f"{module_dir.name}/{rel}")


# =========================================================================== #
# controller
# =========================================================================== #
_CONTROLLER_HEAD = "from odoo import http\nfrom odoo.http import request\n"


def controller_plan(spec: ControllerSpec, module_dir: Path, read: ReadFn) -> EditPlan:
    """An ``http.Controller`` with one route in ``controllers/main.py``
    (appended when the file exists), plus package imports."""
    module_dir = Path(module_dir)
    _require_module(module_dir, read)
    x = snake(spec.name)
    if not is_identifier(x):
        raise GeneratorError(f"controller name '{spec.name}' is not usable as an identifier")
    cls = class_name(spec.name)
    if not cls.endswith("Controller"):
        cls += "Controller"
    route = spec.route or f"/{module_dir.name}/{x}"
    ov = Overlay(read)
    edits: list[FileEdit] = []
    path = module_dir / "controllers" / "main.py"
    cur = ov(path)
    body = render("controller_class.py.j2", cls=cls, route=route, auth=spec.auth, method=x)
    if cur is None:
        new = _CONTROLLER_HEAD + "\n\n" + body
    else:
        try:
            tree = ast.parse(cur)
        except SyntaxError as e:
            raise GeneratorError(f"cannot parse controllers/main.py: {e.msg}")
        if any(isinstance(n, ast.ClassDef) and n.name == cls for n in tree.body):
            raise GeneratorError(f"class {cls} already exists in controllers/main.py")
        new = cur
        if "from odoo import http" not in new:
            new = "from odoo import http\n" + new
        if "from odoo.http import request" not in new:
            new = _insert_lines(new, 1, "from odoo.http import request\n")
        new = new.rstrip("\n") + "\n\n\n" + body
    ov.put(edits, path, new)
    _ensure_import(edits, module_dir / "controllers" / "__init__.py", ov, "main")
    _ensure_import(edits, module_dir / "__init__.py", ov, "controllers")
    return EditPlan(edits=edits, summary=f"controller {cls} ({route}) in "
                                         f"{module_dir.name}/controllers/main.py")


__all__ = [
    "ControllerSpec", "ReportSpec", "VIEW_KINDS", "MODEL_KINDS", "X2MANY_TYPES",
    "add_field_to_source", "controller_plan", "field_plan", "find_model_class",
    "list_tag", "menu_plan", "model_plan", "module_plan", "report_plan",
    "view_plan", "wizard_plan",
]
