"""model — RawModel -> ResolvedModel (builder + resolver, ONE subsystem).

RawModel/ResolvedModel are public, inspectable data *types* sharing one package
with a freely-movable internal seam (RFC §6, §17). Applies _inherit / _inherits
/ xpath to produce effective models with provenance. Lands in Phase 3.
"""
