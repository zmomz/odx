"""JSON-RPC 2.0 over the LSP base protocol (Content-Length framed), stdlib only."""

from __future__ import annotations

import json
from typing import Any, BinaryIO


def read_message(stream: BinaryIO) -> dict | None:
    headers: dict[str, str] = {}
    while True:
        line = stream.readline()
        if not line:
            return None
        line = line.decode("ascii", "replace").strip()
        if not line:
            break
        k, _, v = line.partition(":")
        headers[k.strip().lower()] = v.strip()
    n = int(headers.get("content-length", "0"))
    body = stream.read(n) if n else b""
    if not body:
        return None
    return json.loads(body.decode("utf-8"))


def write_message(stream: BinaryIO, msg: dict[str, Any]) -> None:
    body = json.dumps(msg, ensure_ascii=False).encode("utf-8")
    stream.write(f"Content-Length: {len(body)}\r\n\r\n".encode("ascii") + body)
    stream.flush()
