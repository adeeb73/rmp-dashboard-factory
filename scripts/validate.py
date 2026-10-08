#!/usr/bin/env python
"""Deterministic three-layer validator for RMP dashboard definitions.

Layer 1 - JSON Schema conformance (jsonschema).
Layer 2 - Platform standards (naming, tags, panel hygiene, refresh policy).
Layer 3 - Security scan (forbidden substrings).

No LLM is involved. This is the only gate that can approve a change.

Exit codes: 0 = pass, 1 = validation failure, 2 = usage or IO error.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
SCHEMA_PATH = REPO_ROOT / "schemas" / "dashboard.schema.json"
DASHBOARDS_DIR = REPO_ROOT / "dashboards"

NAME_PATTERN = re.compile(r"^[a-z][a-z0-9-]{2,39}$")
METRIC_PREFIX = "rmp."

REQUIRED_TAG_PREFIXES = ("env", "team", "owner")
ALLOWED_REFRESH = {30, 60, 300, 900, 1800, 3600}
ALLOWED_PANEL_TYPES = {"timeseries", "stat", "table", "bar", "gauge", "heatmap"}
ALLOWED_UNITS = {"percent", "ms", "bytes", "count", "ratio"}
MIN_PANELS = 3
MAX_PANELS = 12

FORBIDDEN_SUBSTRINGS = (
    "password", "passwd", "pwd", "secret", "token", "api_key", "apikey",
    "private_key", "privatekey", "bearer ", "aws_access_key", "aws_secret",
    "client_secret", "connection_string", "connectionstring", "-----begin",
    "ssh-rsa", "authorization:",
)


def log(message: str) -> None:
    print(message, flush=True)


def load_schema() -> dict:
    try:
        with SCHEMA_PATH.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError:
        log(f"[fatal] schema not found: {SCHEMA_PATH}")
        log("        fix: py bootstrap.py --force")
        sys.exit(2)
    except json.JSONDecodeError as exc:
        log(f"[fatal] schema is not valid JSON: {exc}")
        sys.exit(2)


def import_jsonschema():
    try:
        from jsonschema import Draft202012Validator
    except ImportError:
        log("[fatal] jsonschema is not installed.")
        log("        fix: pip install -r requirements.txt")
        sys.exit(2)
    return Draft202012Validator


def layer1_schema(document, schema, validator_cls):
    errors = []
    validator = validator_cls(schema)
    for error in sorted(validator.iter_errors(document), key=lambda e: list(e.path)):
        location = "/".join(str(part) for part in error.path) or "<root>"
        errors.append(f"[schema] {location}: {error.message}")
    return errors


def layer2_standards(document):
    errors = []
    metadata = document["metadata"]
    spec = document["spec"]

    name = metadata["name"]
    if not NAME_PATTERN.match(name):
        errors.append(
            f"[standards] metadata.name '{name}' must be lowercase kebab-case, 3-40 chars, starting with a letter")

    tag_prefixes = {tag.split(":", 1)[0] for tag in metadata.get("tags", []) if ":" in tag}
    for required in REQUIRED_TAG_PREFIXES:
        if required not in tag_prefixes:
            errors.append(
                f"[standards] metadata.tags must include a '{required}:<value>' tag")

    refresh = spec["refreshIntervalSeconds"]
    if refresh not in ALLOWED_REFRESH:
        errors.append(
            f"[standards] spec.refreshIntervalSeconds={refresh} is not allowed; "
            f"use one of {sorted(ALLOWED_REFRESH)}")

    panels = spec["panels"]
    if not (MIN_PANELS <= len(panels) <= MAX_PANELS):
        errors.append(
            f"[standards] spec.panels must contain {MIN_PANELS}-{MAX_PANELS} panels, found {len(panels)}")

    seen_ids = set()
    for panel in panels:
        panel_id = panel["id"]
        if panel_id in seen_ids:
            errors.append(f"[standards] duplicate panel id '{panel_id}'")
        seen_ids.add(panel_id)

        if panel["type"] not in ALLOWED_PANEL_TYPES:
            errors.append(
                f"[standards] panel '{panel_id}': type '{panel['type']}' is not allowed")

        unit = panel.get("unit")
        if unit is None:
            errors.append(f"[standards] panel '{panel_id}': unit is required by platform standards")
        elif unit not in ALLOWED_UNITS:
            errors.append(f"[standards] panel '{panel_id}': unit '{unit}' is not allowed")

        metric = panel["metric"]
        if not metric.startswith(METRIC_PREFIX):
            errors.append(
                f"[standards] panel '{panel_id}': metric '{metric}' must start with '{METRIC_PREFIX}'")

    return errors


def layer3_security(raw_text: str):
    errors = []
    lowered = raw_text.lower()
    for needle in FORBIDDEN_SUBSTRINGS:
        if needle in lowered:
            errors.append(f"[security] forbidden substring '{needle}' found in document")
    return errors


def validate_file(path: pathlib.Path, schema: dict, validator_cls) -> list:
    try:
        raw_text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return [f"[io] cannot read {path}: {exc}"]

    try:
        document = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        return [f"[json] {path.name}: invalid JSON at line {exc.lineno} column {exc.colno}: {exc.msg}"]

    errors = layer1_schema(document, schema, validator_cls)
    security_errors = layer3_security(raw_text)

    if errors:
        return errors + security_errors

    if not isinstance(document, dict):
        return ["[schema] top level document must be a JSON object"]

    try:
        errors.extend(layer2_standards(document))
    except (KeyError, TypeError) as exc:
        errors.append(f"[standards] could not evaluate standards: {type(exc).__name__}: {exc}")

    errors.extend(security_errors)
    return errors


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Validate RMP dashboard definitions.")
    parser.add_argument("paths", nargs="*", help="Dashboard JSON files to validate.")
    parser.add_argument("--all", action="store_true",
                        help="Validate every dashboards/*.json file.")
    args = parser.parse_args(argv)

    targets = []
    if args.all:
        targets.extend(sorted(DASHBOARDS_DIR.glob("*.json")))
    targets.extend(pathlib.Path(p) for p in args.paths)

    if not targets:
        log("[fatal] nothing to validate. Pass a file path or --all.")
        log("        fix: python scripts/generate.py --request \"...\" --out dashboards/")
        return 2

    schema = load_schema()
    validator_cls = import_jsonschema()

    failed = 0
    for target in targets:
        if not target.exists():
            log(f"FAIL  {target}  (file not found)")
            failed += 1
            continue

        errors = validate_file(target, schema, validator_cls)
        if errors:
            failed += 1
            log(f"FAIL  {target}")
            for error in errors:
                log(f"      {error}")
        else:
            log(f"PASS  {target}")

    log("")
    log(f"validated {len(targets)} file(s); {len(targets) - failed} passed, {failed} failed")

    if failed:
        log("[gate] BLOCKED. Fix the errors above, regenerate, and re-run.")
        return 1

    log("[gate] APPROVED. Safe to commit.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
