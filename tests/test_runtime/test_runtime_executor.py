from __future__ import annotations

import sys

from odx.runtime.commands import Command
from odx.runtime.executor import LogFilter, execute, filter_log_line, parse_log_line

PY = sys.executable

SALE = "2024-01-01 10:00:00,123 1234 INFO mydb odoo.addons.sale.models.sale_order: hello"
SALE_WARN = "2024-01-01 10:00:00,123 1234 WARNING mydb odoo.addons.sale: careful"
SALE_EXT = "2024-01-01 10:00:00,123 1234 ERROR mydb odoo.addons.sale_ext.models.x: boom"
BASE = "2024-01-01 10:00:00,123 1234 DEBUG ? odoo.modules.loading: loading"
TRACE = "Traceback (most recent call last):"


# --------------------------------------------------------------------------- #
# log parsing/filtering
# --------------------------------------------------------------------------- #
def test_parse_log_line():
    rec = parse_log_line(SALE)
    assert rec.level == "INFO" and rec.db == "mydb"
    assert rec.logger == "odoo.addons.sale.models.sale_order" and rec.message == "hello"
    assert parse_log_line(BASE).db == "?"
    assert parse_log_line(TRACE) is None


def test_filter_no_filters_keeps_everything():
    for line in (SALE, BASE, TRACE, ""):
        assert filter_log_line(line)


def test_filter_scope_is_exact_module_not_prefix():
    assert filter_log_line(SALE, scope="sale")
    assert filter_log_line(SALE_WARN, scope="sale")
    assert not filter_log_line(SALE_EXT, scope="sale")       # sale_ext != sale
    assert filter_log_line(SALE_EXT, scope="sale_ext")
    assert not filter_log_line(BASE, scope="sale")
    assert not filter_log_line(TRACE, scope="sale")


def test_filter_level_threshold():
    assert not filter_log_line(SALE, level="warning")
    assert filter_log_line(SALE_WARN, level="warning")
    assert filter_log_line(SALE_EXT, level="WARNING")
    assert filter_log_line(BASE, level="debug")
    assert not filter_log_line(BASE, level="info")
    assert filter_log_line(TRACE, level="error")
    assert not filter_log_line(TRACE, level="critical")


def test_filter_scope_and_level():
    assert filter_log_line(SALE_WARN, scope="sale", level="warning")
    assert not filter_log_line(SALE, scope="sale", level="warning")


def test_stateful_filter_keeps_tracebacks_with_their_record():
    f = LogFilter(scope="sale_ext")
    lines = [SALE, TRACE, SALE_EXT, TRACE, '  File "x.py"', SALE]
    assert [line for line in lines if f(line)] == [SALE_EXT, TRACE, '  File "x.py"']


# --------------------------------------------------------------------------- #
# execute
# --------------------------------------------------------------------------- #
def test_dry_run_runs_nothing(tmp_path):
    marker = tmp_path / "m"
    cmd = Command(argv=(PY, "-c", f"open({str(marker)!r}, 'w')"))
    res = execute(cmd, dry_run=True)
    assert res.ok and res.dry_run and not marker.exists()


def test_missing_binary_returns_127_with_hint():
    res = execute(Command(argv=("definitely-not-a-binary-odx",)), capture=True)
    assert res.returncode == 127
    assert "not found" in res.message and "definitely-not-a-binary-odx" in res.message


def test_missing_psql_hint():
    res = execute(Command(argv=("/nonexistent/psql", "-l")))
    assert res.returncode == 127 and "postgresql-client" in res.message


def test_capture_env_and_returncode():
    cmd = Command(argv=(PY, "-c", "import os,sys; print(os.environ['ODX_T']); sys.exit(3)"),
                  env={"ODX_T": "v"})
    res = execute(cmd, capture=True)
    assert res.returncode == 3 and res.stdout.strip() == "v"


def test_stdin_text():
    cmd = Command(argv=(PY, "-c", "import sys; print(sys.stdin.read().upper())"), stdin="abc")
    assert execute(cmd, capture=True).stdout.strip() == "ABC"


def test_stdout_and_stdin_files(tmp_path):
    src = tmp_path / "in.bin"
    src.write_bytes(b"payload")
    out = tmp_path / "out.bin"
    cmd = Command(argv=(PY, "-c", "import sys; sys.stdout.write(sys.stdin.read()[::-1])"),
                  stdin_file=src, stdout_file=out)
    assert execute(cmd).ok
    assert out.read_bytes() == b"daolyap"


def test_missing_stdin_file_is_an_error_not_a_crash(tmp_path):
    res = execute(Command(argv=(PY, "-c", "pass"), stdin_file=tmp_path / "nope"))
    assert res.returncode == 2 and "cannot open" in res.message


def test_stream_with_filter():
    script = f"print({SALE!r}); print({SALE_EXT!r}); print({BASE!r})"
    seen: list[str] = []
    res = execute(Command(argv=(PY, "-c", script)), on_line=seen.append,
                  line_filter=lambda line: filter_log_line(line, scope="sale"), capture=True)
    assert res.ok
    assert seen == [SALE]
    assert res.stdout.count("\n") == 3


def test_stream_missing_binary():
    res = execute(Command(argv=("definitely-not-a-binary-odx",)), on_line=print)
    assert res.returncode == 127
