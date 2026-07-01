"""Phase 0.5 resolver spike (RFC §3, §19).

A minimal, deliberately rough resolver pointed at real Odoo modules to find the
boundary between statically resolvable and needs-a-fallback. NOT the Phase 3
engine — it is a throwaway-grade probe that graduates into `odx/engine/` once the
catalog says the approach holds. Kept permanently as a correctness harness
(RFC §22): re-run as the resolver improves; guards against regression.
"""
