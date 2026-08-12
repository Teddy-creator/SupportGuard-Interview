from __future__ import annotations

from sqlalchemy import ForeignKeyConstraint, UniqueConstraint

from current_predicate_facts import record_predicate_operands
from supportguard.db.models import _V124_TENANT_REFERENCES, Base


def test_every_frozen_tenant_reference_is_composite_in_orm_metadata() -> None:
    verified = []
    for child_name, local_id, parent_name in _V124_TENANT_REFERENCES:
        child = Base.metadata.tables[child_name]
        matches = [
            constraint
            for constraint in child.constraints
            if isinstance(constraint, ForeignKeyConstraint)
            and {column.name for column in constraint.columns} == {"tenant_id", local_id}
            and {element.column.table.name for element in constraint.elements} == {parent_name}
        ]
        assert len(matches) == 1, (child_name, local_id, parent_name)
        verified.append([child_name, local_id, parent_name])
    record_predicate_operands(
        requirement_id="C4-P0-08a",
        predicate_id="c4_p0_08a",
        subject_kind="orm_tenant_reference_contract",
        operands={
            "frozen_reference_count": len(_V124_TENANT_REFERENCES),
            "verified_reference_count": len(verified),
            "verified_references": verified,
        },
    )


def test_every_composite_parent_has_a_tenant_candidate_key() -> None:
    parent_names = {parent for _, _, parent in _V124_TENANT_REFERENCES}
    for parent_name in parent_names:
        parent = Base.metadata.tables[parent_name]
        candidate_keys = [
            {column.name for column in constraint.columns}
            for constraint in parent.constraints
            if isinstance(constraint, UniqueConstraint)
        ]
        assert {"tenant_id", "id"} in candidate_keys, parent_name
