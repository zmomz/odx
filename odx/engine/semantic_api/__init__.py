"""semantic_api — the typed read-only surface over resolved models + index.

Formerly "queries" (renamed in RFC §6 to avoid reading as SQL). The only thing
analyzers and generators consult: field_exists, references_to, get_model, ...
Lands in Phase 3.
"""
