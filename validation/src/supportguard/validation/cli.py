from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from supportguard.config import get_settings
from supportguard.evals.fault_f06 import execute as execute_f06
from supportguard.evals.fault_f06 import preflight as f06_preflight
from supportguard.evals.gate import enforce_evaluation_route, recompute_evaluation_status
from supportguard.evals.journey_j12 import execute as execute_j12
from supportguard.evals.journey_j12 import preflight as j12_preflight
from supportguard.evals.provider_p16 import add_arguments as add_p16_arguments
from supportguard.evals.provider_p16 import execute as execute_p16
from supportguard.evals.provider_p16 import preflight as p16_preflight
from supportguard.evals.rag_dev30 import run_rag_dev30
from supportguard.observability.logging import configure_json_logging
from supportguard.observability.tracing import configure_tracing


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="supportguard-validation")
    subcommands = parser.add_subparsers(dest="command", required=True)
    eval_parser = subcommands.add_parser("eval", help="Validate frozen Interview inputs")
    eval_commands = eval_parser.add_subparsers(dest="eval_command", required=True)
    eval_commands.add_parser("validate", help="Verify Phase 7 inputs remain frozen and unexecuted")
    for command, help_text in (
        ("rag-dev30", "Run the frozen public RAG Dev30 contract in Phase 7"),
        ("ie-f06", "Run the deterministic six-case fault contract in Phase 7"),
        ("ie-p16", "Run the one-shot real Provider contract in Phase 7"),
        ("ie-j12", "Run the twelve public product journeys in Phase 7"),
    ):
        command_parser = eval_commands.add_parser(command, help=help_text)
        command_parser.add_argument("--candidate-sha")
        command_parser.add_argument("--output", type=Path)
        command_parser.add_argument("--preflight", action="store_true")
        if command == "ie-p16":
            add_p16_arguments(command_parser)
        if command == "ie-j12":
            command_parser.add_argument("--proof-manifest", type=Path)
    return parser


def evaluation_status() -> dict[str, object]:
    return recompute_evaluation_status(Path.cwd())


def main() -> None:
    args = build_parser().parse_args()
    if args.eval_command == "rag-dev30":
        if args.preflight:
            enforce_evaluation_route("validate")
            print(json.dumps(evaluation_status(), sort_keys=True))
            return
        if not args.candidate_sha or args.output is None:
            raise RuntimeError("rag_dev30_candidate_sha_and_output_required")
        receipt = asyncio.run(
            run_rag_dev30(Path.cwd(), candidate_sha=args.candidate_sha, output=args.output)
        )
        print(
            json.dumps(
                {
                    "schema": receipt["schema"],
                    "passed": receipt["claims"]["passed"],
                    "metrics": receipt["metrics"],
                },
                sort_keys=True,
            )
        )
        return
    if args.eval_command == "ie-p16":
        if args.preflight:
            enforce_evaluation_route("validate")
            print(json.dumps(p16_preflight(Path.cwd()), sort_keys=True))
            return
        if not args.candidate_sha or args.output is None or args.candidate_identity_output is None:
            raise RuntimeError("ie_p16_candidate_sha_output_and_identity_required")
        receipt = asyncio.run(
            execute_p16(
                Path.cwd(),
                candidate_sha=args.candidate_sha,
                output=args.output,
                candidate_identity_output=args.candidate_identity_output,
            )
        )
        print(
            json.dumps(
                {
                    "schema": receipt["schema"],
                    "executed": receipt["executed"],
                    "passed": receipt["passed"],
                    "failed": receipt["failed"],
                    "cost": receipt["cost"],
                },
                sort_keys=True,
            )
        )
        return
    if args.eval_command == "ie-f06":
        if args.preflight:
            enforce_evaluation_route("validate")
            print(json.dumps(f06_preflight(Path.cwd()), sort_keys=True))
            return
        if not args.candidate_sha or args.output is None:
            raise RuntimeError("ie_f06_candidate_sha_and_output_required")
        receipt = execute_f06(Path.cwd(), candidate_sha=args.candidate_sha, output=args.output)
        print(
            json.dumps(
                {
                    "schema": receipt["schema"],
                    "passed": receipt["passed"],
                    "failed": receipt["failed"],
                },
                sort_keys=True,
            )
        )
        return
    if args.eval_command == "ie-j12":
        if args.preflight:
            enforce_evaluation_route("validate")
            print(json.dumps(j12_preflight(Path.cwd()), sort_keys=True))
            return
        if not args.candidate_sha or args.output is None or args.proof_manifest is None:
            raise RuntimeError("ie_j12_candidate_sha_output_and_proof_manifest_required")
        receipt = execute_j12(
            Path.cwd(),
            candidate_sha=args.candidate_sha,
            output=args.output,
            proof_manifest=args.proof_manifest,
        )
        print(
            json.dumps(
                {
                    "schema": receipt["schema"],
                    "passed": receipt["passed"],
                    "failed": receipt["failed"],
                },
                sort_keys=True,
            )
        )
        return
    # This gate stays ahead of settings, logging, Provider initialization and
    # every execution artifact read/write.
    enforce_evaluation_route(str(args.eval_command))
    configure_json_logging(service="validation")
    configure_tracing(service="validation", settings=get_settings())
    print(json.dumps(evaluation_status(), sort_keys=True))


if __name__ == "__main__":
    main()
