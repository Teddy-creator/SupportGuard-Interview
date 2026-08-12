#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest


class ManifestCollector:
    def __init__(self, target: Path) -> None:
        self.target = target

    def pytest_collection_finish(self, session: pytest.Session) -> None:
        manifest = {"hermetic": [], "postgres": []}
        for item in session.items:
            if item.get_closest_marker("mcp") is None:
                continue
            partition = (
                "postgres" if item.get_closest_marker("postgres") is not None else "hermetic"
            )
            manifest[partition].append(item.nodeid)
        self.target.write_text(json.dumps(manifest, sort_keys=True) + "\n")


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: collect_mcp_test_manifest.py OUTPUT")
    output = Path(sys.argv[1])
    result = pytest.main(["--collect-only", "-q"], plugins=[ManifestCollector(output)])
    if result != pytest.ExitCode.OK or not output.is_file():
        raise SystemExit(int(result))


if __name__ == "__main__":
    main()
