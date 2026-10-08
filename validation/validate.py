import sys
import json
import yaml
import pathlib
from jsonschema import validate, ValidationError

ROOT = pathlib.Path(__file__).resolve().parent.parent

def load_yaml(path):
    with open(path) as f:
        return yaml.safe_load(f)

def load_json(path):
    with open(path) as f:
        return json.load(f)

def fail(msg):
    print(f"  x {msg}")
    sys.exit(1)

def ok(msg):
    print(f"  + {msg}")

def layer1(dashboard, schema):
    print("[Layer 1] Schema")
    try:
        validate(instance=dashboard, schema=schema)
    except ValidationError as e:
        fail(f"schema violation at {list(e.path)}: {e.message}")
    ok("schema OK")

def layer2(dashboard, standards):
    print("[Layer 2] Standards")
    prefix = standards.get("title_prefix", "")
    if prefix and not dashboard["title"].startswith(prefix):
        fail(f"title must start with '{prefix}'")

    missing = set(standards.get("required_tags", [])) - set(dashboard.get("tags", []))
    if missing:
        fail(f"missing required tags: {sorted(missing)}")

    allowed = set(standards.get("allowed_panel_types", []))
    for p in dashboard["panels"]:
        if p["type"] not in allowed:
            fail(f"panel '{p['name']}' uses disallowed type '{p['type']}'")

    n = len(dashboard["panels"])
    if n < standards.get("min_panels", 1):
        fail(f"too few panels: {n}")
    if n > standards.get("max_panels", 12):
        fail(f"too many panels: {n}")
    ok(f"{n} panels, tags and types OK")

def layer4(dashboard, standards):
    print("[Layer 4] Security")
    blob = json.dumps(dashboard).lower()
    for word in standards.get("forbidden_metric_substrings", []):
        if word in blob:
            fail(f"forbidden substring detected: '{word}'")
    ok("no secrets or forbidden substrings")

def main(path):
    schema = load_json(ROOT / "schemas/dashboard.schema.json")
    standards = load_yaml(ROOT / "standards/naming.yaml")
    dashboard = load_yaml(path)
    print(f"Validating {path}\n")
    layer1(dashboard, schema)
    layer2(dashboard, standards)
    layer4(dashboard, standards)
    print("\nAll checks passed.")

if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python validation/validate.py <path>")
        sys.exit(2)
    main(sys.argv[1])
