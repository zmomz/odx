"""XML-RPC introspection of a live Odoo instance (RFC §12, Phase 6).

Produces `DbSnapshot` (an engine contract) so `diff db` can compare what the
code defines with what the instance has. Snapshots also round-trip through
JSON so the diff works offline (`odx diff db --snapshot file.json`).

The transport is injectable (`proxy_factory`) so tests never need a server.
"""

from __future__ import annotations

import json
import xmlrpc.client
from pathlib import Path
from typing import Any, Callable

from odx.engine.contracts import DbField, DbModel, DbSnapshot
from odx.runtime.settings import RuntimeSettings

ProxyFactory = Callable[[str], Any]

SNAPSHOT_FORMAT = 1


class RpcError(RuntimeError):
    """Talking to the instance failed (connection, auth, or a server fault)."""


def _default_factory(url: str) -> Any:
    return xmlrpc.client.ServerProxy(url, allow_none=True)


def _modules(value: Any) -> tuple[str, ...]:
    """ir.model(.fields).modules is a comma-separated char field."""
    if not value:
        return ()
    if isinstance(value, (list, tuple)):
        return tuple(str(v) for v in value)
    return tuple(m.strip() for m in str(value).split(",") if m.strip())


class OdooRpc:
    def __init__(
        self,
        url: str,
        db: str,
        user: str,
        password: str,
        *,
        proxy_factory: ProxyFactory | None = None,
        batch_size: int = 5000,
    ) -> None:
        self.url = url.rstrip("/")
        self.db = db
        self.user = user
        self.password = password
        self.batch_size = batch_size
        self._factory = proxy_factory or _default_factory
        self._uid: int | None = None
        self._models: Any = None

    @classmethod
    def from_settings(
        cls, settings: "RuntimeSettings", *, user: str = "admin", password: str = "admin",
        url: str | None = None, db: str | None = None, proxy_factory: ProxyFactory | None = None,
    ) -> "OdooRpc":
        """Client for the project's local instance (http://localhost:<http_port>)."""
        return cls(url or f"http://localhost:{settings.http_port}", db or settings.db_name,
                   user, password, proxy_factory=proxy_factory)

    # ------------------------------------------------------------------ #
    def login(self) -> int:
        if self._uid is not None:
            return self._uid
        try:
            common = self._factory(f"{self.url}/xmlrpc/2/common")
            uid = common.authenticate(self.db, self.user, self.password, {})
        except (OSError, xmlrpc.client.Error) as exc:
            raise RpcError(f"cannot reach Odoo at {self.url}: {exc}") from exc
        if not uid:
            raise RpcError(f"authentication failed for {self.user!r} on database {self.db!r}")
        self._uid = int(uid)
        self._models = self._factory(f"{self.url}/xmlrpc/2/object")
        return self._uid

    def call(self, model: str, method: str, *args: Any, **kwargs: Any) -> Any:
        uid = self.login()
        try:
            return self._models.execute_kw(self.db, uid, self.password, model, method,
                                           list(args), kwargs)
        except xmlrpc.client.Fault as exc:
            raise RpcError(f"{model}.{method} failed: {exc.faultString}") from exc
        except (OSError, xmlrpc.client.Error) as exc:
            raise RpcError(f"{model}.{method} failed: {exc}") from exc

    def search_read_all(self, model: str, domain: list, fields: list[str]) -> list[dict]:
        """search_read in id-ordered batches (ir.model.data can be huge)."""
        out: list[dict] = []
        offset = 0
        while True:
            rows = self.call(model, "search_read", domain, fields=fields,
                             offset=offset, limit=self.batch_size, order="id")
            out.extend(rows)
            if len(rows) < self.batch_size:
                return out
            offset += self.batch_size

    # ------------------------------------------------------------------ #
    def snapshot(self) -> DbSnapshot:
        model_rows = self.search_read_all("ir.model", [], ["model", "modules", "transient"])
        field_rows = self.search_read_all(
            "ir.model.fields", [],
            ["model", "name", "ttype", "relation", "required", "store", "modules"],
        )
        module_rows = self.search_read_all(
            "ir.module.module", [("state", "=", "installed")], ["name", "state"],
        )
        data_rows = self.search_read_all("ir.model.data", [], ["module", "name"])

        fields_by_model: dict[str, dict[str, DbField]] = {}
        for r in field_rows:
            f = DbField(
                name=r["name"],
                type=r.get("ttype") or "",
                relation=r.get("relation") or None,
                required=bool(r.get("required")),
                store=bool(r.get("store", True)),
                modules=_modules(r.get("modules")),
            )
            fields_by_model.setdefault(r["model"], {})[f.name] = f

        models: dict[str, DbModel] = {}
        for r in model_rows:
            name = r["model"]
            models[name] = DbModel(
                name=name,
                fields=dict(sorted(fields_by_model.get(name, {}).items())),
                modules=_modules(r.get("modules")),
                transient=bool(r.get("transient")),
            )

        return DbSnapshot(
            database=self.db,
            models=dict(sorted(models.items())),
            installed_modules={r["name"]: r.get("state", "installed")
                               for r in sorted(module_rows, key=lambda r: r["name"])},
            xmlids=tuple(sorted(f"{r['module']}.{r['name']}" for r in data_rows)),
        )


# =========================================================================== #
# offline snapshots
# =========================================================================== #
def snapshot_to_dict(snap: DbSnapshot) -> dict:
    return {
        "format": SNAPSHOT_FORMAT,
        "database": snap.database,
        "installed_modules": dict(snap.installed_modules),
        "xmlids": list(snap.xmlids),
        "models": {
            name: {
                "modules": list(m.modules),
                "transient": m.transient,
                "fields": {
                    fname: {
                        "type": f.type,
                        "relation": f.relation,
                        "required": f.required,
                        "store": f.store,
                        "modules": list(f.modules),
                    }
                    for fname, f in m.fields.items()
                },
            }
            for name, m in snap.models.items()
        },
    }


def snapshot_from_dict(data: dict) -> DbSnapshot:
    fmt = data.get("format", SNAPSHOT_FORMAT)
    if fmt != SNAPSHOT_FORMAT:
        raise ValueError(f"unsupported snapshot format {fmt!r} (expected {SNAPSHOT_FORMAT})")
    models: dict[str, DbModel] = {}
    for name, m in (data.get("models") or {}).items():
        fields = {
            fname: DbField(
                name=fname,
                type=f.get("type", ""),
                relation=f.get("relation"),
                required=bool(f.get("required", False)),
                store=bool(f.get("store", True)),
                modules=tuple(f.get("modules", ())),
            )
            for fname, f in (m.get("fields") or {}).items()
        }
        models[name] = DbModel(name=name, fields=fields,
                               modules=tuple(m.get("modules", ())),
                               transient=bool(m.get("transient", False)))
    return DbSnapshot(
        database=data.get("database", ""),
        models=models,
        installed_modules=dict(data.get("installed_modules") or {}),
        xmlids=tuple(data.get("xmlids") or ()),
    )


def snapshot_to_json(snap: DbSnapshot, path: str | Path) -> Path:
    path = Path(path)
    path.write_text(json.dumps(snapshot_to_dict(snap), indent=2, sort_keys=True) + "\n",
                    encoding="utf-8")
    return path


def snapshot_from_json(path: str | Path) -> DbSnapshot:
    with Path(path).open(encoding="utf-8") as fh:
        return snapshot_from_dict(json.load(fh))
