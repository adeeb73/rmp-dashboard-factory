import json, sys, pathlib, datetime

dashboard = json.loads(sys.argv[1])
out_dir = pathlib.Path("deployed")
out_dir.mkdir(exist_ok=True)

slug = dashboard["title"].lower().replace(" ", "-").replace("/", "-")
path = out_dir / f"{slug}.json"
path.write_text(json.dumps(dashboard, indent=2))

print(f"[mock-rmp] deployed {path} at {datetime.datetime.utcnow().isoformat()}")
