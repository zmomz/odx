# Odoo Developer Semantic Engine — Design RFC

*An engine that statically reproduces enough of Odoo's semantics to understand a real codebase, surfaced through thin clients (CLI first; MCP, LSP, CI, web later).*

**Status:** Draft RFC · **Version:** 0.4 · **Core language:** Python · **First client:** Typer CLI

---

## How to read this document

Engineering RFC, not a pitch. Structure: **Problem → Constraints → Design → Trade-offs → Alternatives → Open questions → Plan.** Load-bearing decisions (expensive to reverse) are stated firmly; speculative ones (cheap to change once a real client exists) are marked **TBD** on purpose.

Decision test: **not** *how far in the future is this?* but *if left undecided, will the resolver get built in a way we'd have to tear out?* If yes, decide now; if no, defer.

> **Note on this document's status.** This RFC has reached the point of diminishing returns. The next thing that will teach us anything is **Phase 0.5** — pointing a rough resolver at messy real Odoo (especially OCA) and finding where assumptions break. Further design revision should largely stop until that failure data exists. This version exists to capture decisions already made, not to invite another round.

---

## Table of contents

1. [Problem](#1-problem)
2. [The hardest part, stated honestly](#2-the-hardest-part-stated-honestly)
3. [The core assumption to validate first](#3-the-core-assumption-to-validate-first)
4. [Constraints](#4-constraints)
5. [Design overview: engine + runtime + clients](#5-design-overview-engine--runtime--clients)
6. [The engine as a layered pipeline](#6-the-engine-as-a-layered-pipeline)
7. [The semantic index (the differentiated core)](#7-the-semantic-index-the-differentiated-core)
8. [Incremental recompute & caching](#8-incremental-recompute--caching)
9. [The analyzer contract (plugin-ready, not pluggable yet)](#9-the-analyzer-contract-plugin-ready-not-pluggable-yet)
10. [Core data contracts](#10-core-data-contracts)
11. [The public API](#11-the-public-api)
12. [Runtime (infrastructure, not engine)](#12-runtime-infrastructure-not-engine)
13. [The clients](#13-the-clients)
14. [The CLI client in detail](#14-the-cli-client-in-detail)
15. [check, report, doctor: three distinct capabilities](#15-check-report-doctor-three-distinct-capabilities)
16. [The field-type → attributes matrix](#16-the-field-type--attributes-matrix)
17. [Trade-offs & alternatives](#17-trade-offs--alternatives)
18. [Competitive context](#18-competitive-context)
19. [Build plan (phased)](#19-build-plan-phased)
20. [Project layout](#20-project-layout)
21. [Configuration](#21-configuration)
22. [Testing strategy](#22-testing-strategy)
23. [Open questions](#23-open-questions)
24. [Glossary](#24-glossary)

---

## 1. Problem

Odoo development has tooling, but nothing both easy to use and aware of the code's *meaning*. `odoo-bin` is a server launcher with flat subcommands and no validation. `odev` is capable but partner-flavored, large of surface, ops-focused, single-surface (CLI only).

The bugs that hurt most in Odoo are semantic and load-time: a view references a field absent from the resolved `_inherit` chain; a one2many lacks its inverse; an access rule is missing; a compute has no `@api.depends`; an XML ID dangles. None surface until the server loads — one crash at a time.

The knowledge to catch all of them is the *same* knowledge: a resolved model of the addons tree plus an index of what references what. That knowledge is equally useful to a terminal, an editor, an AI assistant, and CI. So the thing to build is an **engine** that produces it, with thin clients over it.

---

## 2. The hardest part, stated honestly

The parser is not the hard part. The resolver is hard, but it is not the hardest part either. **The hardest part is reproducing Odoo's semantics without importing Odoo.**

These are language-implementation problems, not parsing problems:

- `_inherit` (extension) and `_inherits` (delegation — different mechanism)
- mixins and abstract models; delegated fields; field overrides
- module load order and manifest dependency graph
- XML view inheritance (`xpath`, `position`)
- dynamic model modification (fields built in loops, models assembled from runtime data, monkeypatches, custom metaclasses)

We are effectively writing a **static compiler front-end for a dynamic framework.** Some of Odoo's metaprogramming is genuinely undecidable statically (fields whose existence depends on runtime data). That is not a reason to abandon the approach — it is the reason §3's spike must find the **boundary** between *statically resolvable* and *needs-a-fallback*, because that boundary is the central design input for everything downstream.

---

## 3. The core assumption to validate first

Everything rests on one assumption:

> **We can statically reproduce enough of Odoo's semantics to understand a real codebase well enough that developers trust the output — without booting Odoo.**

If false, no architecture matters. **The first work item is therefore a spike with an objective gate, not more design** (Phase 0.5, §19).

### The spike's deliverables

**A scoreboard** (orientation, not the gate):

| Metric | Target |
|--------|--------|
| Files parsed without crashing | 100% |
| Models resolved | ≥ 95% |
| Fields resolved (incl. inherited) | ≥ 95% |
| `_inherit` chains resolved | ≥ 90% |

**A categorized failure list** (the actual deliverable). Every failure tagged:

- **fixable** — a parser/resolver bug we can close (raises the score with effort).
- **hard** — resolvable statically but expensive (e.g. cross-module `_inherits`, `xpath` inheritance).
- **out-of-scope** — fundamentally undecidable statically (runtime-data-driven fields); must fall back to the runtime bridge or be explicitly unsupported.

The percentage tells us *where we are*; the catalog tells us *whether we can move*, and where the static/runtime boundary lies. A 70% caused by one fixable bug is better than a 92% caused by ten unfixable patterns — so the gate is the catalog's shape, not the number.

**Go/no-go:** if core resolution (`_inherit`, effective fields) is promising and failures cluster in *fixable* or cleanly *out-of-scope*, proceed; the failure catalog scopes Phase 3. If *hard*/*out-of-scope* failures riddle ordinary modules, rethink the approach here — cheaply.

---

## 4. Constraints

- **C1 — No booting Odoo.** The engine works by static analysis. (The *runtime* component may talk to a live instance, but understanding must not require one.)
- **C2 — The engine returns data, never formatted output.** `Diagnostic`s, `EditPlan`s, info structs. Presentation lives in clients.
- **C3 — The engine writes nothing.** Mutations are *described* (`EditPlan`); a client applies them.
- **C4 — `Location` recorded at parse time.** Editor squiggles and CI annotations need precise ranges; retrofitting is expensive. Load-bearing.
- **C5 — Incremental recompute designed in.** Structures must not foreclose recomputing only what a change affects (§8). May *ship* full-rebuild first.
- **C6 — No client awareness in the engine.** Enforced by architecture test (no client/runtime imports in engine; no Typer/Rich/LSP in engine).
- **C7 — Infrastructure ≠ semantics.** RPC/shell/docker/postgres are runtime, a *sibling* of the engine, never a dependency of it.

---

## 5. Design overview: engine + runtime + clients

```
┌──────────────────────────────────────────────────────────────┐
│  CLIENTS  (thin; render only — no domain logic)               │
│  cli   ·   mcp   ·   lsp   ·   ci   ·   web                    │
└───────────────────────────┬──────────────────────────────────┘
                            │ call
┌───────────────────────────▼──────────────────────────────────┐
│  PUBLIC API   (typed, client-agnostic; returns plain data)     │
└───────────────┬───────────────────────────┬──────────────────┘
                │                            │
┌───────────────▼──────────────┐   ┌─────────▼───────────────────┐
│  ENGINE  (pure static         │   │  RUNTIME  (infrastructure)   │
│  semantics; see §6)           │   │  rpc · shell · docker · pg   │
│  knows nothing about RPC      │   │  engine does NOT depend on it│
└───────────────────────────────┘   └──────────────────────────────┘
```

Some API methods (`diff_against_db`, `doctor`) *compose* both — engine says what the code defines, runtime says what the instance has — but the engine never imports the runtime. Two clients prove the abstraction; one is a guess (§19 validation gate).

---

## 6. The engine as a layered pipeline

The engine is a pipeline, one direction of flow, each stage one responsibility, each depending only on earlier stages.

```
  source files
       │
   ┌───▼────┐
   │ Parser │      files → ASTs / XML trees / CSV rows  (records Location)
   └───┬────┘
       │
 ┌─────▼─────────┐
 │ Model Resolver │  RawModel → ResolvedModel
 │ (builder+      │  extract raw defs, then apply _inherit/_inherits/xpath
 │  resolver,     │  → effective models with provenance
 │  one subsystem)│
 └─────┬─────────┘
       │
 ┌─────▼─────────┐
 │ Semantic Index│  the reference graph + XML-ID graph  (the moat — §7)
 └─────┬─────────┘
       │
 ┌─────▼──────┐
 │ Semantic API│   typed read surface (field_exists, references_to, get_model…)
 └─────┬──────┘
       │
   ┌───┴─────────────┐
   ▼                 ▼
┌─────────┐    ┌────────────┐
│Analyzers│    │ Generators │
│→Diagnostic   │ →EditPlan  │
└─────────┘    └────────────┘
```

Notes on two decisions from review:

- **Model Builder and Resolver are one subsystem, not two** (§17). `RawModel → ResolvedModel` are public, inspectable data *types* — `RawModel` stays a real intermediate value (useful for debugging and the incremental path) — but they share one package with a freely-movable internal seam, because they evolve together. Splitting them into two enforced subsystems just causes code to thrash across the boundary.
- **"Queries" renamed "Semantic API"** — "queries" read as SQL. This is the read-only surface over resolved models + index, and the only thing analyzers/generators consult.

Discipline: a stage may depend only on earlier stages. Parser can't ask the Resolver anything; Analyzers can't re-parse.

---

## 7. The semantic index (the differentiated core)

The index is the moat, and earlier drafts undersold it. The parser is table stakes; the resolver is hard; **the index is where the differentiated capability lives.** Indexing field references *in views* is the minimum. The value is indexing references through *every* channel Odoo can reference a field or model:

- views (`<field>`, attrs/invisible/readonly conditions)
- **domains** (in fields, actions, rules)
- compute methods and their `@api.depends`
- report templates (QWeb)
- server actions and automated actions (base automation)
- constraints (`@api.constrains`, SQL constraints)
- security: groups, record rules, ACL rows
- actions, menus
- the **XML-ID graph** (defined vs referenced)

With that breadth indexed, the impact/where-used query becomes nearly free and genuinely differentiated:

```
$ odx why sale.order.partner_id        # terse
$ odx impact sale.order.partner_id     # detailed report
   17 views · 4 reports · 2 compute methods · 3 domains
   6 server actions · 2 automated actions · 1 record rule
```

(`why` and `impact` are the same index query at two verbosity levels — naming in §14.) This breadth is also what makes provenance-based incremental invalidation (§8) correct: to invalidate everything touching a changed field, the index must *know* everything that touches it.

The Semantic Index is therefore a **first-class subsystem with an explicit, enumerated set of reference kinds**, not a passive lookup table.

---

## 8. Incremental recompute & caching

One concern, two properties of the *same* mechanism — not a separate three-tier cache subsystem (§17). The goal: `odx check` in ~120ms on re-run, not ~5s.

**The mechanism is the invalidation graph.** When a file changes:

```
  partner.py changed
        │  re-parse partner.py only
        ▼  rebuild the raw defs it contributed
   dirty models: res.partner (+ anything inheriting it)
        │  re-resolve only those → new ResolvedModels
        ▼  drop index entries originating-from or targeting those
   re-run analyzers over the affected scope only
```

**Caching is the same graph made durable.** Each stage's output is persistable, keyed by a content hash of its inputs; on the next run, unchanged hashes reuse cached output; the invalidation graph decides what's stale. There is no second cache mechanism to keep in sync — persistence is just "the non-stale results survive process restart."

**What's load-bearing now (so v1 doesn't foreclose it):**
- ResolvedModels carry **provenance** (which file/module/def contributed each field) → a changed file maps to dirty models.
- The Resolver can recompute a single model from its contributing defs without a global rebuild.
- The Index supports **targeted invalidation** (drop refs from file X / to model Y).
- Analyzers accept a **scope**, not only "whole project."

**What's deferred (TBD):** v1 may full-rebuild and skip on-disk cache. The structures above exist from Phase 3; durable caching + true incremental activation land when a client (LSP) needs them.

---

## 9. The analyzer contract (plugin-ready, not pluggable yet)

Every analyzer — built-in or future third-party — implements one uniform contract:

```python
class Analyzer(Protocol):
    code_prefix: str                 # namespace, e.g. "view", "oca", "perf"
    def run(self, api: SemanticAPI, scope: Scope) -> list[Diagnostic]: ...
```

**Decided now (free):** built-ins are *not* special-cased — they implement exactly this interface. A `code_prefix` namespace is reserved per analyzer so future external rulesets (OCA conventions, company house rules, migration rules, performance rules, security rules) slot in without collision.

**Deliberately NOT built now (speculative infrastructure):** no plugin registration, discovery, loading, or external API. The *seam* is plugin-ready; the *machinery* is absent until there's demand. This is the reversibility test: a uniform contract costs nothing and prevents a later refactor; a plugin system built now would be guesswork.

---

## 10. Core data contracts

### `Diagnostic` — the unit of analysis
```python
@dataclass(frozen=True)
class Diagnostic:
    severity: Severity            # ERROR | WARNING | INFO | HINT
    code: str                     # stable, namespaced, e.g. "view.unknown-field"
    message: str                  # action-oriented
    model: str | None
    field: str | None
    location: Location | None     # → squiggle / CI annotation
    suggested_fix: EditPlan | None
    related: list[Location]
```
Same object → CLI table row, editor squiggle, MCP JSON, PR annotation. `code` is namespaced (ties to §9 prefixes) and documented like a linter rule.

### `Location` (C4)
```python
@dataclass(frozen=True)
class Location:
    file: Path
    start_line: int; start_col: int
    end_line: int;   end_col: int
```

### `EditPlan` (C3)
```python
@dataclass(frozen=True)
class EditPlan:
    edits: list[FileEdit]
    summary: str

@dataclass(frozen=True)
class FileEdit:
    file: Path
    kind: EditKind                # CREATE | INSERT | REPLACE
    range: Location | None
    new_text: str
```
Application is a client concern: CLI writes + prints tree; LSP → `WorkspaceEdit`; MCP proposes for approval; web previews diff; CI opens a PR.

### Info structures
```python
@dataclass(frozen=True)
class ModelInfo:
    name: str
    inherit_chain: list[str]      # _inherit (extension / prototype)
    delegates: list[str]          # _inherits (delegation) — SEPARATE mechanism (§2/§17):
                                  # a real many2one to another record, not a field merge.
                                  # Collapsing into inherit_chain blinds the §7 index to
                                  # delegated fields' true origin. (Added post-Phase-0.5.)
    fields: list[FieldInfo]       # effective (own + inherited + delegated)
    location: Location | None

@dataclass(frozen=True)
class FieldInfo:
    name: str
    type: str
    attributes: dict[str, object]  # incl. inferred comodel/inverse_name once resolved
    defined_in: str               # provenance — powers why + invalidation
    location: Location | None

@dataclass(frozen=True)
class Reference:
    kind: str                     # one of the §7 reference kinds
    location: Location
    detail: str
```

### `Resolution` — a first-class state (added post-Phase-0.5)

The spike (§3) confirmed what §2 predicted: some model/field facts are statically
knowable, some are provably runtime-derived, and some are improvable-but-not-yet.
Encoding that boundary as a *value* — not an exception or a silent guess — is what
keeps the engine's output trustworthy (a confident wrong answer is the one thing
§3 says destroys trust). So every resolved model/field carries:

```python
class Resolution(str, Enum):
    STATIC      # fully resolved from static evidence
    DYNAMIC     # provably runtime-derived → route to the runtime bridge, don't guess
    PARTIAL     # resolved with a known gap (improvable, not undecidable)
    UNRESOLVED  # expected target not found; a candidate Diagnostic (via an analyzer)
```

Clients branch on it: LSP mutes DYNAMIC instead of red-squiggling it; MCP tells the
AI "ask the runtime"; `report` counts DYNAMIC/UNRESOLVED apart from real defects.
The resolver *records* this state (with a reason note); it does **not** emit the
Diagnostic — turning an UNRESOLVED state into an accusation is an analyzer's job
(§6 layering), so there is one path from broken-state to Diagnostic, not two.
Today the engine carries `resolution` + reason notes on the resolver's
`ResolvedField`/`ResolvedModel`; it folds into the client-facing `FieldInfo`/
`ModelInfo` surface when the Semantic API (§6) is built.

> **On these post-0.5 additions.** This RFC's footer parks contract revision *until
> the spike returns failure data*. It has. `Resolution` and `ModelInfo.delegates`
> (below) are the first fold-back of that data into the contracts — the designed
> re-entry point, not drift. `delegates` separates `_inherits` (delegation — a real
> many2one to another record) from `_inherit` (extension); collapsing them would
> blind the §7 index to delegated fields' true origin.

---

## 11. The public API

Typed, client-agnostic, returns plain data. **Stabilized, not frozen** — zero external consumers means breaking changes are nearly free; we keep them free until external dependents exist (§17).

```python
class Engine:
    @classmethod
    def load(cls, project_root: Path) -> "Engine": ...
    def reload(self, changed: list[Path] | None = None) -> None: ...

    # query (read-only)
    def get_model(self, name: str) -> ModelInfo | None: ...
    def get_fields(self, model: str) -> list[FieldInfo]: ...
    def field_exists(self, model: str, field: str) -> bool: ...
    def references_to(self, model: str, field: str | None = None) -> list[Reference]: ...
    def access_rules(self, model: str) -> list[AccessRule]: ...

    # analyze (read-only → Diagnostics)
    def check(self, scope: Scope = ALL, only: list[str] | None = None) -> list[Diagnostic]: ...
    def report(self, scope: Scope = ALL, with_runtime: bool = False) -> HealthReport: ...

    # generate / fix (→ EditPlan; never writes)
    def new_module(self, spec: ModuleSpec) -> EditPlan: ...
    def new_model(self, spec: ModelSpec) -> EditPlan: ...
    def add_field(self, spec: FieldSpec) -> EditPlan: ...
    def add_view(self, spec: ViewSpec) -> EditPlan: ...
    def fix(self, scope: Scope = ALL) -> EditPlan: ...

    # runtime-composed (engine + runtime; see §12)
    def diff_against_db(self, conn: DbConnection) -> list[Diagnostic]: ...
    def doctor(self, conn: DbConnection) -> DoctorReport: ...
    def upgrade_check(self, target: str) -> UpgradeReport: ...
    def upgrade_apply(self, target: str) -> EditPlan: ...
```

Read-only vs mutating is encoded in return types: `Diagnostic`/`*Info`/`*Report` never touch disk; mutations return an `EditPlan` a client chooses to apply. No direct-write method exists.

---

## 12. Runtime (infrastructure, not engine)

A sibling of the engine (C7), never a dependency. Responsibilities: **rpc** (XML-RPC/JSON-RPC to a live instance), **shell** (drive `odoo shell`), **docker/postgres** (lifecycle, DB create/drop/dump/restore).

Composed (not owned) by API methods needing both sides: `diff_against_db` (code vs installed → drift `Diagnostic`s), `doctor` (fresh DB → clean install → what breaks). An architecture test asserts `engine/` imports nothing from `runtime/`.

---

## 13. The clients

Thin; call the API, render in their idiom. Not-yet-built clients are deliberately **TBD** internally.

- **CLI** (#1, first) — Typer + Rich. Renders contracts; owns the grammar (§14). A real product, months of UX (§17).
- **MCP** (#2) — exposes API as tools an AI calls, so it queries ground truth instead of hallucinating fields/`_inherit` chains. Built second to **validate the abstraction** (§19). Contracts serialize to JSON nearly directly. *Tool surface: TBD until built.*
- **LSP** (later) — `Diagnostic`s → squiggles, `references_to` → find-references, queries → hover/autocomplete, `suggested_fix` → code actions. Needs §8 incrementality. *Reload/host: TBD.*
- **CI** (later) — `check`/`report`/`upgrade check` → PR annotations at `Location`; auto-PRs from `EditPlan`s. *Format: TBD.*
- **Web** (later) — visualize the §7 reference graph, browse effective models, preview diffs. *Transport: TBD.*

---

## 14. The CLI client in detail

First product; real UX (completion, help, errors, prompts, config detection, color, discoverability — months, not a weekend, §17). Grammar is a contract.

### Fixed verb set

| Verb | Meaning | Backing API | Writes? |
|------|---------|-------------|:------:|
| `new` | make something new | `new_*` | ✅ |
| `add` | add to existing | `add_*` | ✅ |
| `inherit` | scaffold inherited artifact | `add_view`(inherit) | ✅ |
| `check` | fast static analysis; report only | `check` | ❌ |
| `report` | aggregate health report (§15) | `report` | ❌ |
| `fix` | apply safe changes | `fix` | ✅ |
| `why` / `impact` | where-used (terse / detailed) | `references_to` | ❌ |
| `run`/`test`/`logs`/`shell` | daily loop | runtime | ❌ |
| `db` | database ops | runtime | varies (explicit) |
| `doctor` | runtime fresh-install probe (§15) | runtime+engine | ❌ |
| `diff` | code-vs-DB | runtime+engine | ❌ |
| `upgrade` | migration (`check`/`apply`) | engine(+runtime) | varies (explicit) |
| `init` | set up project | config | ✅ (config only) |

### Grammar rules
1. `tool <verb> <noun> [target] [--flags]`. No exceptions.
2. Read-only vs mutating is a hard law (mirrors API return types).
3. Nouns are Odoo's vocabulary (`model`, `view`, `field`, `wizard`, `report`, `menu`, `access`).
4. Bare command = useful default; flags = control. Never require a flag.
5. Every mutating command shows what it did (tree/diff).
6. No synonyms — one verb per intent. (`why`/`impact` are one capability at two verbosity levels, not synonyms.)

### Reference (binary name TBD; placeholder `odx`, not `odev`)
```
odx init
odx run [--watch] · logs · shell · test [module]

odx new module|model|wizard|report|controller <name>
odx add field <name> --to <model>
odx add view <type> --for <model>
odx add menu "<label>" --under <parent>
odx inherit view <xmlid>

odx check [--only views,access,security]
odx report [--with-runtime]
odx fix [access|...]

odx why <field|model> · impact <field|model>
odx doctor · diff db
odx db list|new|drop|copy|dump|restore|anonymize
odx upgrade check|apply --to <version>
```

UX requirements: shell completion; teaching `--help` with examples; bare `odx` shows ~5 common commands; errors say what's wrong *and what to do next* (exactly what a `Diagnostic` carries); smart-default prompts; runtime auto-detection.

---

## 15. check, report, doctor: three distinct capabilities

Review wanted Doctor to be the flagship health report. The ambition is right, but it conflates three things; we keep them distinct and compose them.

- **`check`** — fast, static, scoped. The thing you run constantly (and in your editor). Pure engine; returns `Diagnostic`s. No DB, no install.
- **`report`** — the flagship **aggregate**: runs the full analyzer battery (manifest, models, views, security, deprecated APIs, dead/duplicate XML IDs, missing ACLs, performance, dependencies, migration readiness), composes it into a scored **HealthReport** with recommendations. Mostly static; optionally pulls in the runtime probe (`--with-runtime`). This is what you run before a release.
- **`doctor`** — the **runtime** probe specifically: spin a throwaway DB, install the module clean, report what breaks. Catches "works on my machine" / install-order / leftover-data bugs that static analysis cannot.

So the flagship is `report` (an aggregate of the focused primitives), not an overloaded `doctor`. `check` stays fast and sharp; `doctor` stays a focused runtime tool; `report` is the battery with a score.

---

## 16. The field-type → attributes matrix

The engine **never assumes a type** — flag, confirmed name-based guess, or prompt → only that type's legal follow-ups gathered into a `FieldSpec`.

### Name-based guessing (suggest, confirm)
| Pattern | Suggested |
|---------|-----------|
| `_id` | many2one |
| `_ids` | one2many / many2many |
| `is_`/`has_` | boolean |
| `_date`/`_datetime` | date/datetime |
| `amount`/`price`/`total` | monetary |

### Matrix
| Type | Required | Optional / prompted | Engine-enforced validation |
|------|----------|--------------------|----------------------------|
| char / text | — | size, required, default, help, translate | — |
| integer / float | — | (digits for float), required, default, help | — |
| monetary | currency_field | digits, required, help | no `currency_id` → offer to add |
| boolean | — | default, help | — |
| date / datetime | — | required, default, help | — |
| selection | options | required, default, help | default must be a valid key |
| many2one | comodel | ondelete, domain, required, help | comodel must exist (autocomplete) |
| one2many | comodel, inverse_name | domain, help | inverse must exist or offer to create; **error** if absent |
| many2many | comodel | relation, columns, domain, help | comodel must exist |
| binary | — | attachment, help | — |
| compute (any base) | compute method, depends | store, inverse, search | `store=True` w/o depends → **warn**; generate stub |

### Universal validations (emitted as `Diagnostic`s → every client surfaces them)
monetary w/o currency → error+fix · one2many w/o inverse → error · name collides on `_inherit` chain → warn · `required=True` no default on populated model → warn · compute `store=True` w/o depends → warn.

---

## 17. Trade-offs & alternatives

**Static analysis vs booting Odoo.** Booting gives accurate models free but needs a runnable instance + DB + deps, is slow and side-effecting, and works in neither an editor on keystroke nor CI without infra. Static (C1) is harder engineering but the only thing that works everywhere; the runtime bridge recovers ground truth *where an instance exists* (diff/doctor) as complement, not foundation. **This is the project's defining bet** (see §2).

**Builder + Resolver: separate vs merged.** Merged into one subsystem with public `RawModel→ResolvedModel` types. Separate enforced subsystems cause constant code thrash because they co-evolve; one subsystem with a movable internal seam avoids it while keeping the intermediate value inspectable.

**Cache: dedicated subsystem vs property of the pipeline.** Rejected a three-tier cache subsystem. Caching = the invalidation graph (§8) made durable via content-hash keys. A parallel cache mechanism would have to stay in sync with invalidation — a bug factory. One mechanism, two properties.

**Plugins now vs seam now.** Build the uniform analyzer contract + reserved namespaces now (free, prevents a refactor); build zero plugin machinery now (speculative). Reversibility test.

**Doctor as mega-command vs three capabilities.** Rejected overloading `doctor`. `check` (fast static), `doctor` (runtime probe), `report` (scored aggregate) are distinct; the flagship is `report` composing the others (§15).

**Full-rebuild vs incremental.** Design for incremental (C5/§8); ship full-rebuild first; activate when LSP needs it.

**"The CLI is cheap."** Rejected. Parser/resolver are the hard *logic*; the CLI's *UX* is months and it's the first product users judge.

**Freeze API early.** Rejected. Zero external consumers → freezing is ceremony. Stabilize, don't freeze; version when external dependents appear. Keep a one-time **validation gate** before MCP (§19) — "hold still to test client-neutrality," not "promise compatibility."

**Document everything up front.** Rejected. Commit only to expensive-to-reverse decisions; mark the rest TBD. (And: stop revising this RFC after Phase 0.5 produces failure data.)

**Split into many RFCs now.** Deferred, not adopted. Six cross-referencing RFCs before any code is premature document-architecture and *more* sync overhead, not less. Split *after* Phase 0.5, when the resolver section starts churning independently of the CLI-grammar section. (Likely future shape: `RFC-architecture`, `RFC-semantic-model`, `RFC-resolver`, `RFC-analyzers`, `RFC-cli-grammar`, `ROADMAP.md`.)

---

## 18. Competitive context

`odoo-bin` — official launcher; no validation/project awareness; owns the vocabulary; low ergonomics bar. `odev` — capable partner CLI; gated features; ops-flavored; single-surface. **Unclaimed space:** semantic depth + guessability + multi-surface; nobody exposes an Odoo semantic engine to editor and AI.

Lessons: Rails (coherent in-sync generation → `EditPlan`; never black-box → show the plan); Django (`makemigrations`→`migrate` preview → check/fix split); gh/cargo (consistent `noun verb`, helpful errors, one front door); stripe/vercel (polish, live tailing, health diagnostics); git (cautionary — succeeded *despite* UX, needed `gh` on top). Standing risk: scope/grammar discipline as surface grows — the API (§11) and grammar (§14) are guardrails; architecture tests enforce the split.

---

## 19. Build plan (phased)

Ordering: **validate the core assumption (0.5) → ship a real client to shape the API (0–4) → gate-check the abstraction with a different client (5) → add clients cheaply (6+).** Don't start at the most valuable piece (migration, LSP); both collapse without resolver + shaped API.

### Phase 0 — Skeleton & config
Tool runs; subcommands stubbed; `init` detects runtime, writes config; packaging gives the binary; pleasant `--help`. Engine/runtime/clients package split established physically; `test_arch/` enforces it from line one.

### Phase 0.5 — Resolver spike on real modules (validate §3)
Minimal resolver pointed at several large real addons (incl. OCA). Produce the scoreboard **and** the tagged failure catalog (fixable / hard / out-of-scope) and the static/runtime boundary. **Decision gate** per §3. This output scopes Phase 3. *Re-runnable as a permanent correctness harness.*

### Phase 1 — Daily-loop wrappers (CLI; fast wins)
`run`/`--watch`, filtered `logs`, `shell`, `db list/new/drop/copy`. Thin runtime wrappers. Dogfood ergonomics. No engine needed.

### Phase 2 — Scaffolding via EditPlan
`new module`/`new model`, prompts, Jinja2 templates, field-type matrix → `FieldSpec`. Generators return `EditPlan`; CLI applies. Shapes the `EditPlan` contract. First "wow."

### Phase 3 — The engine pipeline (hardest; the product)
Parser → Model Resolver → Semantic Index → Semantic API (§6–§7), recording `Location` (C4) and provenance (C5/§8). Index built with the full §7 reference-kind breadth. Scoped by the Phase 0.5 catalog. Most time here.

### Phase 4 — Analyzers (Diagnostics)
Uniform analyzer contract (§9). `check` + sub-checks → `Diagnostic`; `fix` → `EditPlan`; first cut of `report`. `add_field`/`new_model` upgraded to validate against the engine (§16). Shapes the `Diagnostic` contract. Earns trust.

### Phase 5 — MCP client + API validation gate
Stop adding to the API; audit for client-neutrality (anything read CLI-shaped?). Build MCP against the held-still version; its friction reveals API flaws. **The abstraction's proof:** clean drop-in = right; fights = fix the API now, before LSP/CI/web. (Validation gate, *not* a compatibility freeze.)

### Phase 6 — Runtime-backed introspection
`diff db`, `doctor`, `report --with-runtime`, `why`/`impact` polish, `db dump/restore/anonymize`. Runtime composition at the API; instantly available to CLI + MCP.

### Phase 7+ — LSP, then migration / web / CI; durable cache & incremental activation
LSP (activates §8 incrementality + durable cache; editor primitives). Migration (`upgrade check/apply`; per-version delta rulesets as data; lean on OCA migrator for boilerplate, differentiate on custom-code breakage). Web (graph viz, model browser). CI (annotations, auto-PRs). Each small because engine + API are done.

```
0    skeleton + init
0.5  RESOLVER SPIKE (scoreboard + failure catalog)   ← validate the assumption
1    run/logs/shell/db  (dogfood)
2    scaffolding (shapes EditPlan)
3    engine pipeline + rich Semantic Index            ← hardest
4    analyzers + first report (shapes Diagnostic)     ← earns trust
5    MCP client + API VALIDATION GATE                 ← proves the abstraction
6    runtime introspection (diff/doctor/report+rt)
7+   LSP · migration · web · CI · durable cache
```

*No week numbers — depends on time budget and the Phase 0.5 outcome (§23).*

---

## 20. Project layout

```
your_engine/
  pyproject.toml
  DESIGN.md                      ← this RFC  (split into RFC-* after Phase 0.5)
  ROADMAP.md
  your_engine/
    engine/                      ← PURE static semantics; no runtime/client imports
      contracts.py               ← Diagnostic, EditPlan, Location, *Info, *Spec
      api.py                     ← Engine class (public API)
      parser/                    ← files → ASTs/XML/CSV (+ Location)
      model/                     ← RawModel→ResolvedModel (builder+resolver, one subsystem)
      index/                     ← semantic/reference index (§7), XML-ID graph, invalidation
      semantic_api/              ← typed read surface (was "queries")
      analyzers/                 ← uniform Analyzer contract; views/access/security/depends/xmlids/perf…
      generators/                ← module/model/field/view/… → EditPlan
      report/                    ← HealthReport aggregation (§15)
    runtime/                     ← INFRASTRUCTURE; sibling of engine, not a dependency
      rpc.py shell.py docker.py postgres.py
    clients/
      cli/                       ← Typer + Rich (first)
      mcp/                       ← MCP server (second)
      lsp/ ci/ web/              ← later
    templates/                   ← Jinja2 (module/ model/ view/ wizard/ report/)
  tests/
    fixtures/                    ← real + planted-bug addons trees
    spike/                       ← Phase 0.5 harness (scoreboard + catalog), re-runnable
    test_model/ test_index/ test_analyzers/ test_generators/
    test_api/                    ← the contract
    test_incremental/            ← change one file → assert minimal invalidation
    test_arch/                   ← engine imports no runtime/clients; no Typer/Rich in engine
    test_cli/ test_mcp/
```

The physical split *is* the architecture; `test_arch/` fails the build on violation.

---

## 21. Configuration

Written by `init`, read by the engine, used by every client.

```yaml
# .od.yml  (illustrative; TOML vs YAML open — §23)
project: my_project
odoo:
  version: "18.0"
  runtime: docker            # docker | venv | source | odoo.sh
  addons_paths: [./addons, ./enterprise]
  source_path: null
database: { default: my_dev_db, host: localhost, port: 5432 }
docker:   { compose_file: ./docker-compose.yml, service: odoo }
run:      { http_port: 8069, dev: all }
logs:     { scope: my_module }
```
`init` detects the runtime and confirms interactively — never silently assumes.

---

## 22. Testing strategy

- **Engine fully testable without any client** — most tests call the public API.
- **Phase 0.5 harness is permanent** (`tests/spike/`) — scoreboard + catalog, re-run as the resolver improves; guards against regression.
- **Resolver fixtures from real code** — slices of large OCA/custom modules; assert effective fields/`_inherit`/`_inherits`/mixins/`xpath` resolve, including the spike's known failure modes.
- **Index fixtures** — assert each §7 reference kind is captured (a domain ref, a compute-depends ref, a report ref, etc.).
- **Analyzer fixtures with planted bugs** — each asserts the expected `Diagnostic` (right `code`, right `Location`).
- **Generator golden files** — `EditPlan`s compared; applying yields expected trees.
- **Incrementality tests** (`test_incremental/`) — change one file; assert only expected models/refs invalidated + recomputed.
- **No-write law** — assert read methods and `check/report/why/diff/doctor` never mutate the filesystem.
- **Architecture tests** — enforce engine/runtime/client separation and no-Typer-in-engine.

---

## 23. Open questions

1. **Name & binary.** Short, typeable, not `odev` (`odx` placeholder). *Blocks Phase 0.*
2. **Resolver depth for v1.** Largely answered by Phase 0.5; the failure catalog sets the line. *Scopes Phase 3.*
3. **Time budget & solo/team.** Turns phases into a calendar.
4. **Incrementality + durable cache: activation timing.** Structures from Phase 3; turn on at LSP (Phase 7). TBD.
5. **Config format.** TOML vs YAML.
6. **Runtime comms.** XML-RPC vs JSON-RPC vs `odoo shell` for diff/doctor.
7. **Web/CI transport.** In-process vs thin HTTP over the API. TBD until those clients.
8. **LSP host.** pygls in-process vs separate process. TBD until Phase 7.
9. **RFC split timing.** Split `DESIGN.md` into `RFC-*` after Phase 0.5, once sections churn at different rates.

---

## 24. Glossary

| Term | Meaning |
|------|---------|
| **Engine** | Pure static-analysis core: parser → model resolver → index → semantic API → analyzers/generators. No runtime/client awareness. |
| **Runtime** | Infrastructure talking to a live Odoo/DB/docker. Sibling of the engine, never a dependency. |
| **RawModel / ResolvedModel** | Per-module raw defs vs effective model after `_inherit`/`_inherits`/`xpath`. Public types, one subsystem. |
| **Semantic Index** | First-class reference graph capturing every kind of field/model reference (§7) + XML-ID graph; supports targeted invalidation. The differentiated core. |
| **Semantic API** | The typed read-only surface over resolved models + index (formerly "Queries"). |
| **Provenance** | Which file/module/def contributed a field; powers `why`/`impact` and incremental invalidation. |
| **Incremental recompute / cache** | One mechanism: the invalidation graph decides what's stale; durable content-hash keys let non-stale results survive restart. |
| **Analyzer contract** | One uniform interface every analyzer implements; plugin-ready seam, no plugin machinery yet. |
| **`check` / `report` / `doctor`** | Fast static checks / scored aggregate health report / runtime fresh-install probe. |
| **`why` / `impact`** | Same index query at terse / detailed verbosity. |
| **`Diagnostic` / `EditPlan` / `Location`** | Core contracts: an issue / a proposed edit set / a precise source range. |
| **Validation gate** | One-time hold on API changes to test client-neutrality via a second client — not a compatibility freeze. |
| **No-write law** | The engine never mutates user source; it returns `EditPlan`s a client may apply. |

---

*End of RFC v0.4. The defining bet is §2 (static semantics without importing Odoo); the next action is §19 Phase 0.5, not another revision. Living document — but revision should pause until the spike returns failure data.*