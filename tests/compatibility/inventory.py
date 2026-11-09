#!/usr/bin/env python3
"""Validate the scoped inventory against the pinned Moby API specification."""
import hashlib
import json
from pathlib import Path
import re

root = Path(__file__).resolve().parents[2]
spec = (root / "tests/compatibility/docker-api-v1.48.yaml").read_bytes()
inventory = json.loads((root / "docs/compatibility/docker-api-v1.48.json").read_text())
assert hashlib.sha256(spec).hexdigest() == inventory["source_sha256"]
expected = []
path = method = None
for line in spec.decode().split("paths:\n", 1)[1].splitlines():
    match = re.match(r"^  (/.*):$", line)
    if match:
        path = match[1]
    match = re.match(r"^    (get|post|put|delete|head):$", line)
    if match:
        method = match[1].upper()
    match = re.match(r'^      operationId: "?([^" ]+)"?$', line)
    if match and path.startswith(("/containers", "/exec", "/networks")):
        expected.append((match[1], method, path))
actual = [(op["operation"], op["method"], op["path"]) for op in inventory["operations"]]
assert actual == expected, "Inventory is incomplete or differs from the pinned schema"
for op in inventory["operations"]:
    assert op["status"] in ("partial", "missing", "verified")
    if op["status"] == "verified":
        assert op.get("evidence"), "Verified operations require conformance evidence"
print(f"{len(actual)} container/exec/network operations inventoried; "
      f"{sum(op['status'] == 'verified' for op in inventory['operations'])} fully verified.")
