"""The engine pipeline (RFC §6): workspace -> resolver -> arch -> index -> API.

    Workspace.load        parse (python / xml / csv), load graph first
    ModelResolver         RawModel -> ResolvedModel (load-ordered)
    EffectiveArchBuilder  base + cohort -> effective views (load-ordered)
    IndexBuilder          every §7 channel -> SemanticIndex (+ XML-ID graph)
    SemanticAPI           the read-only surface analyzers/generators consult

`build()` is the full rebuild; `Pipeline` keeps the stage outputs so `reload`
can re-run only from the workspace stage for changed files (RFC §8; see
`odx.engine.incremental`).
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path

from odx.engine.contracts import AccessRule
from odx.engine.index import SemanticIndex
from odx.engine.index.builder import IndexBuilder
from odx.engine.model.arch import EffectiveArchBuilder, EffectiveView
from odx.engine.model.raw import RawView
from odx.engine.model.resolver import ModelResolver, ResolvedModel
from odx.engine.parser.csv import access_rules_from
from odx.engine.semantic_api import SemanticAPI
from odx.engine.workspace import Workspace


@dataclass
class BuildResult:
    workspace: Workspace
    models: dict[str, ResolvedModel]
    views: dict[str, EffectiveView]
    raw_views: list[RawView]
    index: SemanticIndex
    access: list[AccessRule]
    api: SemanticAPI


def collect_access(ws: Workspace, api_catalog) -> list[AccessRule]:
    out: list[AccessRule] = []
    for c in ws.raw_csv():
        for a in access_rules_from(c):
            ref = a.model_ref or a.model
            m = api_catalog.model_by_xmlid.get(ref) or \
                api_catalog.model_by_local.get(ref.split(".", 1)[-1])
            out.append(replace(a, model=m or a.model))
    # <record model="ir.model.access"> in XML data
    for x in ws.raw_xml():
        for r in x.records:
            if r.model != "ir.model.access":
                continue
            mf = r.fields.get("model_id")
            ref = mf.ref if mf and mf.ref else None
            m = (api_catalog.model_by_xmlid.get(ref) or
                 api_catalog.model_by_local.get(ref.split(".", 1)[-1])) if ref else None
            gf = r.fields.get("group_id")

            def flag(name: str) -> bool:
                f = r.fields.get(name)
                v = (f.eval or f.text or "") if f else ""
                return v.strip() in ("1", "True", "true")
            out.append(AccessRule(
                model=m or (ref or ""), group=gf.ref if gf and gf.ref else None,
                perm_read=flag("perm_read"), perm_write=flag("perm_write"),
                perm_create=flag("perm_create"), perm_unlink=flag("perm_unlink"),
                location=r.location, xmlid=r.xmlid,
                name=(r.fields["name"].text if "name" in r.fields else None),
                model_ref=ref, module=r.module))
    return out


def build(ws: Workspace) -> BuildResult:
    models = ModelResolver(ws.graph).resolve(ws.raw_python())
    raw_views = [v for x in ws.raw_xml() for v in x.views]
    views = EffectiveArchBuilder(ws.graph).build(raw_views)
    from odx.engine.index.catalog import ModelCatalog
    catalog = ModelCatalog(models)
    index = IndexBuilder(ws, catalog, views, raw_views).build()
    access = collect_access(ws, catalog)
    api = SemanticAPI(ws, models, views, raw_views, index, access)
    return BuildResult(ws, models, views, raw_views, index, access, api)


def load(addons_paths: list[Path], *, project_paths: list[Path] | None = None) -> BuildResult:
    return build(Workspace.load(addons_paths, project_paths=project_paths))
