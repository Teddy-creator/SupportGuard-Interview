from __future__ import annotations

from pathlib import Path

from supportguard.validation.public_mirror import validate_public_git_boundary


def test_public_mirror_excludes_private_history_and_binds_source_snapshot() -> None:
    result = validate_public_git_boundary(Path.cwd())

    assert result["result"] == "pass"
    assert result["source_commit"] == "a528a19d1b00c5699af9f5a87f12bd515c1a834d"
    assert result["source_tree"] == "ba50b72e014e85fb60c85ad9eb8ae253077ac817"
    assert result["public_repository"] == "Teddy-creator/SupportGuard-Interview"
    assert result["private_source_reachable"] is False
    assert result["tag_count"] == 0
