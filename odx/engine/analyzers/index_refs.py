"""IndexReferenceAnalyzer — every UNRESOLVED §7 reference becomes a Diagnostic.

The index already did the hard part: each entry carries an honest Resolution,
and UNRESOLVED only when the target model is *complete* (so absence is proven,
not our blind spot). This analyzer is just the accusation table: reference kind
-> stable code, severity and an action-oriented message. DYNAMIC entries are
never accused (RFC §10: route to the runtime, don't guess).
"""

from __future__ import annotations

from odx.engine.contracts import Diagnostic, Resolution, Scope, Severity
from odx.engine.index import RefKind

E, W = Severity.ERROR, Severity.WARNING

# kind -> (field-level code, severity, message) ; model-level codes below
FIELD_RULES: dict[str, tuple[str, Severity, str]] = {
    RefKind.VIEW_FIELD: ("view.unknown-field", E,
                         "Field `{field}` does not exist on `{model}`; Odoo will refuse to load {detail}."),
    RefKind.VIEW_MODIFIER: ("view.unknown-modifier-field", E,
                            "Modifier reads `{field}`, which is not a field of `{model}` ({detail})."),
    RefKind.VIEW_DOMAIN: ("view.domain-unknown-field", E,
                          "Domain uses `{path}`: `{field}` is not a field of `{model}` ({detail})."),
    RefKind.SEARCH_FILTER: ("view.search-unknown-field", E,
                            "Search filter uses `{path}`: `{field}` is not a field of `{model}` ({detail})."),
    RefKind.DEPENDS: ("field.depends-unknown-field", E,
                      "@api.depends('{path}'): `{field}` is not a field of `{model}`; the compute will never be triggered by it."),
    RefKind.CONSTRAINT: ("field.constrains-unknown-field", W,
                         "@api.constrains('{path}'): `{field}` is not a field of `{model}`; the constraint never fires for it."),
    RefKind.ONCHANGE: ("field.onchange-unknown-field", W,
                       "@api.onchange('{path}'): `{field}` is not a field of `{model}`."),
    RefKind.RELATED: ("field.related-broken", E,
                      "related='{path}': `{field}` cannot be followed on `{model}` ({note})."),
    RefKind.INVERSE: ("field.inverse-dangling", E,
                      "One2many inverse `{field}` does not exist on `{model}` ({detail}); add the Many2one or fix inverse_name."),
    RefKind.FIELD_DOMAIN: ("field.domain-unknown-field", E,
                           "Field domain uses `{path}`: `{field}` is not a field of `{model}` ({detail})."),
    RefKind.CURRENCY_FIELD: ("field.currency-field-missing", E,
                             "currency_field `{field}` does not exist on `{model}` ({detail})."),
    RefKind.MODEL_ATTR: ("model.order-unknown-field", E,
                         "`{field}` in {detail} is not a field of `{model}`; searches on this model will fail."),
    RefKind.SQL_CONSTRAINT: ("model.sql-constraint-unknown-column", W,
                             "SQL constraint references `{field}`, not a column of `{model}`."),
    RefKind.ACTION_DOMAIN: ("action.domain-unknown-field", E,
                            "Action domain uses `{path}`: `{field}` is not a field of `{model}` ({detail})."),
    RefKind.SERVER_ACTION: ("action.code-unknown-field", W,
                            "Server action code reads `{path}`: `{field}` is not a field of `{model}` ({detail})."),
    RefKind.CRON: ("action.code-unknown-field", W,
                   "Cron code reads `{path}`: `{field}` is not a field of `{model}` ({detail})."),
    RefKind.AUTOMATION: ("action.automation-unknown-field", E,
                         "Automated action uses `{path}`: `{field}` is not a field of `{model}` ({detail})."),
    RefKind.RECORD_RULE: ("security.rule-unknown-field", E,
                          "Record rule domain uses `{path}`: `{field}` is not a field of `{model}` ({detail})."),
    RefKind.REPORT: ("report.unknown-field", W,
                     "Report template reads `{path}`: `{field}` is not a field of `{model}` ({detail})."),
    RefKind.MAIL_TEMPLATE: ("data.mail-template-unknown-field", W,
                            "Mail template reads `{path}`: `{field}` is not a field of `{model}` ({detail})."),
    RefKind.DATA_RECORD: ("data.unknown-field", E,
                          "Data sets `{field}`, which is not a field of `{model}` ({detail}); module install will fail."),
    RefKind.FILTER: ("data.filter-unknown-field", W,
                     "Saved filter domain uses `{path}`: `{field}` is not a field of `{model}`."),
    RefKind.DELEGATE: ("model.delegate-field-missing", E,
                       "_inherits field `{field}` does not exist on `{model}`."),
}

METHOD_RULES: dict[str, tuple[str, Severity, str]] = {
    RefKind.VIEW_BUTTON: ("view.button-method-missing", E,
                          "Button calls `{method}()`, which `{model}` does not define ({detail})."),
    RefKind.FIELD_METHOD: ("field.method-missing", E,
                           "`{method}` ({detail}) is not a method of `{model}`."),
    RefKind.FUNCTION: ("data.function-method-missing", E,
                       "<function> calls `{model}.{method}()`, which is not defined."),
    RefKind.CRON: ("action.cron-method-missing", E,
                   "Cron calls `{model}.{method}()`, which is not defined ({detail})."),
    RefKind.SERVER_ACTION: ("action.code-method-missing", W,
                            "Server action calls `{method}()` on `{model}`, which is not defined ({detail})."),
}

MODEL_RULES: dict[str, tuple[str, Severity, str]] = {
    RefKind.VIEW: ("view.unknown-model", E, "View targets unknown model `{model}` ({detail})."),
    RefKind.INHERIT: ("model.unknown-parent", E,
                      "`_inherit` names `{model}`, which no module in the dependency closure defines ({detail})."),
    RefKind.DELEGATE: ("model.unknown-delegate", E,
                       "`_inherits` names unknown model `{model}` ({detail})."),
    RefKind.FIELD_COMODEL: ("field.unknown-comodel", E,
                            "Relational field targets unknown model `{model}` ({detail})."),
    RefKind.ACTION: ("action.unknown-model", E, "Action targets unknown model `{model}` ({detail})."),
    RefKind.DATA_RECORD: ("data.unknown-model", E, "Data record of unknown model `{model}` ({detail})."),
    RefKind.CODE: ("python.unknown-model", W,
                   "`env['{model}']` names a model no module in the dependency closure defines ({detail})."),
}

NOTE_CODES = {"hop-nonrelational": "path-through-nonrelational"}


class IndexReferenceAnalyzer:
    code_prefix = "ref"
    categories = ("views", "fields", "data", "actions", "security", "reports", "python")

    def run(self, api, scope: Scope) -> list:
        out: list[Diagnostic] = []
        for e in api.entries():
            if e.resolution is not Resolution.UNRESOLVED:
                continue
            if not api.in_scope(scope, module=e.module, location=e.location):
                continue
            ctx = dict(model=e.model, field=e.field, method=e.method, path=e.path or e.field,
                       detail=e.detail, note=e.note)
            if e.method:
                rule = METHOD_RULES.get(e.kind)
            elif e.field:
                rule = FIELD_RULES.get(e.kind)
            else:
                rule = MODEL_RULES.get(e.kind)
            if rule is None:
                continue
            code, sev, msg = rule
            if e.note == "hop-nonrelational" and e.field:
                msg = ("`{path}` walks through `{field}` on `{model}`, which is not a relational "
                       "field ({detail}).")
            out.append(Diagnostic(severity=sev, code=code, message=msg.format(**ctx),
                                  model=e.model, field=e.field or e.method,
                                  location=e.location))
        return out
