from __future__ import annotations

import argparse
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent

FILES: dict[str, str] = {}

# ---------------------------------------------------------------------------
# requirements.txt
# ---------------------------------------------------------------------------
FILES["requirements.txt"] = r'''google-genai>=1.0.0
jsonschema>=4.21.0
'''

# ---------------------------------------------------------------------------
# .gitignore
# ---------------------------------------------------------------------------
FILES[".gitignore"] = r'''# Python
.venv/
__pycache__/
*.py[cod]
.pytest_cache/

# Local secrets
.env
*.key
*.pem

# Gemini response cache
.cache/

# Terraform local state and providers
terraform/.terraform/
*.tfstate
*.tfstate.*
crash.log

# Mock RMP output (regenerated on every deploy)
deployed/*.json
'''

# ---------------------------------------------------------------------------
# README.md
# ---------------------------------------------------------------------------
FILES["README.md"] = r'''# RMP Dashboard Factory

GitOps framework that generates, validates, versions, and deploys Resource
Management Platform (RMP) dashboards.

## Principle

AI is a candidate generator. Git is the source of truth. Deterministic
validation is the gate. Nothing reaches a tenant without passing validation
and being merged to `main`.

## Flow

1. `python scripts/generate.py --request "..." --out dashboards/`
2. `python scripts/validate.py --all`
3. Commit to a feature branch, open a PR (validation only, no deploy).
4. Merge to `main` -> GitHub Actions deploys via Terraform.
5. Rollback = `git revert`.

## Local commands

    py -m venv .venv
    .\.venv\Scripts\Activate.ps1
    pip install -r requirements.txt

    python scripts/generate.py --request "Payments API health dashboard" --out dashboards/
    python scripts/validate.py --all

    cd terraform
    terraform init
    terraform apply -auto-approve
    cd ..

Output lands in `deployed/`.

## Swapping the mock RMP for a real one

`scripts/mock_deploy.py` is the only component that knows how to write to the
platform. Replace it (or replace the `null_resource` in `terraform/main.tf`
with a real provider resource) and the workflow, schema, validator, and Git
history all stay exactly the same.
'''

# ---------------------------------------------------------------------------
# schemas/dashboard.schema.json
# ---------------------------------------------------------------------------
FILES["schemas/dashboard.schema.json"] = r'''{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "https://example.invalid/rmp/dashboard.schema.json",
  "title": "RMP Dashboard Definition",
  "type": "object",
  "required": ["apiVersion", "kind", "metadata", "spec"],
  "additionalProperties": false,
  "properties": {
    "apiVersion": {
      "const": "rmp/v1"
    },
    "kind": {
      "const": "Dashboard"
    },
    "metadata": {
      "type": "object",
      "required": ["name", "title", "owner", "tags"],
      "additionalProperties": false,
      "properties": {
        "name": {
          "type": "string",
          "pattern": "^[a-z][a-z0-9-]{2,39}$"
        },
        "title": {
          "type": "string",
          "minLength": 3,
          "maxLength": 80
        },
        "description": {
          "type": "string",
          "maxLength": 300
        },
        "owner": {
          "type": "string",
          "pattern": "^[a-z0-9][a-z0-9-]{1,29}$"
        },
        "tags": {
          "type": "array",
          "minItems": 1,
          "maxItems": 10,
          "items": {
            "type": "string",
            "pattern": "^[a-z0-9-]+:[a-z0-9._-]+$"
          }
        }
      }
    },
    "spec": {
      "type": "object",
      "required": ["refreshIntervalSeconds", "timeRange", "panels"],
      "additionalProperties": false,
      "properties": {
        "refreshIntervalSeconds": {
          "type": "integer",
          "minimum": 30,
          "maximum": 3600
        },
        "timeRange": {
          "type": "string",
          "enum": ["15m", "1h", "6h", "24h", "7d", "30d"]
        },
        "panels": {
          "type": "array",
          "minItems": 1,
          "maxItems": 12,
          "items": {
            "type": "object",
            "required": ["id", "title", "type", "metric"],
            "additionalProperties": false,
            "properties": {
              "id": {
                "type": "string",
                "pattern": "^[a-z][a-z0-9_]{1,29}$"
              },
              "title": {
                "type": "string",
                "minLength": 3,
                "maxLength": 80
              },
              "type": {
                "type": "string",
                "enum": ["timeseries", "stat", "table", "bar", "gauge", "heatmap"]
              },
              "metric": {
                "type": "string",
                "pattern": "^[a-z][a-z0-9_.]{2,79}$"
              },
              "unit": {
                "type": "string",
                "enum": ["percent", "ms", "bytes", "count", "ratio"]
              },
              "thresholds": {
                "type": "array",
                "maxItems": 5,
                "items": {
                  "type": "object",
                  "required": ["value", "severity"],
                  "additionalProperties": false,
                  "properties": {
                    "value": {
                      "type": "number"
                    },
                    "severity": {
                      "type": "string",
                      "enum": ["info", "warning", "critical"]
                    }
                  }
                }
              }
            }
          }
        }
      }
    }
  }
}
'''

# ---------------------------------------------------------------------------
# requests/payments-api.txt
# ---------------------------------------------------------------------------
FILES["requests/payments-api.txt"] = r'''Create a dashboard for the payments API.

Audience: on-call SRE.
Must show: request rate, p99 latency, error rate, saturation of the connection pool.
Time range: 6h. Refresh every 60 seconds.
Owner team: payments. Environment: production.
'''

# ---------------------------------------------------------------------------
# scripts/generate.py
# ---------------------------------------------------------------------------
FILES["scripts/generate.py"] = r'''#!/usr/bin/env python
"""Generate an RMP dashboard definition with Google Gemini.

AI is a CANDIDATE GENERATOR ONLY.

This script never deploys, never commits, and never touches a live tenant.
Its output must pass scripts/validate.py before it can be merged.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import random
import re
import sys
import time

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
SCHEMA_PATH = REPO_ROOT / "schemas" / "dashboard.schema.json"
DASHBOARDS_DIR = REPO_ROOT / "dashboards"
CACHE_DIR = REPO_ROOT / ".cache"

MAX_ATTEMPTS_PER_MODEL = 4
MAX_MODELS = 3

RETRYABLE_MARKERS = (
    "429", "500", "502", "503", "504",
    "resource_exhausted", "unavailable", "deadline",
    "overloaded", "rate limit", "internal error",
)

SCHEMA_DROP_KEYS = frozenset({
    "$schema", "$id", "$defs", "definitions", "additionalProperties",
    "pattern", "minLength", "maxLength", "minItems", "maxItems",
    "minimum", "maximum", "title", "description", "default", "examples",
})

PROMPT_TEMPLATE = """You are generating ONE RMP (Resource Management Platform) dashboard definition.

Return ONLY a JSON object that validates against this JSON Schema:
{schema}

Hard rules:
- apiVersion must be exactly "rmp/v1".
- kind must be exactly "Dashboard".
- metadata.name must be lowercase kebab-case, 3-40 characters, starting with a letter.
- metadata.tags must contain at least one "env:<value>", one "team:<value>",
  and one "owner:<value>" tag.
- spec.timeRange must be one of 15m, 1h, 6h, 24h, 7d, 30d.
- spec.refreshIntervalSeconds must be one of 30, 60, 300, 900, 1800, 3600.
- spec.panels must contain between 3 and 12 panels.
- Every panel id must be unique, lowercase snake_case.
- Every panel metric must start with the prefix "rmp.".
- Every panel must declare a "unit".
- Never include passwords, secrets, tokens, API keys, or connection strings.
- Never invent real hostnames, IP addresses, customer names, or personal data.
  Use generic placeholders only.

User request:
{request}
"""


def log(message: str) -> None:
    print(message, flush=True)


def import_sdk():
    """Import google-genai with an actionable error if it is missing."""
    try:
        from google import genai
        from google.genai import types
    except ImportError:
        log("[fatal] google-genai is not installed.")
        log("        fix: pip install -r requirements.txt")
        sys.exit(2)
    return genai, types


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


def sanitize_schema(node):
    """Convert JSON Schema into the subset Gemini's response_schema accepts."""
    if isinstance(node, dict):
        out = {}
        for key, value in node.items():
            if key in SCHEMA_DROP_KEYS:
                continue
            if key == "const":
                out["enum"] = [value]
                continue
            out[key] = sanitize_schema(value)
        return out
    if isinstance(node, list):
        return [sanitize_schema(item) for item in node]
    return node


def rank_model(name: str):
    """Lower score = more preferred. Non-chat models score >= 1000."""
    lowered = name.lower()
    score = 0
    for bad in ("embedding", "aqa", "image", "tts", "veo", "imagen", "learnlm"):
        if bad in lowered:
            score += 1000
    if "flash" in lowered:
        score -= 100
    if "lite" in lowered:
        score += 25
    if "thinking" in lowered:
        score += 15
    if "pro" in lowered:
        score += 50
    if "preview" in lowered or "exp" in lowered:
        score += 5
    return (score, lowered)


def discover_models(client) -> list:
    """Discover generateContent-capable models from the account at runtime."""
    names = set()
    try:
        for model in client.models.list():
            raw = getattr(model, "name", "") or ""
            name = raw.split("/")[-1].strip()
            if not name:
                continue
            actions = getattr(model, "supported_actions", None) or []
            if actions and "generateContent" not in actions:
                continue
            names.add(name)
    except Exception as exc:
        log(f"[models] discovery failed: {type(exc).__name__}: {exc}")

    ordered = sorted(names, key=rank_model)
    ordered = [n for n in ordered if rank_model(n)[0] < 1000]

    if not ordered:
        log("[models] discovery returned nothing usable; falling back to moving aliases")
        ordered = ["gemini-flash-latest", "gemini-pro-latest"]

    return ordered[:MAX_MODELS]


def cache_path(request_text: str, schema_text: str) -> pathlib.Path:
    digest = hashlib.sha256()
    digest.update(request_text.strip().encode("utf-8"))
    digest.update(b"--schema--")
    digest.update(schema_text.encode("utf-8"))
    return CACHE_DIR / (digest.hexdigest()[:32] + ".json")


def call_model(types_mod, client, model: str, prompt: str, gemini_schema, use_schema: bool) -> str:
    config_kwargs = {
        "response_mime_type": "application/json",
        "temperature": 0.2,
    }
    if use_schema and gemini_schema is not None:
        config_kwargs["response_schema"] = gemini_schema
    config = types_mod.GenerateContentConfig(**config_kwargs)
    response = client.models.generate_content(model=model, contents=prompt, config=config)
    text = getattr(response, "text", None)
    if not text:
        raise RuntimeError("model returned an empty response")
    return text


def call_with_backoff(fn, label: str) -> str:
    """Exponential backoff with jitter on rate limits and transient 5xx."""
    delay = 2.0
    last_error = None
    for attempt in range(1, MAX_ATTEMPTS_PER_MODEL + 1):
        try:
            return fn()
        except Exception as exc:
            last_error = exc
            message = f"{type(exc).__name__}: {exc}".lower()
            retryable = any(marker in message for marker in RETRYABLE_MARKERS)
            if not retryable or attempt == MAX_ATTEMPTS_PER_MODEL:
                raise
            wait = delay + random.uniform(0.0, 0.75)
            log(f"[retry] {label}: attempt {attempt}/{MAX_ATTEMPTS_PER_MODEL} failed; "
                f"retrying in {wait:.1f}s ({type(exc).__name__})")
            time.sleep(wait)
            delay = min(delay * 2.0, 30.0)
    raise last_error


def extract_json(text: str) -> dict:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```[A-Za-z0-9_-]*\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise ValueError("model response contained no JSON object")
    return json.loads(cleaned[start:end + 1])


def emit(document: dict, args, model: str, from_cache: bool) -> int:
    text = json.dumps(document, indent=2) + "\n"
    if args.print_only:
        log(text)
        return 0

    name = document.get("metadata", {}).get("name", "unnamed-dashboard")
    out_dir = pathlib.Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / (name + ".json")
    target.write_text(text, encoding="utf-8", newline="\n")

    origin = "cache-hit" if from_cache else "cache-miss"
    log(f"[write] {target} (model={model}, {origin})")
    log(f"[next]  python scripts/validate.py {target}")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Generate an RMP dashboard candidate with Google Gemini.")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--request", "-r", help="Natural-language dashboard request.")
    source.add_argument("--request-file", help="Path to a text file containing the request.")
    parser.add_argument("--out", default=str(DASHBOARDS_DIR),
                        help="Output directory. Default: dashboards/")
    parser.add_argument("--no-cache", action="store_true",
                        help="Bypass the response cache.")
    parser.add_argument("--print-only", action="store_true",
                        help="Print the JSON without writing a file.")
    args = parser.parse_args(argv)

    request_text = args.request
    if args.request_file:
        try:
            request_text = pathlib.Path(args.request_file).read_text(encoding="utf-8")
        except OSError as exc:
            log(f"[fatal] cannot read --request-file: {exc}")
            return 2

    schema = load_schema()
    schema_text = json.dumps(schema, sort_keys=True)
    gemini_schema = sanitize_schema(schema)

    cache_file = cache_path(request_text, schema_text)
    if cache_file.exists() and not args.no_cache:
        try:
            cached = json.loads(cache_file.read_text(encoding="utf-8"))
            log(f"[cache] hit {cache_file.name} (model={cached.get('model')})")
            return emit(cached["document"], args, cached.get("model", "cached"), True)
        except Exception as exc:
            log(f"[cache] unreadable, ignoring: {exc}")

    genai, types_mod = import_sdk()

    api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if not api_key:
        log("[fatal] GEMINI_API_KEY is not set in this shell session.")
        log("        current session: $env:GEMINI_API_KEY = \"AIza...\"")
        log("        permanent:       [Environment]::SetEnvironmentVariable(\"GEMINI_API_KEY\",\"AIza...\",\"User\")")
        return 2
    if not api_key.startswith("AIza"):
        log("[warn] GEMINI_API_KEY does not start with 'AIza'. Continuing anyway.")

    try:
        client = genai.Client(api_key=api_key)
    except Exception as exc:
        log(f"[fatal] could not create Gemini client: {type(exc).__name__}: {exc}")
        return 2

    candidates = discover_models(client)
    log(f"[models] candidates: {', '.join(candidates)}")

    prompt = PROMPT_TEMPLATE.format(
        schema=json.dumps(schema, indent=2),
        request=request_text.strip(),
    )

    document = None
    used_model = None
    last_error = None

    for model in candidates:
        for use_schema in (True, False):
            label = f"{model} response_schema={'on' if use_schema else 'off'}"
            log(f"[model] using {label}")
            try:
                text = call_with_backoff(
                    lambda m=model, u=use_schema: call_model(
                        types_mod, client, m, prompt, gemini_schema, u),
                    label,
                )
                document = extract_json(text)
                used_model = model
                break
            except Exception as exc:
                last_error = exc
                log(f"[model] {label} failed: {type(exc).__name__}: {exc}")
        if document is not None:
            break

    if document is None:
        log(f"[fatal] every candidate model failed. last error: {last_error}")
        log("        fix: check quota at https://aistudio.google.com/app/apikey")
        return 1

    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(
            json.dumps({"model": used_model, "document": document}, indent=2),
            encoding="utf-8", newline="\n")
    except OSError as exc:
        log(f"[warn] could not write cache: {exc}")

    return emit(document, args, used_model, False)


if __name__ == "__main__":
    sys.exit(main())
'''

# ---------------------------------------------------------------------------
# scripts/validate.py
# ---------------------------------------------------------------------------
FILES["scripts/validate.py"] = r'''#!/usr/bin/env python
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
'''

# ---------------------------------------------------------------------------
# scripts/mock_deploy.py
# ---------------------------------------------------------------------------
FILES["scripts/mock_deploy.py"] = r'''#!/usr/bin/env python
"""Mock RMP deployer.

Simulates the Resource Management Platform write API by copying validated
dashboard definitions into <repo-root>/deployed/ and emitting a manifest.

The repository root is resolved from __file__, never from the current working
directory, so Terraform can invoke this from any location.

Swapping to a real RMP: replace this module with a Terraform provider resource
(or a thin authenticated HTTP client) and keep the same inputs and outputs.
The workflow, schema, validator, and Git history do not change.

Exit codes: 0 = success, 1 = deployment failure, 2 = usage error.
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import pathlib
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
DASHBOARDS_DIR = REPO_ROOT / "dashboards"
DEPLOYED_DIR = REPO_ROOT / "deployed"
MANIFEST_NAME = "manifest.json"


def log(message: str) -> None:
    print(message, flush=True)


def sha256_of(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    digest.update(path.read_bytes())
    return digest.hexdigest()


def deploy_one(source: pathlib.Path) -> dict:
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"cannot read {source}: {exc}") from exc

    name = payload.get("metadata", {}).get("name")
    if not name:
        raise RuntimeError(f"{source.name}: missing metadata.name")

    DEPLOYED_DIR.mkdir(parents=True, exist_ok=True)
    target = DEPLOYED_DIR / (name + ".json")
    target.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8", newline="\n")

    log(f"[deploy] {source.name} -> deployed/{target.name}")
    return {
        "name": name,
        "source": f"dashboards/{source.name}",
        "target": f"deployed/{target.name}",
        "sha256": sha256_of(target),
        "bytes": target.stat().st_size,
    }


def prune_orphans(keep_names) -> None:
    """Remove deployed artifacts whose source dashboard no longer exists.

    This is what makes `git revert` a true rollback for the mock provider.
    """
    if not DEPLOYED_DIR.exists():
        return
    for candidate in DEPLOYED_DIR.glob("*.json"):
        if candidate.name == MANIFEST_NAME:
            continue
        if candidate.stem not in keep_names:
            candidate.unlink()
            log(f"[prune] removed orphan deployed/{candidate.name}")


def write_manifest(records, environment: str) -> pathlib.Path:
    manifest = {
        "generatedAt": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "target": "mock-rmp",
        "environment": environment,
        "dashboardCount": len(records),
        "dashboards": sorted(records, key=lambda r: r["name"]),
    }
    DEPLOYED_DIR.mkdir(parents=True, exist_ok=True)
    path = DEPLOYED_DIR / MANIFEST_NAME
    path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8", newline="\n")
    log(f"[manifest] wrote deployed/{MANIFEST_NAME} ({len(records)} dashboard(s))")
    return path


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Mock RMP deployer.")
    parser.add_argument("paths", nargs="*", help="Dashboard JSON files to deploy.")
    parser.add_argument("--all", action="store_true", help="Deploy every dashboards/*.json.")
    parser.add_argument("--environment", default="local", help="Environment label for the manifest.")
    args = parser.parse_args(argv)

    sources = []
    if args.all:
        sources.extend(sorted(DASHBOARDS_DIR.glob("*.json")))
    sources.extend(pathlib.Path(p) for p in args.paths)

    if not sources:
        log("[fatal] no dashboards found to deploy.")
        log(f"        looked in: {DASHBOARDS_DIR}")
        log("        fix: python scripts/generate.py --request \"...\" --out dashboards/")
        return 1

    records = []
    for source in sources:
        if not source.exists():
            log(f"[fatal] {source} does not exist")
            return 1
        try:
            records.append(deploy_one(source))
        except RuntimeError as exc:
            log(f"[fatal] {exc}")
            return 1

    prune_orphans({record["name"] for record in records})
    write_manifest(records, args.environment)

    log(f"[ok] deployed {len(records)} dashboard(s) to {DEPLOYED_DIR}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
'''

# ---------------------------------------------------------------------------
# terraform/versions.tf
# ---------------------------------------------------------------------------
FILES["terraform/versions.tf"] = r'''terraform {
  required_version = ">= 1.5.0"

  required_providers {
    null = {
      source  = "hashicorp/null"
      version = "~> 3.2"
    }
  }
}
'''

# ---------------------------------------------------------------------------
# terraform/variables.tf
# ---------------------------------------------------------------------------
FILES["terraform/variables.tf"] = r'''variable "python_executable" {
  description = "Interpreter used by the mock RMP deployer. Use 'py' on Windows if 'python' is not on PATH."
  type        = string
  default     = "python"
}

variable "environment" {
  description = "Logical environment recorded in the deployment manifest."
  type        = string
  default     = "local"
}
'''

# ---------------------------------------------------------------------------
# terraform/main.tf
# ---------------------------------------------------------------------------
FILES["terraform/main.tf"] = r'''locals {
  repo_root       = abspath("${path.root}/..")
  dashboards_dir  = "${local.repo_root}/dashboards"
  dashboard_files = fileset(local.dashboards_dir, "*.json")

  dashboard_hashes = [
    for file_name in local.dashboard_files :
    filesha256("${local.dashboards_dir}/${file_name}")
  ]
}

resource "null_resource" "mock_rmp_deploy" {
  triggers = {
    dashboards_hash = sha256(join(",", local.dashboard_hashes))
    environment     = var.environment
  }

  provisioner "local-exec" {
    working_dir = local.repo_root
    command     = "${var.python_executable} scripts/mock_deploy.py --all --environment ${var.environment}"
  }
}
'''

# ---------------------------------------------------------------------------
# terraform/outputs.tf
# ---------------------------------------------------------------------------
FILES["terraform/outputs.tf"] = r'''output "repo_root" {
  description = "Resolved repository root used by the mock deployer."
  value       = local.repo_root
}

output "dashboard_count" {
  description = "Number of dashboard definitions discovered."
  value       = length(local.dashboard_files)
}

output "dashboard_names" {
  description = "Dashboard definition file names discovered."
  value       = sort(tolist(local.dashboard_files))
}

output "deployed_files" {
  description = "Absolute paths of the mock RMP deployment artifacts."
  value = [
    for file_name in local.dashboard_files :
    "${local.repo_root}/deployed/${file_name}"
  ]
}
'''

# ---------------------------------------------------------------------------
# .github/workflows/pipeline.yml
# ---------------------------------------------------------------------------
FILES[".github/workflows/pipeline.yml"] = r'''name: rmp-dashboard-pipeline

on:
  pull_request:
    branches: [main]
  push:
    branches: [main]
  workflow_dispatch:
    inputs:
      request:
        description: "Natural-language dashboard request (manual runs only)"
        required: false
        default: "Create a dashboard for the payments API: request rate, p99 latency, error rate, and connection pool saturation."

permissions:
  contents: read

env:
  PYTHON_VERSION: "3.11"
  TERRAFORM_VERSION: "1.9.8"

jobs:
  validate:
    name: Validate dashboards (deterministic gate)
    runs-on: ubuntu-latest
    steps:
      - name: Checkout
        uses: actions/checkout@v4

      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: ${{ env.PYTHON_VERSION }}
          cache: pip

      - name: Install dependencies
        run: pip install -r requirements.txt

      - name: Validate every dashboard definition
        run: python scripts/validate.py --all

      - name: Upload validated definitions
        uses: actions/upload-artifact@v4
        with:
          name: validated-dashboards
          path: dashboards/*.json
          if-no-files-found: error
          retention-days: 14

  deploy:
    name: Deploy to mock RMP (main only)
    needs: validate
    if: github.event_name == 'push' && github.ref == 'refs/heads/main'
    runs-on: ubuntu-latest
    steps:
      - name: Checkout
        uses: actions/checkout@v4

      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: ${{ env.PYTHON_VERSION }}
          cache: pip

      - name: Install dependencies
        run: pip install -r requirements.txt

      - name: Set up Terraform
        uses: hashicorp/setup-terraform@v3
        with:
          terraform_version: ${{ env.TERRAFORM_VERSION }}

      - name: Terraform init
        working-directory: terraform
        run: terraform init -input=false

      - name: Terraform apply
        working-directory: terraform
        run: terraform apply -auto-approve -input=false

      - name: Verify deployment output exists
        run: |
          test -d deployed
          test -f deployed/manifest.json

      - name: Upload deployed artifacts
        uses: actions/upload-artifact@v4
        with:
          name: deployed-dashboards
          path: |
            deployed/*.json
          if-no-files-found: error
          retention-days: 14

  generate:
    name: Generate candidate dashboard (manual)
    if: github.event_name == 'workflow_dispatch'
    runs-on: ubuntu-latest
    steps:
      - name: Checkout
        uses: actions/checkout@v4

      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: ${{ env.PYTHON_VERSION }}
          cache: pip

      - name: Install dependencies
        run: pip install -r requirements.txt

      - name: Generate candidate with Gemini
        env:
          GEMINI_API_KEY: ${{ secrets.GEMINI_API_KEY }}
        run: python scripts/generate.py --request "${{ github.event.inputs.request }}" --out dashboards/

      - name: Validate the candidate
        run: python scripts/validate.py --all

      - name: Upload candidate for human review
        uses: actions/upload-artifact@v4
        with:
          name: candidate-dashboard
          path: dashboards/*.json
          if-no-files-found: error
          retention-days: 14
'''

# ---------------------------------------------------------------------------
# Placeholder keep files
# ---------------------------------------------------------------------------
FILES["dashboards/.gitkeep"] = ""
FILES["deployed/.gitkeep"] = ""


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Create the RMP Dashboard Factory project files.")
    parser.add_argument("--force", action="store_true",
                        help="Overwrite files that already exist.")
    args = parser.parse_args(argv)

    written = 0
    skipped = 0

    for relative in sorted(FILES):
        content = FILES[relative]
        target = ROOT / relative
        target.parent.mkdir(parents=True, exist_ok=True)

        if target.exists() and not args.force:
            print(f"[skip]  {relative}")
            skipped += 1
            continue

        target.write_text(content, encoding="utf-8", newline="\n")
        print(f"[write] {relative}")
        written += 1

    print("")
    print(f"done: {written} written, {skipped} skipped")
    print("next:  py -m venv .venv")
    return 0


if __name__ == "__main__":
    sys.exit(main())