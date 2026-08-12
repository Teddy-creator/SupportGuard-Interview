from __future__ import annotations

from pathlib import Path

import yaml

from supportguard.validation.public_mirror import validate_public_git_boundary

PGVECTOR_IMAGE = (
    "pgvector/pgvector@sha256:"
    "1d533553fefe4f12e5d80c7b80622ba0c382abb5758856f52983d8789179f0fb"
)


def test_public_mirror_excludes_private_history_and_binds_source_snapshot() -> None:
    result = validate_public_git_boundary(Path.cwd())

    assert result["result"] == "pass"
    assert result["source_commit"] == "a528a19d1b00c5699af9f5a87f12bd515c1a834d"
    assert result["source_tree"] == "ba50b72e014e85fb60c85ad9eb8ae253077ac817"
    assert result["public_repository"] == "Teddy-creator/SupportGuard-Interview"
    assert result["private_source_reachable"] is False
    assert result["tag_count"] == 0


def test_public_ci_runs_baseline_upgrade_as_the_migrator_role() -> None:
    workflow = yaml.safe_load(Path(".github/workflows/ci.yml").read_text(encoding="utf-8"))
    integration = workflow["jobs"]["integration"]
    migration = next(
        step
        for step in integration["steps"]
        if step.get("name") == "Empty database Interview baseline migration and schema drift"
    )

    assert integration["env"]["DATABASE_URL"].startswith(
        "postgresql+asyncpg://supportguard:supportguard@"
    )
    assert migration["env"]["DATABASE_URL"].startswith(
        "postgresql+asyncpg://supportguard_migrator:supportguard_migrator@"
    )
    assert integration["services"]["postgres"]["image"] == PGVECTOR_IMAGE
    assert migration["run"].count("supportguard db baseline-upgrade") == 2
    assert "alembic -c alembic-interview.ini check" not in migration["run"]


def test_public_compose_pins_the_phase3_catalog_image() -> None:
    compose = yaml.safe_load(Path("docker-compose.yml").read_text(encoding="utf-8"))

    assert compose["services"]["postgres"]["image"] == PGVECTOR_IMAGE
