#!/bin/sh
set -eu

supportguard db baseline-upgrade
supportguard db seed
supportguard db configure-mcp-roles
supportguard knowledge ingest
exec uvicorn supportguard.main:app --host 0.0.0.0 --port 8000
