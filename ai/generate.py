import os
import json
import time
import yaml
import hashlib
import pathlib
import argparse
from dotenv import load_dotenv
from google import genai
from google.genai import types
from google.genai import errors as genai_errors

load_dotenv()

API_KEY = os.environ.get("GEMINI_API_KEY")
if not API_KEY:
    raise SystemExit("GEMINI_API_KEY is not set")

client = genai.Client(api_key=API_KEY)

CACHE_DIR = pathlib.Path(".cache")
CACHE_DIR.mkdir(exist_ok=True)
ROOT = pathlib.Path(__file__).resolve().parent.parent

# Retry policy
MAX_ATTEMPTS = 4
INITIAL_BACKOFF = 5          # seconds
BACKOFF_MULTIPLIER = 2


def list_models():
    try:
        return [m.name.split("/")[-1] for m in client.models.list()]
    except Exception as e:
        print(f"[warn] could not list models: {e}")
        return []


def candidate_models():
    """Ordered list of flash models to try, best first."""
    available = set(list_models())
    preferred = os.environ.get("GEMINI_MODEL")

    candidates = []
    if preferred:
        candidates.append(preferred)

    for name in [
        "gemini-flash-latest",
        "gemini-2.5-flash",
        "gemini-2.0-flash",
        "gemini-2.0-flash-001",
        "gemini-1.5-flash",
    ]:
        if name not in candidates:
            candidates.append(name)

    # Filter to models the account can actually use; keep preferred even
    # if we could not list (list may have failed).
    if available:
        filtered = [c for c in candidates if c in available]
        if preferred and preferred in available and preferred not in filtered:
            filtered.insert(0, preferred)
        if filtered:
            return filtered

    return candidates or ["gemini-2.0-flash"]


def load_json(path):
    with open(path) as f:
        return json.load(f)


def load_text(path):
    with open(path) as f:
        return f.read()


def cache_key(payload):
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def is_retryable(err):
    """503 UNAVAILABLE, 429 RESOURCE_EXHAUSTED, 500 INTERNAL."""
    code = getattr(err, "code", None)
    if code in (429, 500, 503):
        return True
    msg = str(err)
    return any(t in msg for t in ("503", "429", "UNAVAILABLE", "RESOURCE_EXHAUSTED", "INTERNAL"))


def call_gemini(model, prompt, schema):
    """One attempt against one model. Raises on failure."""
    config_kwargs = {
        "response_mime_type": "application/json",
        "temperature": 0.2,
    }
    try:
        config_kwargs["response_schema"] = schema
        return client.models.generate_content(
            model=model,
            contents=prompt,
            config=types.GenerateContentConfig(**config_kwargs),
        )
    except genai_errors.ClientError as e:
        # Some newer models reject response_schema. Retry without it.
        if "response_schema" in str(e) or "INVALID_ARGUMENT" in str(e):
            print(f"[warn] {model}: response_schema rejected, retrying without schema.")
            config_kwargs.pop("response_schema", None)
            return client.models.generate_content(
                model=model,
                contents=prompt,
                config=types.GenerateContentConfig(**config_kwargs),
            )
        raise


def generate_dashboard(app, purpose, metrics, env):
    schema = load_json(ROOT / "schemas/dashboard.schema.json")
    standards = yaml.safe_load(load_text(ROOT / "standards/naming.yaml"))
    template = load_text(ROOT / "templates/api-availability.yaml")
    prompt_tpl = load_text(ROOT / "ai/prompts/generate_dashboard.txt")

    prompt = prompt_tpl.format(
        app=app,
        purpose=purpose,
        metrics=", ".join(metrics),
        env=env,
        standards=yaml.dump(standards),
        template=template,
        min_panels=standards.get("min_panels", 1),
        max_panels=standards.get("max_panels", 12),
    )

    key = cache_key({"app": app, "purpose": purpose, "metrics": metrics, "env": env})
    cached = CACHE_DIR / f"{key}.json"
    if cached.exists():
        print(f"[cache hit] {cached}")
        return json.loads(cached.read_text())

    models = candidate_models()
    print(f"[models] trying in order: {', '.join(models)}")

    last_error = None

    for model in models:
        backoff = INITIAL_BACKOFF
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                print(f"[gemini] model={model} attempt={attempt}/{MAX_ATTEMPTS}")
                response = call_gemini(model, prompt, schema)
                result = json.loads(response.text)
                cached.write_text(json.dumps(result, indent=2))
                print(f"[ok] model={model} succeeded on attempt {attempt}")
                return result
            except Exception as e:
                last_error = e
                if is_retryable(e):
                    print(f"[retry] {model}: {type(e).__name__}: {str(e)[:120]}")
                    if attempt < MAX_ATTEMPTS:
                        print(f"[wait] sleeping {backoff}s before retry")
                        time.sleep(backoff)
                        backoff *= BACKOFF_MULTIPLIER
                    else:
                        print(f"[give up] {model} exhausted retries, moving to next model")
                else:
                    print(f"[fatal] {model}: {type(e).__name__}: {str(e)[:200]}")
                    raise

    raise SystemExit(f"All models failed. Last error: {last_error}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--app", required=True)
    parser.add_argument("--purpose", required=True)
    parser.add_argument("--metrics", required=True, help="Comma-separated")
    parser.add_argument("--env", default="demo", choices=["demo", "nonprod", "prod", "regional"])
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    dashboard = generate_dashboard(
        app=args.app,
        purpose=args.purpose,
        metrics=[m.strip() for m in args.metrics.split(",") if m.strip()],
        env=args.env,
    )

    out = pathlib.Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w") as f:
        yaml.dump(dashboard, f, sort_keys=False)

    print(f"[ok] wrote {out}")


if __name__ == "__main__":
    main()