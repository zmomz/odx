"""The engine — pure static semantics (RFC §6).

Pipeline: parser -> model (RawModel->ResolvedModel) -> index -> semantic_api
          -> analyzers / generators.

Hard rule (C6): nothing under engine/ may import from odx.runtime or odx.clients.
Enforced by tests/test_arch/.
"""
