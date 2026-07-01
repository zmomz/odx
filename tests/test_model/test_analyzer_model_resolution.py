"""ModelResolutionAnalyzer — the accusation half of the discovery/accusation split.

The resolver records unresolvable fields as state (`UNRESOLVED` + a note); this
analyzer is the single place that turns such state into a `Diagnostic`. These
tests assert that the *analyzer* (not the resolver) owns `field.related-cycle`,
and that honest non-defects (DYNAMIC, PARTIAL) are NOT accused.
"""

from __future__ import annotations

from pathlib import Path

from odx.engine.analyzers.model_resolution import ModelResolutionAnalyzer
from odx.engine.contracts import Severity
from odx.engine.loadorder import ModuleGraph
from odx.engine.model.resolver import ModelResolver
from odx.engine.parser.python import parse_source


def _analyze(sources: dict[str, str]):
    modules = [parse_source(src, Path(f"{m}/models.py"), m) for m, src in sources.items()]
    models = ModelResolver(ModuleGraph()).resolve(modules)
    return ModelResolutionAnalyzer().run_on_models(models), models


def test_related_cycle_becomes_error_diagnostic():
    src = ("class M(models.Model):\n"
           "    _name = 'm'\n"
           "    a = fields.Many2one(related='b.parent_id')\n"
           "    b = fields.Many2one(related='a.parent_id')\n")
    diags, _models = _analyze({"mod": src})
    cyc = [d for d in diags if d.code == "field.related-cycle"]
    assert {d.field for d in cyc} == {"a", "b"}
    assert all(d.severity is Severity.ERROR for d in cyc)
    assert all(d.model == "m" for d in cyc)
    # the Diagnostic points at the field's location (for squiggles / CI annotations)
    assert all(d.location is not None for d in cyc)


def test_no_diagnostics_for_clean_models():
    src = ("class M(models.Model):\n"
           "    _name = 'm'\n"
           "    p = fields.Many2one('res.partner')\n"
           "    n = fields.Char(related='p.name')\n"
           "class P(models.Model):\n"
           "    _name = 'res.partner'\n"
           "    name = fields.Char()\n")
    diags, _models = _analyze({"mod": src})
    assert diags == []


def test_dynamic_and_partial_are_not_accused():
    # a DYNAMIC comodel (runtime-derived) and a related-through-dynamic chain are
    # honest non-defects — the analyzer must stay silent, not manufacture errors.
    src = ("class M(models.Model):\n"
           "    _name = 'm'\n"
           "    dyn = fields.Many2one(comodel_name=SOME_VAR)\n"
           "    ref = fields.Many2one(related='dyn')\n")
    diags, models = _analyze({"mod": src})
    # sanity: those fields really are non-STATIC…
    assert models["m"].fields["dyn"].resolution.value == "dynamic"
    # …but they produce no Diagnostics
    assert diags == []
