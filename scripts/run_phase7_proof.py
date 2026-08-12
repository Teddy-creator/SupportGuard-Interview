#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import cast

from supportguard.evals.deterministic_proof import PROOF_KINDS, ProofKind, execute

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    parser = argparse.ArgumentParser(description="Run one Candidate-bound Phase 7 proof")
    parser.add_argument("kind", choices=PROOF_KINDS)
    parser.add_argument("--candidate-sha", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    receipt = execute(
        ROOT,
        candidate_sha=args.candidate_sha,
        output=args.output,
        kind=cast(ProofKind, args.kind),
    )
    print(
        json.dumps(
            {
                "schema": receipt["schema"],
                "classification": receipt["classification"],
                "denominator": receipt["denominator"],
                "passed": receipt["passed"],
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
