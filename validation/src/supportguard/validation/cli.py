from __future__ import annotations

import argparse
import json
from pathlib import Path

from supportguard.config import get_settings
from supportguard.evals.gate import enforce_evaluation_route, recompute_evaluation_status
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
        eval_commands.add_parser(command, help=help_text)
    return parser


def evaluation_status() -> dict[str, object]:
    return recompute_evaluation_status(Path.cwd())


def main() -> None:
    args = build_parser().parse_args()
    # This gate stays ahead of settings, logging, Provider initialization and
    # every execution artifact read/write.
    enforce_evaluation_route(str(args.eval_command))
    configure_json_logging(service="validation")
    configure_tracing(service="validation", settings=get_settings())
    print(json.dumps(evaluation_status(), sort_keys=True))


if __name__ == "__main__":
    main()
