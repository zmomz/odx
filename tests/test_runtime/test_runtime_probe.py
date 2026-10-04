from __future__ import annotations

import pytest

from odx.engine.contracts import InstallProbe
from odx.runtime.executor import ExecResult
from odx.runtime.probe import PROBE_DB_PREFIX, install_probe
from odx.runtime.settings import RuntimeSettings

S = RuntimeSettings(runtime="docker")


class FakeRunner:
    def __init__(self, install_rc=0, drop_rc=0, log="INFO loaded\n"):
        self.calls = []
        self.install_rc, self.drop_rc, self.log = install_rc, drop_rc, log

    def __call__(self, cmd, *, capture=False):
        self.calls.append((cmd, capture))
        if "dropdb" in cmd.argv:
            return ExecResult(cmd, self.drop_rc, stderr="" if not self.drop_rc else "busy")
        return ExecResult(cmd, self.install_rc, stdout=self.log)


def test_probe_installs_then_drops():
    r = FakeRunner(log="2024 ... ERROR odx_doctor_x odoo.modules.loading: boom\n")
    probe = install_probe(S, ["sale", "my_mod", "sale"], runner=r)
    assert isinstance(probe, InstallProbe)
    assert probe.database.startswith(PROBE_DB_PREFIX)
    assert probe.modules == ("sale", "my_mod")
    assert probe.returncode == 0 and "boom" in probe.log
    install, drop = r.calls
    assert install[1] is True and drop[1] is True
    a = install[0].argv
    assert a[a.index("-d") + 1] == probe.database
    assert a[a.index("-i") + 1] == "sale,my_mod"
    assert "--stop-after-init" in a and "--log-level=info" in a
    assert probe.command == a
    assert drop[0].argv[-1] == probe.database


def test_probe_keep_db_and_explicit_name():
    r = FakeRunner(install_rc=1)
    probe = install_probe(S, ["m"], db_name="mine", keep_db=True, runner=r)
    assert probe.database == "mine" and probe.returncode == 1
    assert len(r.calls) == 1


def test_probe_drop_failure_is_reported_in_log():
    probe = install_probe(S, ["m"], runner=FakeRunner(drop_rc=1))
    assert "could not drop probe database" in probe.log


def test_probe_drops_even_if_runner_raises():
    calls = []

    def runner(cmd, *, capture=False):
        calls.append(cmd)
        if "dropdb" not in cmd.argv:
            raise RuntimeError("boom")
        return ExecResult(cmd, 0)
    with pytest.raises(RuntimeError):
        install_probe(S, ["m"], runner=runner)
    assert "dropdb" in calls[-1].argv


def test_probe_requires_modules():
    with pytest.raises(ValueError):
        install_probe(S, [" ", ""], runner=FakeRunner())


def test_probe_names_are_unique():
    names = {install_probe(S, ["m"], runner=FakeRunner()).database for _ in range(5)}
    assert len(names) == 5
