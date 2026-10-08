#!/usr/bin/env python
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
