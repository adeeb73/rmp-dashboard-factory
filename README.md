# RMP Dashboard Factory

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

## User manual

For detailed setup, generation, validation, deployment, rollback, and troubleshooting instructions, see [MANUAL.md](MANUAL.md).
