from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[2]
SHA = "a" * 40
JOBS = {"backend", "integration", "frontend", "product-e2e", "image"}


def _module() -> ModuleType:
    path = ROOT / "scripts" / "record_hosted_ci.py"
    spec = importlib.util.spec_from_file_location("record_hosted_ci", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _payload(*, conclusion: str, steps: list[dict[str, str]], messages: list[str]) -> dict:
    return {
        "databaseId": 123,
        "attempt": 1,
        "headSha": SHA,
        "headBranch": "main",
        "event": "push",
        "status": "completed",
        "conclusion": conclusion,
        "url": "https://github.com/Teddy-creator/SupportGuard/actions/runs/123",
        "workflowName": "CI",
        "jobs": [
            {
                "databaseId": index,
                "name": name,
                "status": "completed",
                "conclusion": conclusion,
                "steps": steps,
                "annotation_messages": messages,
            }
            for index, name in enumerate(sorted(JOBS), start=1)
        ],
    }


def test_hosted_ci_success_requires_real_steps_and_exact_sha() -> None:
    module = _module()
    receipt = module.normalize_run(
        _payload(
            conclusion="success",
            steps=[{"name": "Run tests", "status": "completed", "conclusion": "success"}],
            messages=[],
        ),
        expected_sha=SHA,
    )

    assert receipt["classification"] == "completed_success"
    assert receipt["claims"] == {
        "hosted_execution_started": True,
        "local_execution_used_as_substitute": False,
        "release_blocker": False,
    }


def test_hosted_ci_zero_step_billing_failure_is_external_blocker() -> None:
    module = _module()
    receipt = module.normalize_run(
        _payload(
            conclusion="failure",
            steps=[],
            messages=["The job was not started because your spending limit needs to be increased."],
        ),
        expected_sha=SHA,
    )

    assert receipt["classification"] == "external_zero_step_blocker"
    assert receipt["claims"]["hosted_execution_started"] is False
    assert receipt["claims"]["release_blocker"] is True


def test_hosted_ci_one_zero_step_job_cannot_be_reported_as_success() -> None:
    module = _module()
    payload = _payload(
        conclusion="success",
        steps=[{"name": "Run tests", "status": "completed", "conclusion": "success"}],
        messages=[],
    )
    payload["jobs"][0]["steps"] = []

    receipt = module.normalize_run(payload, expected_sha=SHA)

    assert receipt["classification"] == "workflow_failure"
    assert receipt["claims"]["hosted_execution_started"] is True
    assert receipt["claims"]["release_blocker"] is True


def test_hosted_ci_all_zero_step_jobs_need_only_one_trusted_account_annotation() -> None:
    module = _module()
    payload = _payload(conclusion="failure", steps=[], messages=[])
    payload["jobs"][0]["annotation_messages"] = [
        "The job was not started because your spending limit needs to be increased."
    ]

    receipt = module.normalize_run(payload, expected_sha=SHA)

    assert receipt["classification"] == "external_zero_step_blocker"
    assert receipt["claims"]["hosted_execution_started"] is False


def test_hosted_ci_partial_execution_is_not_an_external_zero_step_blocker() -> None:
    module = _module()
    payload = _payload(
        conclusion="failure",
        steps=[],
        messages=["The job was not started because actions usage is disabled."],
    )
    payload["jobs"][0]["steps"] = [
        {"name": "Run tests", "status": "completed", "conclusion": "failure"}
    ]

    receipt = module.normalize_run(payload, expected_sha=SHA)

    assert receipt["classification"] == "workflow_failure"
    assert receipt["claims"]["hosted_execution_started"] is True


def test_hosted_ci_generic_runner_text_is_not_a_trusted_quota_blocker() -> None:
    module = _module()
    payload = _payload(
        conclusion="failure",
        steps=[],
        messages=["This workflow uses a GitHub-hosted runner."],
    )

    receipt = module.normalize_run(payload, expected_sha=SHA)

    assert receipt["classification"] == "workflow_failure"
    assert receipt["claims"]["hosted_execution_started"] is False


def test_hosted_ci_rejects_a_different_candidate_sha() -> None:
    module = _module()
    with pytest.raises(module.HostedCIError, match="sha_mismatch"):
        module.normalize_run(
            _payload(conclusion="success", steps=[], messages=[]),
            expected_sha="b" * 40,
        )
