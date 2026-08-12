from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from supportguard.cli import build_parser as build_runtime_parser
from supportguard.evals.gate import EvaluationGateError, recompute_evaluation_status
from supportguard.validation.cli import build_parser, evaluation_status, main


def test_evaluation_status_reports_no_active_candidate_dataset() -> None:
    status = evaluation_status()
    assert status["active_dataset"] is None
    assert status["candidate_eligible"] is False
    assert status["execution_allowed"] is False
    assert status["contracts"] == {
        "rag_dev30": "frozen_unexecuted",
        "ie_f06": "frozen_unexecuted",
        "ie_p16": "frozen_unexecuted",
        "ie_j12": "frozen_unexecuted",
    }
    assert status["denominators"] == {
        "rag_dev30": 30,
        "ie_f06": 6,
        "ie_p16": 16,
        "ie_j12": 12,
    }
    assert status["protected_holdout"] == "not_accessed"
    assert status["cross_encoder"] == "not_executed"


def test_eval_validate_cli_is_truthful(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    monkeypatch.setattr(sys, "argv", ["supportguard-validation", "eval", "validate"])
    main()
    output = json.loads(capsys.readouterr().out)
    assert output == evaluation_status()


@pytest.mark.parametrize(
    ("command", "extra"),
    [
        ("rag-dev30", []),
        ("ie-f06", []),
        ("ie-p16", []),
        ("ie-j12", []),
    ],
)
def test_candidate_evaluation_commands_fail_closed(
    command: str, extra: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden_path_effect(*args: object, **kwargs: object) -> None:
        raise AssertionError(f"blocked route touched a file: {args!r} {kwargs!r}")

    for method in ("open", "read_text", "read_bytes", "write_text", "write_bytes"):
        monkeypatch.setattr(Path, method, forbidden_path_effect)
    monkeypatch.setattr(sys, "argv", ["supportguard-validation", "eval", command, *extra])
    with pytest.raises(EvaluationGateError, match="before artifact access"):
        main()


def test_status_recomputation_fails_closed_when_evidence_changes(tmp_path: Path) -> None:
    root = Path.cwd()
    for relative in (
        "validation/contracts/interview_v2/rag-dev30.contract.v1.json",
        "validation/contracts/interview_v2/rag-dev30.v1.jsonl",
        "validation/contracts/interview_v2/ie-f06.v1.json",
        "validation/contracts/interview_v2/ie-p16.v1.json",
        "validation/contracts/interview_v2/ie-j12.v1.json",
    ):
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((root / relative).read_bytes())
    contract_path = tmp_path / "validation/contracts/interview_v2/ie-p16.v1.json"
    contract = json.loads(contract_path.read_text())
    contract["execution_state"] = "passed"
    contract_path.write_text(json.dumps(contract))
    with pytest.raises(EvaluationGateError, match="identity mismatch"):
        recompute_evaluation_status(tmp_path)


def test_parser_eval_routes_match_frozen_inventory() -> None:
    parser = build_parser()
    eval_action = next(action for action in parser._actions if action.dest == "command")
    eval_parser = eval_action.choices["eval"]
    command_action = next(
        action for action in eval_parser._actions if action.dest == "eval_command"
    )
    exposed = set(command_action.choices)
    assert exposed == {
        "validate",
        "rag-dev30",
        "ie-f06",
        "ie-p16",
        "ie-j12",
    }


def test_runtime_parser_does_not_expose_validation_routes() -> None:
    parser = build_runtime_parser()
    command_action = next(action for action in parser._actions if action.dest == "command")
    assert "eval" not in command_action.choices
