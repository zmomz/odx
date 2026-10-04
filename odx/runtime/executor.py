"""Run `Command`s (RFC §12) and filter Odoo logs.

`execute` never raises for an environment problem (missing binary, unreadable
dump file): it returns an `ExecResult` with a conventional return code and a
message telling the user what to do next.
"""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from typing import Callable

from odx.runtime.commands import Command

LineFilter = Callable[[str], bool]
LineSink = Callable[[str], None]


@dataclass(frozen=True)
class ExecResult:
    command: Command
    returncode: int
    stdout: str = ""
    stderr: str = ""
    dry_run: bool = False
    message: str = ""                # human hint when odx itself failed to launch

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    @property
    def output(self) -> str:
        return self.stdout + self.stderr


_MISSING_HINT = {
    "docker": "install Docker (with the compose plugin) or change [odoo] runtime in .odx.toml",
    "psql": "install the PostgreSQL client tools (e.g. `apt install postgresql-client`)",
    "createdb": "install the PostgreSQL client tools (e.g. `apt install postgresql-client`)",
    "dropdb": "install the PostgreSQL client tools (e.g. `apt install postgresql-client`)",
    "pg_dump": "install the PostgreSQL client tools (e.g. `apt install postgresql-client`)",
    "pg_restore": "install the PostgreSQL client tools (e.g. `apt install postgresql-client`)",
}


def _missing(cmd: Command, exc: OSError) -> ExecResult:
    binary = cmd.argv[0] if cmd.argv else "?"
    name = os.path.basename(binary)
    hint = _MISSING_HINT.get(name, "check [odoo] runtime / odoo_bin / python in .odx.toml")
    msg = f"`{binary}` not found or not executable ({exc.strerror or exc}); {hint}."
    return ExecResult(command=cmd, returncode=127, stderr=msg, message=msg)


def execute(
    cmd: Command,
    *,
    dry_run: bool = False,
    capture: bool = False,
    on_line: LineSink | None = None,
    line_filter: LineFilter | None = None,
) -> ExecResult:
    """Run `cmd`.

    - dry_run: do nothing, return rc 0 (the caller prints `cmd.display()`).
    - on_line: stream merged stdout/stderr line by line to the sink (after
      `line_filter`); the full output is also returned when `capture`.
    - capture: collect output instead of inheriting the terminal.
    - otherwise: inherit stdio (interactive commands like shell/run).
    """
    if dry_run:
        return ExecResult(command=cmd, returncode=0, dry_run=True)
    if not cmd.argv:
        return ExecResult(command=cmd, returncode=2, message="empty command")

    env = {**os.environ, **cmd.env} if cmd.env else None
    cwd = str(cmd.cwd) if cmd.cwd and cmd.cwd.is_dir() else None

    stdin_fh = stdout_fh = None
    try:
        try:
            if cmd.stdin_file is not None:
                stdin_fh = open(cmd.stdin_file, "rb")
            if cmd.stdout_file is not None:
                stdout_fh = open(cmd.stdout_file, "wb")
        except OSError as exc:
            msg = f"cannot open {exc.filename}: {exc.strerror}"
            return ExecResult(command=cmd, returncode=2, stderr=msg, message=msg)

        try:
            if on_line is not None and stdout_fh is None:
                return _stream(cmd, env, cwd, stdin_fh, on_line, line_filter, capture)

            if stdout_fh is not None:
                proc = subprocess.run(
                    cmd.argv, env=env, cwd=cwd, stdin=stdin_fh, stdout=stdout_fh,
                    stderr=subprocess.PIPE if capture else None,
                )
                err = proc.stderr.decode(errors="replace") if capture and proc.stderr else ""
                return ExecResult(command=cmd, returncode=proc.returncode, stderr=err)

            if capture:
                proc = subprocess.run(
                    cmd.argv, env=env, cwd=cwd,
                    input=None if stdin_fh else (cmd.stdin.encode() if cmd.stdin is not None else None),
                    stdin=stdin_fh if stdin_fh else (None if cmd.stdin is not None else subprocess.DEVNULL),
                    capture_output=True,
                )
                return ExecResult(command=cmd, returncode=proc.returncode,
                                  stdout=proc.stdout.decode(errors="replace"),
                                  stderr=proc.stderr.decode(errors="replace"))

            proc = subprocess.run(
                cmd.argv, env=env, cwd=cwd,
                input=None if stdin_fh else (cmd.stdin.encode() if cmd.stdin is not None else None),
                stdin=stdin_fh,
            )
            return ExecResult(command=cmd, returncode=proc.returncode)
        except (FileNotFoundError, PermissionError) as exc:
            return _missing(cmd, exc)
        except KeyboardInterrupt:
            return ExecResult(command=cmd, returncode=130, message="interrupted")
    finally:
        for fh in (stdin_fh, stdout_fh):
            if fh is not None:
                fh.close()


def _stream(cmd, env, cwd, stdin_fh, on_line, line_filter, capture) -> ExecResult:
    proc = subprocess.Popen(
        cmd.argv, env=env, cwd=cwd,
        stdin=stdin_fh if stdin_fh else (subprocess.PIPE if cmd.stdin is not None else subprocess.DEVNULL),
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, errors="replace", bufsize=1,
    )
    if cmd.stdin is not None and not stdin_fh and proc.stdin:
        proc.stdin.write(cmd.stdin)
        proc.stdin.close()
    collected: list[str] = []
    try:
        assert proc.stdout is not None
        for raw in proc.stdout:
            line = raw.rstrip("\n")
            if capture:
                collected.append(raw)
            if line_filter is None or line_filter(line):
                on_line(line)
        rc = proc.wait()
    except KeyboardInterrupt:
        proc.terminate()
        proc.wait()
        return ExecResult(command=cmd, returncode=130, stdout="".join(collected), message="interrupted")
    return ExecResult(command=cmd, returncode=rc, stdout="".join(collected))


# =========================================================================== #
# log filtering
# =========================================================================== #
# 2024-01-01 10:00:00,123 1234 INFO dbname odoo.addons.sale.models.x: msg
LOG_RE = re.compile(
    r"^(?P<date>\d{4}-\d{2}-\d{2}) (?P<time>\d{2}:\d{2}:\d{2},\d{3}) "
    r"(?P<pid>\d+) (?P<level>[A-Z]+) (?P<db>\S+) (?P<logger>[\w.]+): (?P<msg>.*)$"
)

LEVELS = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "WARN": 30, "ERROR": 40, "CRITICAL": 50}


@dataclass(frozen=True)
class LogRecord:
    level: str
    db: str
    logger: str
    message: str


def parse_log_line(line: str) -> LogRecord | None:
    m = LOG_RE.match(line.rstrip("\n"))
    if not m:
        return None
    return LogRecord(level=m["level"], db=m["db"], logger=m["logger"], message=m["msg"])


def logger_in_scope(logger: str, scope: str) -> bool:
    prefix = f"odoo.addons.{scope}"
    return logger == prefix or logger.startswith(prefix + ".")


def filter_log_line(line: str, scope: str | None = None, level: str | None = None) -> bool:
    """Keep `line`? Pure.

    - scope: keep records whose logger is `odoo.addons.<scope>[.*]`.
    - level: keep records at or above this level (DEBUG/INFO/WARNING/ERROR/CRITICAL).
    Lines that aren't log records (traceback continuation lines) carry no
    logger, so with a scope they are dropped; with only a level they are kept
    while the threshold admits ERROR (tracebacks belong to error records).
    `LogFilter` is the stateful variant that attaches them to their record.
    """
    if not scope and not level:
        return True
    rec = parse_log_line(line)
    threshold = LEVELS.get(level.upper(), 0) if level else 0
    if rec is None:
        # Continuation lines (tracebacks) — keep them only without a scope
        # filter and when the threshold still admits errors.
        return not scope and threshold <= LEVELS["ERROR"]
    if scope and not logger_in_scope(rec.logger, scope):
        return False
    if level and LEVELS.get(rec.level, 0) < threshold:
        return False
    return True


class LogFilter:
    """Stateful variant: continuation lines follow their record's verdict.

    Use as `line_filter` when streaming so a matching ERROR's traceback is
    shown and a non-matching record's traceback is hidden.
    """

    def __init__(self, scope: str | None = None, level: str | None = None) -> None:
        self.scope = scope
        self.level = level
        self._last = True

    def __call__(self, line: str) -> bool:
        if parse_log_line(line) is None and (self.scope or self.level):
            return self._last
        self._last = filter_log_line(line, self.scope, self.level)
        return self._last
