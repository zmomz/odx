"""generators — module/model/field/view/... -> EditPlan (RFC §10, §16).

Never writes; describes edits. Uses the field-type -> attributes matrix (§16) to
gather only legal follow-ups into a FieldSpec. Lands in Phase 2.
"""
