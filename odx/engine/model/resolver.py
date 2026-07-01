"""Model resolver — RawModel -> ResolvedModel (RFC §6, §17).

Builder + resolver as ONE subsystem (RFC §17): merges each model's contributions
across modules, applies `_inherit` (extension) and `_inherits` (delegation),
computes effective fields with provenance and a Resolution state.

Depends on `odx.engine.loadorder`: field overrides apply in module load order
(the last module in dependency order wins), exactly as view arch does. This is
the same ordering constraint the effective-arch builder has — both sit on the
load graph, which is why the graph is built first (docs/PHASE3.md).

## Why this is a fixpoint, not a single pass

The Phase 0.5 catalog's 353 `PARTIAL`s are dominated by `related=` fields whose
comodel is *inferred* by walking the related path (`order_id.partner_id.name`).
The subtlety that shapes the whole resolver: a `related` field can point *through
another* `related` field, across an `_inherit` boundary, into a model not yet
resolved. So a field's comodel can depend on another field's comodel — resolution
is a dependency graph among fields, and its edges are only discoverable *as you
resolve*. That rules out a single walk. It is a **worklist fixpoint**:

  - a related chain that resolves to a literal comodel  -> STATIC
  - one that bottoms out in a DYNAMIC field             -> DYNAMIC (routed to the
                                                            runtime bridge), never a guess
  - one whose path is broken / non-relational           -> UNRESOLVED (a candidate
                                                            Diagnostic, deferred to Phase 4)
  - one that *cycles* (A related-through-B related-through-A) -> a Diagnostic, because
                                                            that is a real defect in user code

The worklist is what lets us tell "cycle" apart from "undecidable": a field stays
*blocked* (non-terminal) at fixpoint **only** if it depends on another still-blocked
related field — every other outcome (dynamic, broken, non-relational target) is a
terminal answer that unblocks its dependents. So whatever remains pending when a
full drain makes no progress is, by construction, a genuine related-cycle. This is
the field-level form of the false-accusation rule (model/xpath.py, rule 2): three
distinct outcomes, none collapsed into "best effort".

Dynamic patterns (dynamic name/inherit/comodel, fields-in-loops, monkeypatch)
resolve to Resolution.DYNAMIC and are routed to the runtime bridge — never faked.

## Field-override attribute merge (backlog item 2 — done)

Odoo re-declaring a field is an ATTRIBUTE MERGE, not a wholesale replace: an
attribute the override omits is inherited from the field it overrides. The old
resolver did last-wins replace, silently dropping every unrestated attribute.
`comodel` was only the visible symptom (the scoreboard tracks it); the same bug
dropped `help`/`domain`/`string`/`required`/`groups`/... on any override, invisible
to every metric. `_merge_field` implements the real merge at BOTH override seams,
in load order: same-model contributions (`_absorb`) and inheritance
(`_InheritanceMerger`, transitive + parent-first). `_settle_comodel` then
re-derives the comodel state — STATIC when merging recovered a comodel, DYNAMIC
for a Many2oneReference whose comodel is a runtime sibling field (honest in both
directions: never fabricate a comodel, never mark a resolvable field DYNAMIC to
look cautious). On the corpus this drained the 80 comodel-absent PARTIALs to 0
(74 -> STATIC, 6 -> DYNAMIC) and, by restoring `related=` that overrides dropped,
rescued 11 fields the old resolver left UNRESOLVED.

## One2many inverse discovery (backlog item 3 — done)

A One2many names its comodel and an `inverse_name` (the Many2one on the comodel
pointing back). The parser reads a literal `inverse_name` but never verified it
names a real relational field on the comodel. `_verify_inverses` (pass 2.5) does,
against the comodel's EFFECTIVE fields — the inverse is frequently inherited or
delegated onto the comodel, not declared there (18 such live corpus sites), so it
reuses `_EffectiveFields`, not raw defs, and reads the merged `inverse_name`.
Honest in both directions: present+relational -> STATIC (`inverse-resolved`);
present but a non-relational Integer soft-FK (the legitimate reference-style
inverse, e.g. `res_id`/`sms_id_int`) -> left STATIC + `inverse-nonrelational`;
genuinely absent from a fully-visible comodel -> UNRESOLVED + `inverse-dangling`;
comodel outside the corpus -> known literal, not downgraded, `inverse-comodel-outside-corpus`.
Modelling `delegate=True` in the parser (the modern `_inherits` spelling)
dissolved the corpus's one apparent dangling inverse — it was reachable through a
delegated field all along, so marking it UNRESOLVED would have been a false
accusation. The field-resolution backlog is now closed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from odx.engine.contracts import FieldInfo, Location, Provenance, Resolution
from odx.engine.loadorder import ModuleGraph
from odx.engine.model.raw import RawField, RawModel, RawModule


@dataclass
class ResolvedField:
    info: FieldInfo
    provenance: Provenance
    resolution: Resolution = Resolution.STATIC
    # why the field is non-STATIC (e.g. "related-cycle", "related-through-dynamic").
    # This is the resolver's *discovery*; turning a reason into an accusation
    # (a Diagnostic) is an analyzer's job, not the resolver's (RFC §6 layering).
    notes: list[str] = field(default_factory=list)


@dataclass
class ResolvedModel:
    name: str
    kind: str = "Model"
    location: Location | None = None
    inherit_chain: list[str] = field(default_factory=list)      # prototype (_inherit) parents
    delegates: list[str] = field(default_factory=list)          # _inherits delegation targets
    fields: dict[str, ResolvedField] = field(default_factory=dict)  # effective (own + inherited)
    resolution: Resolution = Resolution.STATIC
    notes: list[str] = field(default_factory=list)
    # contributions kept for incremental invalidation (RFC §8): which files fed this model
    contributors: list[Provenance] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# Internal working model — mutable during the fixpoint. We keep RawField refs
# (mutable comodel/resolution/notes) so a comodel discovered mid-worklist is
# visible through every effective-field view without rebuilding anything; the
# frozen public ResolvedField/FieldInfo are materialized only at the end.
# --------------------------------------------------------------------------- #
@dataclass
class _Work:
    name: str
    kind: str = "Model"
    location: Location | None = None
    prototype_parents: list[str] = field(default_factory=list)  # _inherit (excluding self)
    delegate_parents: list[str] = field(default_factory=list)   # _inherits keys
    own: dict[str, RawField] = field(default_factory=dict)      # merged, last-in-load-order wins
    own_module: dict[str, str] = field(default_factory=dict)    # field name -> defining module
    contributors: list[Provenance] = field(default_factory=list)
    identity: Resolution = Resolution.STATIC
    notes: list[str] = field(default_factory=list)


class ModelResolver:
    """Consumes the load graph + raw modules; produces resolved models.

    The resolver *discovers* unresolvable fields (e.g. related-cycles) and records
    that as first-class state — `Resolution.UNRESOLVED` plus a reason in the
    field's `notes`. It does NOT emit `Diagnostic`s: discovery and accusation are
    different layers (RFC §6). A field's broken state is turned into a Diagnostic
    by an analyzer (`odx.engine.analyzers.model_resolution`, Phase 4), so there is
    exactly one path from broken-state to Diagnostic — the analyzer — for cycles
    and everything else alike.
    """

    def __init__(self, graph: ModuleGraph) -> None:
        self.graph = graph

    # ------------------------------------------------------------------ #
    # public entry
    # ------------------------------------------------------------------ #
    def resolve(self, raw_modules: list[RawModule]) -> dict[str, ResolvedModel]:
        work = self._merge_contributions(raw_modules)          # pass 1 — order & same-model merge
        _InheritanceMerger(work).run()                         # pass 1.5 — attr-merge across _inherit/_inherits
        _settle_comodels(work)                                 # pass 1.6 — re-derive comodel state post-merge
        eff = _EffectiveFields(work)                           # inheritance closure (memoized)
        self._resolve_related(work, eff)                       # pass 2 — worklist fixpoint
        self._verify_inverses(work, eff)                       # pass 2.5 — one2many inverse discovery
        return self._materialize(work, eff)                    # pass 3 — freeze public contracts

    def resolve_one(self, name: str) -> ResolvedModel | None:
        """Recompute a single model from its contributing defs (RFC §8 — the
        incremental path; a changed file dirties only the models it contributes
        to plus their `dependents_of()` frontier). Deferred until the LSP turns
        on the durable cache (docs/PHASE3.md); the seam is here so callers can
        depend on it now."""
        raise NotImplementedError("Phase 3 incremental path; activated at LSP (RFC §8)")

    # ------------------------------------------------------------------ #
    # pass 1 — group contributions by effective model name, ordered by load order
    # ------------------------------------------------------------------ #
    def _merge_contributions(self, raw_modules: list[RawModule]) -> dict[str, _Work]:
        order_index = {m: i for i, m in enumerate(self.graph.load_order())}
        fallback = len(order_index)  # modules outside the known corpus sort last, stably

        # gather (load_rank, discovery_seq, module, file, RawModel) per effective name
        buckets: dict[str, list[tuple]] = {}
        seq = 0
        for rmod in raw_modules:
            for rm in rmod.models:
                name = rm.effective_name
                if name is None:           # identity is fully runtime-derived (dynamic-name/-inherit)
                    continue
                rank = order_index.get(rmod.module, fallback)
                buckets.setdefault(name, []).append((rank, seq, rmod.module, rmod.file, rm))
                seq += 1

        work: dict[str, _Work] = {}
        for name, entries in buckets.items():
            entries.sort(key=lambda e: (e[0], e[1]))           # load order, then discovery order
            wm = _Work(name=name)
            for _rank, _seq, module, file, rm in entries:
                self._absorb(wm, rm, module, file)
            work[name] = wm
        return work

    def _absorb(self, wm: _Work, rm: RawModel, module: str, file: Path) -> None:
        if rm.kind != "Model":                                 # AbstractModel/TransientModel: keep the strongest
            if wm.kind == "Model":
                wm.kind = rm.kind
        if wm.location is None or rm.name is not None:          # prefer the defining (_name) site
            wm.location = rm.location or wm.location
        wm.contributors.append(Provenance(module=module, location=rm.location, via="own"))

        # prototype parents: every _inherit target that isn't this model itself
        for parent in rm.inherit:
            if parent != wm.name and parent not in wm.prototype_parents:
                wm.prototype_parents.append(parent)
        for parent in rm.inherits:                             # _inherits (delegation)
            if parent not in wm.delegate_parents:
                wm.delegate_parents.append(parent)

        # identity resolution: carry the weakest contribution (a dynamic-inherit
        # contribution to a model whose name we DO know elsewhere is still noted).
        if rm.resolution is Resolution.DYNAMIC:
            wm.identity = Resolution.DYNAMIC
        for n in rm.notes:
            if n not in wm.notes:
                wm.notes.append(n)

        # fields: a later contribution *overrides* an earlier one, but Odoo
        # field-override is an ATTRIBUTE MERGE, not a wholesale replace — an
        # attribute the override doesn't restate is inherited from the field it
        # overrides. Entries reach us in load order (see _merge_contributions),
        # so the accumulated `wm.own[name]` is the base and `rf` is the override.
        for rf in rm.fields:
            if rf.name in wm.own:
                wm.own[rf.name] = _merge_field(wm.own[rf.name], rf)
            else:
                wm.own[rf.name] = rf
            wm.own_module[rf.name] = module

    # ------------------------------------------------------------------ #
    # pass 2 — related= comodel inference as a worklist fixpoint
    # ------------------------------------------------------------------ #
    def _resolve_related(self, work: dict[str, _Work], eff: "_EffectiveFields") -> None:
        # seed: every relational related-field the parser left comodel-less
        pending: list[tuple[_Work, RawField]] = []
        for wm in work.values():
            for rf in wm.own.values():
                if _needs_related_inference(rf):
                    pending.append((wm, rf))
        pending_ids = {id(rf) for _wm, rf in pending}

        progress = True
        while pending and progress:
            progress = False
            for wm, rf in list(pending):
                status, comodel, reason = self._try_related(wm, rf, eff, pending_ids)
                if status == "blocked":
                    continue
                # terminal — apply and retire
                if status == "resolved":
                    rf.comodel = comodel
                    rf.resolution = Resolution.STATIC
                    rf.notes.append("comodel-resolved-via-related")
                elif status == "dynamic":
                    rf.resolution = Resolution.DYNAMIC
                    rf.notes.append(reason)
                else:  # "unresolved"
                    rf.resolution = Resolution.UNRESOLVED
                    rf.notes.append(reason)
                pending.remove((wm, rf))
                pending_ids.discard(id(rf))
                progress = True

        # whatever is still pending is, by construction, a related-field cycle:
        # every non-cyclic outcome is terminal and would have unblocked here. We
        # record it as honest state; the analyzer (Phase 4) makes the accusation.
        for _wm, rf in pending:
            rf.resolution = Resolution.UNRESOLVED
            rf.notes.append("related-cycle")

    def _try_related(self, wm: _Work, rf: RawField, eff: "_EffectiveFields",
                     pending_ids: set[int]) -> tuple[str, str | None, str | None]:
        related = rf.attrs.get("related")
        if not isinstance(related, str) or "<expr>" in related:
            return ("dynamic", None, "dynamic-related")       # related= is itself an expression
        segments = [s for s in related.split(".") if s]
        if not segments:
            return ("unresolved", None, "related-empty")

        cur = wm.name
        for i, seg in enumerate(segments):
            found = eff.lookup(cur, seg)
            if found is None:
                return ("unresolved", None, f"related-path-broken:{seg}")
            target = found.rawfield
            last = i == len(segments) - 1

            # depends on another still-unresolved related field -> block and retry
            if id(target) in pending_ids:
                return ("blocked", None, None)

            if last:
                if target.comodel is not None:
                    return ("resolved", target.comodel, None)
                if target.resolution is Resolution.DYNAMIC:
                    return ("dynamic", None, "related-through-dynamic")
                return ("unresolved", None, "related-target-nonrelational")
            # non-terminal hop: must step into the target's comodel
            if not target.is_relational:
                return ("unresolved", None, f"related-hop-nonrelational:{seg}")
            if target.comodel is not None:
                cur = target.comodel
                continue
            if target.resolution is Resolution.DYNAMIC:
                return ("dynamic", None, "related-through-dynamic")
            return ("unresolved", None, f"related-hop-unresolved:{seg}")
        return ("unresolved", None, "related-empty")  # unreachable; keeps the type total

    # ------------------------------------------------------------------ #
    # pass 2.5 — one2many inverse discovery (Phase 0.5 backlog item 3)
    #
    # A One2many names its comodel and an `inverse_name` (the Many2one on the
    # comodel that points back). The parser reads `inverse_name` when it's a
    # literal but never checks it names a real relational field on the comodel.
    # This pass verifies it against the comodel's EFFECTIVE fields — because the
    # inverse Many2one is frequently inherited (`_inherit`) or delegated
    # (`_inherits`/`delegate=True`) onto the comodel, not declared on it directly
    # (18 such live corpus sites). It reuses `_EffectiveFields` (the same merged/
    # inherited closure the related fixpoint walks) and reads the *merged*
    # `inverse_name`, so an override that dropped `inverse_name` (which
    # `_merge_field` restores from the base One2many) is verified against the
    # restored value, not the raw one.
    #
    # Honest in both directions (constraint #4): the field DISCOVERS state, it
    # does not accuse (RFC §6 — the analyzer turns a note into a Diagnostic).
    #   - inverse present & relational (Many2one/Many2oneReference)  -> STATIC,
    #     note `inverse-resolved`.
    #   - inverse present but NON-relational (an Integer soft-FK: `res_id`/
    #     `sms_id_int`/`parent_res_id` paired with a res_model or a deleted-record
    #     id — a legitimate Odoo reference-style inverse, 6 live) -> left STATIC
    #     (the comodel is solidly known; fabricating a defect would be dishonest),
    #     note `inverse-nonrelational` for the analyzer to weigh.
    #   - inverse names a field ABSENT from the (fully visible) comodel -> the one
    #     provable defect: UNRESOLVED, note `inverse-dangling` (+ the missing name).
    #   - comodel outside the corpus -> we can't see its fields, so we can't prove
    #     the inverse dangles; the comodel itself is a known literal, so we do NOT
    #     downgrade it (that would fabricate uncertainty). Note
    #     `inverse-comodel-outside-corpus`.
    #   - dynamic inverse / dynamic comodel -> already DYNAMIC (parser); skipped.
    # ------------------------------------------------------------------ #
    def _verify_inverses(self, work: dict[str, _Work], eff: "_EffectiveFields") -> None:
        for wm in work.values():
            for rf in wm.own.values():
                if rf.ftype != "One2many" or rf.inverse_name is None:
                    continue                                   # computed/related O2M: nothing to verify
                if rf.resolution is Resolution.DYNAMIC:
                    continue                                   # dynamic-inverse/-comodel: already honest
                if rf.comodel is None:
                    continue                                   # comodel dimension handled elsewhere
                if rf.comodel not in work:                     # comodel defined outside the corpus
                    rf.notes.append("inverse-comodel-outside-corpus")
                    continue
                inv = eff.lookup(rf.comodel, rf.inverse_name)  # EFFECTIVE (own+inherited+delegated)
                if inv is None:
                    rf.resolution = Resolution.UNRESOLVED
                    rf.notes.append("inverse-dangling")
                    rf.notes.append(f"inverse-missing:{rf.inverse_name}")
                elif inv.rawfield.ftype in ("Many2one", "Many2oneReference"):
                    rf.notes.append("inverse-resolved")
                else:
                    rf.notes.append("inverse-nonrelational")

    # ------------------------------------------------------------------ #
    # pass 3 — freeze the public, immutable contracts
    # ------------------------------------------------------------------ #
    def _materialize(self, work: dict[str, _Work],
                     eff: "_EffectiveFields") -> dict[str, ResolvedModel]:
        out: dict[str, ResolvedModel] = {}
        for name, wm in work.items():
            fields: dict[str, ResolvedField] = {}
            worst_field = Resolution.STATIC
            for fname, ef in eff.effective(name).items():
                rf = ef.rawfield
                attrs = dict(rf.attrs)
                if rf.comodel is not None:
                    attrs["comodel"] = rf.comodel
                if rf.inverse_name is not None:
                    attrs["inverse_name"] = rf.inverse_name
                fields[fname] = ResolvedField(
                    info=FieldInfo(name=fname, type=rf.ftype, attributes=attrs,
                                   defined_in=ef.module, location=rf.location),
                    provenance=Provenance(module=ef.module, location=rf.location, via=ef.via),
                    resolution=rf.resolution,
                    notes=list(rf.notes),
                )
                if rf.resolution in (Resolution.PARTIAL, Resolution.UNRESOLVED):
                    worst_field = Resolution.PARTIAL

            out[name] = ResolvedModel(
                name=name,
                kind=wm.kind,
                location=wm.location,
                inherit_chain=list(wm.prototype_parents),
                delegates=list(wm.delegate_parents),
                fields=fields,
                resolution=_rollup(wm.identity, worst_field),
                notes=list(wm.notes),
                contributors=list(wm.contributors),
            )
        return out


# --------------------------------------------------------------------------- #
# Effective-field view — own fields plus everything inherited through _inherit
# (prototype) and _inherits (delegation) parents. Memoized per model and
# cycle-guarded (a malformed inherit cycle can't spin the recursion). Returns
# RawField *references*, so the fixpoint's comodel mutations are visible here
# without recomputation. This is also what makes a related chain cross an
# `_inherit` boundary — the exact adversarial case that shapes the resolver.
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class _EffField:
    rawfield: RawField
    module: str        # module that DEFINED the field (provenance)
    via: str           # "own" | "_inherit" | "_inherits" — relationship at this model


class _EffectiveFields:
    def __init__(self, work: dict[str, _Work]) -> None:
        self._work = work
        self._memo: dict[str, dict[str, _EffField]] = {}

    def effective(self, model: str) -> dict[str, _EffField]:
        return self._compute(model, frozenset())

    def lookup(self, model: str, fname: str) -> _EffField | None:
        return self.effective(model).get(fname)

    def _compute(self, model: str, stack: frozenset[str]) -> dict[str, _EffField]:
        if model in self._memo:
            return self._memo[model]
        wm = self._work.get(model)
        if wm is None or model in stack:            # unknown parent (corpus boundary) or cycle
            return {}
        acc: dict[str, _EffField] = {}
        inner = stack | {model}
        # parents first so the child's own fields override inherited ones
        for parent in wm.prototype_parents:
            for fn, ef in self._compute(parent, inner).items():
                acc[fn] = _EffField(ef.rawfield, ef.module, "_inherit")
        for parent in wm.delegate_parents:
            for fn, ef in self._compute(parent, inner).items():
                acc[fn] = _EffField(ef.rawfield, ef.module, "_inherits")
        for fn, rf in wm.own.items():
            acc[fn] = _EffField(rf, wm.own_module[fn], "own")
        # memoize only when computed outside any cycle context (stack-free result)
        if not stack:
            self._memo[model] = acc
        return acc


# --------------------------------------------------------------------------- #
# Field-override attribute merge (Phase 0.5 backlog item 2).
#
# Odoo re-declaring a field does NOT replace it wholesale: any attribute the
# override omits is inherited from the field being overridden. The old resolver
# did last-wins replace, which silently dropped every unrestated attribute —
# `comodel` was only the visible symptom (it is what the scoreboard tracks); the
# same bug dropped `string`/`help`/`domain`/`required`/`groups`/... on any
# override, invisible to every metric. `_merge_field` implements the real merge.
#
# Direction: the OVERRIDE's explicitly-declared attributes win; the BASE only
# fills the gaps the override left. Used at two seams with the same semantics:
#   - same-model contributions in load order (base = earlier, override = later),
#   - inheritance (base = the parent's effective field, override = the child's).
# --------------------------------------------------------------------------- #
def _merge_field(base: RawField, override: RawField) -> RawField:
    merged = RawField(
        name=override.name,
        ftype=override.ftype,                       # the override restates the ctor
        location=override.location,                 # the override is where this decl lives
        comodel=override.comodel,
        inverse_name=override.inverse_name,
        attrs={**base.attrs, **override.attrs},     # base survives; override wins per-key
        resolution=override.resolution,
        notes=list(override.notes),
    )
    # comodel/inverse are only meaningful for the override's (restated) type. A
    # type-changing override (Many2one -> Char) must NOT drag a stale comodel in.
    if merged.is_relational and merged.comodel is None:
        if base.comodel is not None:
            merged.comodel = base.comodel
            if "comodel-via-override-merge" not in merged.notes:
                merged.notes.append("comodel-via-override-merge")
        elif base.resolution is Resolution.DYNAMIC:
            # the base's comodel is itself runtime-derived; an override that does
            # not restate it inherits that runtime-derivedness — don't fabricate.
            merged.resolution = Resolution.DYNAMIC
            for n in base.notes:
                if n.startswith("dynamic") and n not in merged.notes:
                    merged.notes.append(n)
    if merged.ftype == "One2many" and merged.inverse_name is None:
        merged.inverse_name = base.inverse_name
    return merged


def _settle_comodels(work: dict[str, _Work]) -> None:
    for wm in work.values():
        for rf in wm.own.values():
            _settle_comodel(rf)


def _settle_comodel(rf: RawField) -> None:
    """Re-derive a relational field's comodel *state* after merging. Merging can
    fill a comodel that was absent (-> STATIC) or restore a `related=` an override
    dropped (-> re-seed the worklist). This is the single place the comodel
    dimension is classified, so merge and parser can't disagree on it.

    Honest in both directions (constraint #4): a Many2oneReference whose comodel
    is chosen at runtime from a sibling `res_model` is DYNAMIC *by nature* — marked,
    never fabricated; a field whose comodel merging genuinely recovered is STATIC —
    not left PARTIAL to look cautious."""
    if not rf.is_relational:
        return
    if rf.resolution is Resolution.DYNAMIC:         # dynamic-comodel/-inverse — leave as runtime
        return
    # re-derive: strip the comodel-dimension markers we own, then re-classify
    rf.notes[:] = [n for n in rf.notes if n not in ("comodel-absent", "comodel-via-related")]
    if rf.comodel is not None:
        if rf.resolution is Resolution.PARTIAL:     # a gap the merge just closed
            rf.resolution = Resolution.STATIC
        return
    related = rf.attrs.get("related")
    if isinstance(related, str) and "<expr>" not in related:
        rf.resolution = Resolution.PARTIAL
        rf.notes.append("comodel-via-related")      # re-seed the worklist fixpoint
        return
    if rf.ftype == "Many2oneReference":
        rf.resolution = Resolution.DYNAMIC          # comodel via a runtime sibling field
        rf.notes.append("comodel-runtime-ref")
        return
    rf.resolution = Resolution.PARTIAL
    rf.notes.append("comodel-absent")


# --------------------------------------------------------------------------- #
# Inheritance attribute merge — the SECOND override seam. A child field that
# overrides an inherited (`_inherit`/`_inherits`) field inherits the base's
# unrestated attributes, exactly like a same-model override. This is transitive
# and must be applied parent-first: `hr.job.alias_id` (comodel-absent) overrides
# `mail.alias.mixin.alias_id` (comodel-absent) which overrides
# `mail.alias.mixin.optional.alias_id` (comodel='mail.alias') — the comodel flows
# down two hops only if each parent is merged before its children. Cycle-guarded
# (a malformed inherit loop can't spin the recursion) and memoized (each model
# merged once). Mutates `wm.own` in place: a child's own field is child-specific,
# so filling its gaps never corrupts a shared base field.
# --------------------------------------------------------------------------- #
class _InheritanceMerger:
    def __init__(self, work: dict[str, _Work]) -> None:
        self._work = work
        self._eff: dict[str, dict[str, RawField]] = {}   # model -> post-merge effective fields
        self._active: set[str] = set()                   # cycle guard

    def run(self) -> None:
        for name in list(self._work):
            self._effective(name)

    def _effective(self, model: str) -> dict[str, RawField]:
        if model in self._eff:
            return self._eff[model]
        wm = self._work.get(model)
        if wm is None:                                   # parent outside the corpus
            return {}
        if model in self._active:                        # inherit cycle — use own as-is
            return dict(wm.own)
        self._active.add(model)
        base: dict[str, RawField] = {}
        for parent in wm.prototype_parents:              # parents first, so they're merged
            base.update(self._effective(parent))
        for parent in wm.delegate_parents:
            base.update(self._effective(parent))
        # merge each own field that overrides an inherited one; base fills gaps
        for fn, rf in list(wm.own.items()):
            if fn in base:
                wm.own[fn] = _merge_field(base[fn], rf)
        acc = dict(base)
        acc.update(wm.own)                               # own (now merged) overrides inherited
        self._active.discard(model)
        self._eff[model] = acc
        return acc


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _needs_related_inference(rf: RawField) -> bool:
    """A relational field whose comodel the parser could not read literally but
    which carries a `related=` path to walk (the `comodel-via-related` bucket).
    The note is set by the parser and re-established by `_settle_comodel` after
    merging, so an override that dropped `related=` (which attribute-merge then
    restores) is seeded here too."""
    return (rf.is_relational and rf.comodel is None
            and "comodel-via-related" in rf.notes)


def _rollup(identity: Resolution, worst_field: Resolution) -> Resolution:
    """Model resolution = identity, degraded by its fields. A DYNAMIC *field*
    (a comodel legitimately routed to the runtime) does NOT knock a well-known
    model off STATIC — only an unresolved/partial field does. Dynamic *identity*
    (dynamic _name/_inherit) dominates: the model itself is runtime-derived."""
    if identity is Resolution.DYNAMIC:
        return Resolution.DYNAMIC
    if worst_field is Resolution.PARTIAL:
        return Resolution.PARTIAL
    return identity
