"""Record the GitHub Actions result for one exact SupportGuard Candidate SHA."""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess  # nosec B404 - fixed gh executable with sealed arguments
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
GH_BIN = shutil.which("gh") or "/usr/local/bin/gh"
PRIVATE_REPOSITORY = "Teddy-creator/SupportGuard"
PUBLIC_REPOSITORY = "Teddy-creator/SupportGuard-Interview"
PUBLIC_MIRROR_PROVENANCE = ROOT / "public-mirror-provenance.v1.json"
REPOSITORY = PUBLIC_REPOSITORY if PUBLIC_MIRROR_PROVENANCE.is_file() else PRIVATE_REPOSITORY
WORKFLOW = "CI"
BRANCH = "main"
EXPECTED_JOBS = {"backend", "integration", "frontend", "product-e2e", "image"}
EXTERNAL_ZERO_STEP_MARKERS = (
    "recent account payments have failed",
    "spending limit needs to be increased",
    "actions usage is disabled",
    "actions are disabled",
    "runner quota",
)


class HostedCIError(RuntimeError):
    """Raised when hosted evidence cannot be bound to one exact Candidate."""


def _gh_json(*args: str) -> Any:
    completed = subprocess.run(  # noqa: S603  # nosec B603
        [GH_BIN, *args],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )
    if completed.returncode:
        raise HostedCIError(f"github_api_failed:{completed.stderr.strip()[:200]}")
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise HostedCIError("github_api_returned_invalid_json") from exc


def _annotation_messages(job_id: int) -> list[str]:
    payload = _gh_json("api", f"repos/{REPOSITORY}/check-runs/{job_id}/annotations")
    if not isinstance(payload, list):
        raise HostedCIError(f"github_annotations_invalid:{job_id}")
    return [str(item.get("message", "")) for item in payload if item.get("message")]


def _is_external_zero_step(messages: list[str]) -> bool:
    lowered = "\n".join(messages).lower()
    return bool(messages) and any(marker in lowered for marker in EXTERNAL_ZERO_STEP_MARKERS)


def normalize_run(payload: dict[str, Any], *, expected_sha: str) -> dict[str, Any]:
    if payload.get("headSha") != expected_sha:
        raise HostedCIError("hosted_ci_sha_mismatch")
    if payload.get("headBranch") != BRANCH or payload.get("event") != "push":
        raise HostedCIError("hosted_ci_source_mismatch")
    if payload.get("workflowName") != WORKFLOW:
        raise HostedCIError("hosted_ci_workflow_mismatch")
    jobs = payload.get("jobs")
    if not isinstance(jobs, list):
        raise HostedCIError("hosted_ci_jobs_missing")
    names = {str(job.get("name")) for job in jobs}
    if names != EXPECTED_JOBS:
        raise HostedCIError(f"hosted_ci_job_set_mismatch:{sorted(names)}")

    normalized_jobs: list[dict[str, Any]] = []
    all_zero_step = True
    any_external_marker = False
    all_jobs_executed_successfully = True
    for job in sorted(jobs, key=lambda item: str(item.get("name"))):
        steps = job.get("steps")
        if not isinstance(steps, list):
            raise HostedCIError(f"hosted_ci_steps_missing:{job.get('name')}")
        messages = [str(message) for message in job.get("annotation_messages", [])]
        zero_step = len(steps) == 0
        all_zero_step = all_zero_step and zero_step
        any_external_marker = any_external_marker or _is_external_zero_step(messages)
        all_jobs_executed_successfully = all_jobs_executed_successfully and bool(steps)
        all_jobs_executed_successfully = (
            all_jobs_executed_successfully
            and job.get("status") == "completed"
            and job.get("conclusion") == "success"
        )
        normalized_jobs.append(
            {
                "database_id": int(job["databaseId"]),
                "name": str(job["name"]),
                "status": str(job["status"]),
                "conclusion": job.get("conclusion"),
                "step_count": len(steps),
                "steps": [
                    {
                        "name": str(step.get("name")),
                        "status": str(step.get("status")),
                        "conclusion": step.get("conclusion"),
                    }
                    for step in steps
                ],
                "annotation_messages": messages,
            }
        )

    status = str(payload.get("status"))
    conclusion = payload.get("conclusion")
    if status != "completed":
        classification = "in_progress"
    elif conclusion == "success" and all_jobs_executed_successfully:
        classification = "completed_success"
    elif conclusion == "failure" and all_zero_step and any_external_marker:
        classification = "external_zero_step_blocker"
    else:
        classification = "workflow_failure"
    return {
        "contract_version": "supportguard-interview-v2-hosted-ci.v1",
        "recorded_at": datetime.now(UTC).isoformat(),
        "repository": REPOSITORY,
        "workflow": WORKFLOW,
        "candidate_sha": expected_sha,
        "run_id": int(payload["databaseId"]),
        "run_attempt": int(payload.get("attempt", 1)),
        "run_url": str(payload["url"]),
        "status": status,
        "conclusion": conclusion,
        "classification": classification,
        "jobs": normalized_jobs,
        "claims": {
            "hosted_execution_started": not all_zero_step,
            "local_execution_used_as_substitute": False,
            "release_blocker": classification != "completed_success",
        },
    }


def fetch_exact_run(expected_sha: str, *, timeout_seconds: int) -> dict[str, Any]:
    if not re.fullmatch(r"[0-9a-f]{40}", expected_sha):
        raise HostedCIError("candidate_sha_must_be_full_lowercase_hex")
    deadline = time.monotonic() + timeout_seconds
    run_id: int | None = None
    while time.monotonic() <= deadline:
        runs = _gh_json(
            "run",
            "list",
            "--repo",
            REPOSITORY,
            "--workflow",
            WORKFLOW,
            "--branch",
            BRANCH,
            "--event",
            "push",
            "--limit",
            "30",
            "--json",
            "databaseId,headSha,status,conclusion,event",
        )
        matches = [item for item in runs if item.get("headSha") == expected_sha]
        if matches:
            run_id = int(max(matches, key=lambda item: int(item["databaseId"]))["databaseId"])
            break
        time.sleep(5)
    if run_id is None:
        raise HostedCIError("hosted_ci_run_not_found_before_timeout")

    while time.monotonic() <= deadline:
        payload = _gh_json(
            "run",
            "view",
            str(run_id),
            "--repo",
            REPOSITORY,
            "--json",
            "databaseId,attempt,headSha,headBranch,event,status,conclusion,jobs,url,workflowName",
        )
        for job in payload.get("jobs", []):
            job["annotation_messages"] = _annotation_messages(int(job["databaseId"]))
        normalized = normalize_run(payload, expected_sha=expected_sha)
        if normalized["status"] == "completed":
            return normalized
        time.sleep(10)
    raise HostedCIError("hosted_ci_run_did_not_complete_before_timeout")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sha", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--timeout-seconds", type=int, default=1200)
    args = parser.parse_args()
    receipt = fetch_exact_run(args.sha, timeout_seconds=args.timeout_seconds)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(receipt, ensure_ascii=False, sort_keys=True))
    if receipt["classification"] == "external_zero_step_blocker":
        raise SystemExit(2)
    if receipt["classification"] != "completed_success":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
