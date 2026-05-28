# Terraform Module Resolver

A lightweight REST API (FastAPI on Alpine Linux) that accepts a list of Terraform modules and returns ready-to-use `main.tf` and `variables.tf` files.

## How it works

1. Each module source is checked against the **Terraform Public Registry API** — if it matches the `<namespace>/<module>/<provider>` pattern, variable metadata is fetched automatically.
2. For non-registry sources (GitHub, git URLs, local paths) the provider is **inferred from the source string**.
3. Variables from all modules are **merged and deduplicated**; required variables (no upstream default) receive safe placeholder defaults and are annotated with a comment.
4. The `terraform {}` block, provider stubs, module blocks, and `variables.tf` are generated in valid HCL.

---

## Running

```bash
# Docker
docker build -t resolver .
docker run -p 8080:8080 resolver

# Compose
docker compose up

# Local dev
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8080
```

---

## API

### `GET /healthz`
Returns `{"status": "ok"}`.

### `POST /resolve`

**Request body**

```json
{
  "modules": [
    {
      "source":  "terraform-aws-modules/vpc/aws",
      "version": "~> 5.0",
      "alias":   "vpc",
      "inputs":  {
        "vpc_cidr": "10.10.0.0/16"
      }
    },
    {
      "source":  "terraform-aws-modules/eks/aws",
      "version": "~> 20.0"
    }
  ],
  "terraform_version": "~> 1.6",
  "backend": "s3",
  "provider_overrides": {
    "aws": "~> 5.50"
  }
}
```

| Field | Type | Required | Description |
|---|---|---|---|
| `modules` | array | ✅ | List of modules (min 1) |
| `modules[].source` | string | ✅ | Registry path, git URL, or local path |
| `modules[].version` | string | | Version constraint |
| `modules[].alias` | string | | Override the generated module block name |
| `modules[].inputs` | object | | Hard-coded variable overrides (skip `var.*` reference) |
| `terraform_version` | string | | Default `~> 1.5` |
| `backend` | string | | Backend type stub (`s3`, `gcs`, `azurerm`, `local`, …) |
| `provider_overrides` | object | | Override detected provider versions |

**Response**

```json
{
  "main_tf": "terraform {\n  required_version = ...",
  "variables_tf": "variable \"vpc_cidr\" {\n ...",
  "summary": {
    "modules_resolved": 2,
    "variables_extracted": 31,
    "providers_detected": ["hashicorp/aws"]
  }
}
```

---

## Example curl

```bash
curl -s -X POST http://localhost:8080/resolve \
  -H 'Content-Type: application/json' \
  -d '{
    "modules": [
      {"source": "terraform-aws-modules/vpc/aws", "version": "~> 5.0"},
      {"source": "terraform-aws-modules/rds/aws", "version": "~> 6.0"}
    ],
    "backend": "s3"
  }' | jq .
```

---

## Running tests

```bash
pip install pytest pytest-asyncio
pytest tests/ -v
```

---

### `POST /catalog/resolve`

Accepts a list of **building block** names, resolves them to GCP Terraform modules
via [`gcp-mapping.yaml`](https://github.com/rjones-projects/catalog/blob/main/gcp-mapping.yaml),
fetches each module's `variables.tf` from [`gcp_terraform-modules`](https://github.com/rjones-projects/gcp_terraform-modules),
and returns ready-to-use `main.tf` and `variables.tf` files.

**Request body**

```json
{
  "building_blocks": ["bucket", "sql", "network"],
  "terraform_version": "~> 1.9",
  "backend": "gcs",
  "modules_ref": "main"
}
```

| Field | Type | Required | Description |
|---|---|---|---|
| `building_blocks` | array | ✅ | Building block names from the catalog (min 1) |
| `terraform_version` | string | | Default `~> 1.9` |
| `backend` | string | | Backend type stub (`gcs`, `s3`, `azurerm`, …) |
| `modules_ref` | string | | Git ref to pin module sources to (default `main`) |

**Response**

```json
{
  "main_tf": "terraform {\n  required_version = ...",
  "variables_tf": "variable \"project_id\" {\n ...",
  "summary": {
    "building_blocks_requested": ["bucket", "sql", "network"],
    "building_blocks_resolved": ["bucket", "sql", "network"],
    "building_blocks_unresolved": [],
    "modules_resolved": ["gcs", "cloud_sql", "network", "firewall", "dns"],
    "variables_extracted": 24,
    "modules_with_fetch_errors": []
  }
}
```

**Available building blocks** (from catalog):
`analytics`, `bastion`, `bucket`, `delivery`, `environment`, `iam`, `integration`,
`k8s`, `keys`, `network`, `network-policy`, `platform-operations`, `pubsub`,
`security-operations`, `security-policy`, `serverless_app`, `sql`, `vm_workload`, `workflow`

**Example curl**

```bash
curl -s -X POST http://localhost:8080/catalog/resolve \
  -H 'Content-Type: application/json' \
  -d '{
    "building_blocks": ["bucket", "sql"],
    "backend": "gcs"
  }' | jq .
```

---

## Project structure

```
catalog-api/
├── app/
│   ├── __init__.py
│   ├── main.py               # FastAPI app, routes, request/response models
│   ├── resolver.py           # Terraform Registry resolver & HCL generation
│   └── catalog_resolver.py  # Building-block → GCP module resolver
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
└── README.md
```
# branching strategy enforced


# Create a service account
gcloud iam service-accounts create github-actions  --project=vf-gned-ngdi-alpha-ing

# Grant required roles
gcloud projects add-iam-policy-binding vf-gned-ngdi-alpha-ing --member="serviceAccount:github-actions@vf-gned-ngdi-alpha-ing.iam.gserviceaccount.com" --role="roles/artifactregistry.writer"
gcloud projects add-iam-policy-binding vf-gned-ngdi-alpha-ing --member="serviceAccount:github-actions@vf-gned-ngdi-alpha-ing.iam.gserviceaccount.com" --role="roles/run.developer"
#add IAM permissions
gcloud iam service-accounts add-iam-policy-binding  479677124022-compute@developer.gserviceaccount.com --project=vf-gned-ngdi-alpha-ing  --role="roles/iam.serviceAccountUser"  --member="serviceAccount:github-actions@vf-gned-ngdi-alpha-ing.iam.gserviceaccount.com"
gcloud projects add-iam-policy-binding vf-gned-ngdi-alpha-ing --member="serviceAccount:github-actions@vf-gned-ngdi-alpha-ing.iam.gserviceaccount.com" --role="roles/run.admin"

# Create WIF pool + provider (swap in your GitHub org/repo)
gcloud iam workload-identity-pools create github-pool --project=vf-gned-ngdi-alpha-ing --location=global
gcloud iam workload-identity-pools providers create-oidc github-provider --project=vf-gned-ngdi-alpha-ing --location=global --workload-identity-pool=github-pool --issuer-uri="https://token.actions.githubusercontent.com"  --attribute-mapping="google.subject=assertion.sub,attribute.repository=assertion.repository" --attribute-condition="assertion.repository=='rjones-projects/catalog-api'"

# Allow the pool to impersonate the SA
gcloud iam service-accounts add-iam-policy-binding github-actions@vf-gned-ngdi-alpha-ing.iam.gserviceaccount.com --project=vf-gned-ngdi-alpha-ing --role="roles/iam.workloadIdentityUser" --member="principalSet://iam.googleapis.com/projects/$(gcloud projects describe vf-gned-ngdi-alpha-ing --format='value(projectNumber)')/locations/global/workloadIdentityPools/github-pool/attribute.repository/rjones-projects/catalog-api"

#create secrets
 Settings → Secrets and variables → Actions → New repository secret

#get the secret - WIF_PROVIDER
gcloud iam workload-identity-pools providers describe github-provider --project=vf-gned-ngdi-alpha-ing --location=global --workload-identity-pool=github-pool --format="value(name)"

#secret - WIF_SERVICE_ACCOUNT
github-actions@vf-gned-ngdi-alpha-ing.iam.gserviceaccount.com

#added github variables for 
CATALOG_OWNER=rjones-projects
CATALOG_REPO=catalog
CATALOG_FILE=catalog.yaml


docker build -t catalog-api .
#docker tag catalog-api europe-west2-docker.pkg.dev/vf-gned-ngdi-alpha-ing/catalog-api/catalog-api:latest
#docker push europe-west2-docker.pkg.dev/vf-gned-ngdi-alpha-ing/catalog-api/catalog-api:latest
#docker run -p 8080:8080 catalog-api 


