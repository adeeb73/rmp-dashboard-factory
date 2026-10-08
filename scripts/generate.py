#!/usr/bin/env python
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
