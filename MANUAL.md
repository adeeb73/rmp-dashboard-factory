# RMP Dashboard Factory — User Manual

## Purpose and scope

The RMP Dashboard Factory provides a GitOps workflow for generating, reviewing, validating, and deploying dashboard definitions. Gemini creates a *candidate* definition. The deterministic validator is the approval gate; Git is the source of truth. Pull requests validate changes, and changes merged to main run the Terraform mock deployment.

**Current deployment target:** the repository contains a mock RMP deployer. It writes dashboard files and a manifest under deployed/. It does not connect to a live Resource Management Platform tenant or display live telemetry. The deployed artifacts are the demo's verification output.

## 1. Repository map

- dashboards/ — version-controlled RMP dashboard JSON definitions.
- requests/ — example natural-language requests.
- schemas/dashboard.schema.json — JSON Schema for dashboard documents.
- scripts/generate.py — Gemini candidate generation, response validation preparation, and local response cache.
- scripts/validate.py — deterministic JSON Schema, standards, and security checks.
- scripts/mock_deploy.py — local RMP deployment simulator and manifest writer.
- terraform/ — Terraform wrapper for deploying every dashboard definition.
- .github/workflows/pipeline.yml — pull request, main branch, and manual generation workflows.
- deployed/ — generated mock deployment output. JSON artifacts are ignored by Git and recreated by deploy.

## 2. Prerequisites

For local use, install:

- Python 3.11 or a compatible newer version.
- Terraform 1.9.8 for the same version used by GitHub Actions (the Terraform configuration accepts versions 1.5 and newer).
- A Gemini API key for generating new, uncached candidates.

Validation and mock deployment do not need a Gemini API key. Install the Python dependencies from the repository root.

## 3. Set up the local environment

In PowerShell, from the repository root:

    py -m venv .venv
    .\.venv\Scripts\Activate.ps1
    python -m pip install -r requirements.txt

Set a Gemini key for the current PowerShell session before generating a new candidate:

    $env:GEMINI_API_KEY = "your-key"

The generator also accepts the GOOGLE_API_KEY environment variable. Keep keys in environment variables or GitHub Actions secrets. Never add a key to a request, dashboard JSON file, or commit.

The generator caches responses in .cache/. Use --no-cache when you need to bypass an existing response for a request.

## 4. Generate a candidate dashboard

### Use one of the supplied request files

    python scripts/generate.py --request-file requests/payments-api.txt --out dashboards/

Or generate from the database example:

    python scripts/generate.py --request-file requests/orders-db.txt --out dashboards/

### Enter a request directly

    python scripts/generate.py --request "Payments API health dashboard with request rate, p99 latency, error rate, and connection pool saturation." --out dashboards/

The generator discovers generateContent-capable Gemini models at runtime, tries suitable candidates, requests JSON output shaped by the dashboard schema, and writes a JSON file named after metadata.name. It retries transient service failures and caches successful responses.

Useful options:

- --request or -r — provide request text directly. Required unless --request-file is used.
- --request-file — read the request from a UTF-8 text file.
- --out — choose the output directory; dashboards/ is the default.
- --print-only — print the candidate JSON without writing a file.
- --no-cache — ignore the cached result and call Gemini again.

Generation does not commit, validate, or deploy the candidate automatically. A successful model response is not yet approved.

## 5. Review and validate

Inspect the generated JSON in dashboards/ and edit it if needed. Then validate every dashboard:

    python scripts/validate.py --all

To validate one file:

    python scripts/validate.py dashboards/payments-api-sre.json

A successful result reports PASS and an APPROVED gate. A failure reports the file and specific policy errors. Resolve every error, then rerun validation.

The validator is deterministic and does not call Gemini. It checks:

1. JSON Schema conformance.
2. Platform rules, including the dashboard name, required tags, refresh interval, panel count, unique panel IDs, allowed panel types and units, and the rmp. metric prefix.
3. Forbidden credential-like strings, including passwords, secrets, tokens, API keys, private keys, and connection strings.

The current standards require:

- Dashboard names to be lowercase kebab-case, 3–40 characters, starting with a letter.
- At least one env:<value>, team:<value>, and owner:<value> tag.
- 3–12 panels, with unique lowercase snake_case IDs.
- Metrics beginning with rmp.
- An approved panel type and unit for each panel.
- A permitted refresh interval and time range.

The authoritative rules are implemented in scripts/validate.py and schemas/dashboard.schema.json. Keep those in sync when changing standards.

## 6. Local mock deployment

Always validate before deploying. From the repository root:

    python scripts/validate.py --all
    python scripts/mock_deploy.py --all --environment demo

To deploy only selected dashboard files:

    python scripts/mock_deploy.py dashboards/payments-api-sre.json --environment demo

The Terraform route is the same deployment path used by CI:

    cd terraform
    terraform init
    terraform plan -var="environment=demo"
    terraform apply -var="environment=demo"
    cd ..

The environment value is a label recorded in the generated manifest; the current deployer still targets the mock platform. The default label is local. If Python is named py on your Windows machine, pass the executable override:

    terraform apply -var="environment=demo" -var="python_executable=py"

The deployer writes one JSON file per dashboard and deployed/manifest.json. The manifest records the environment, dashboard count, generated time, checksums, and artifact paths. Re-running Terraform updates outputs when the dashboard content or environment changes. The mock deployer itself does not run the validator, so run validation first.

## 7. GitHub Actions workflow

The workflow is .github/workflows/pipeline.yml.

### Pull request

Opening or updating a pull request to main runs the deterministic validator and uploads the validated dashboard definitions as an Actions artifact. It does not deploy.

Recommended process:

1. Create a feature branch.
2. Generate and review the candidate.
3. Run validation locally.
4. Commit the dashboard JSON and open a pull request.
5. Address validation failures and review comments.
6. Merge only after approval and a successful validation run.

### Merge to main

A push to main runs validation first. Once validation succeeds, the deploy job runs Terraform, invokes the mock deployer, verifies the manifest, and uploads deployment files as an Actions artifact.

### Manual candidate generation

In GitHub, open the Actions tab, select rmp-dashboard-pipeline, choose **Run workflow**, and enter a natural-language request. The generate job needs the GEMINI_API_KEY repository secret. It creates a candidate, validates it, and uploads it as an artifact for human review. Download and inspect the candidate before adding it to a feature branch; manual generation does not merge or deploy it.

To add the secret, a repository administrator should open repository settings and add GEMINI_API_KEY under Actions secrets. The secret is only needed by the manual generation job.

Artifacts are retained for 14 days by the workflow.

## 8. Rollback

The intended rollback is a Git revert, so the desired dashboard state remains traceable in history.

1. Identify the commit that introduced the dashboard change.
2. On a new branch, revert that commit.
3. Run the validator and review the resulting diff.
4. Open a pull request and merge it after review.
5. The main-branch workflow validates the reverted state and reruns the mock deployment.

For example, after checking out a rollback branch:

    git revert <commit-sha>
    python scripts/validate.py --all
    git push -u origin <rollback-branch>

The mock deployer prunes generated files whose source dashboard no longer exists. Do not manually edit deployed/ as a substitute for reverting the source definition.

## 9. Troubleshooting

**The generator says the key is missing.** Set GEMINI_API_KEY or GOOGLE_API_KEY in the same shell that runs the command. A valid cached response may be used without making a new API call.

**The generator reports a schema or model error.** Retry once with --no-cache. Review the request and the model output. Do not bypass the validator; fix the definition and rerun scripts/validate.py.

**Validation fails.** Use the reported layer and field to fix the JSON, tags, panel IDs, metric prefix, panel count, units, refresh policy, or forbidden text.

**Terraform cannot find Python.** Run it from terraform/ and pass -var="python_executable=py" if your Python launcher is named py. Confirm Python dependencies were installed in the active environment.

**The deployment job did not run.** Deploy runs only for a push to main and only after the validation job succeeds. Pull requests and manual workflow runs do not deploy.

**The manifest is missing.** Check the Terraform apply logs and deploy job output. A successful mock deployment creates deployed/manifest.json.

## 10. Before connecting a real RMP tenant

The current workflow is intentionally a mock. A platform engineer must replace scripts/mock_deploy.py or the Terraform null_resource with the approved RMP Config-as-Code interface or Terraform provider. Add tenant credentials through protected CI secrets, define environment approvals and branch protections, and verify how the real platform handles updates and deletes. Never use the mock deployment result as evidence that a live tenant was changed.
