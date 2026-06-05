# Catalog API

A lightweight REST API (FastAPI on Alpine Linux) that serves the catalog and
building-block definitions sourced from GitHub via a configurable file service.

## How it works

1. The `/catalog` endpoints fetch a multi-document YAML catalog file via the file
   service and return it as YAML.
2. The `/buildingblock/{name}` endpoint resolves a single building block to its
   constituent GCP Terraform modules via [`gcp-mapping.yaml`](https://github.com/rjones-projects/catalog/blob/main/gcp-mapping.yaml),
   fetches each module's `variables.tf` from [`gcp_terraform-modules`](https://github.com/rjones-projects/gcp_terraform-modules),
   and returns the merged `variables.tf`.

---

## Running

```bash
# Docker
docker build -t catalog-api .
docker run -p 8080:8080 catalog-api

# Local dev
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8080
```

---

## API

### `GET /health`
Returns `{"status": "ok"}`.

### `GET /catalog`

Returns **all documents** from the catalog file as a YAML stream separated by `---`.

Controlled by environment variables: `CATALOG_OWNER`, `CATALOG_REPO`,
`CATALOG_FILE`, `CATALOG_REF`, and `FILE_SERVICE_URL`.

```bash
curl -s http://localhost:8080/catalog
```

### `GET /catalog/{index}`

Returns **a single document** from the multi-document catalog by 0-based integer index.

```bash
curl -s http://localhost:8080/catalog/0
```

### `GET /buildingblock/{name}`

Resolves a single **building block** (e.g. `bucket`, `sql`, `network`) to its
constituent GCP Terraform modules and returns the merged `variables.tf`.

| Param | In | Required | Description |
|---|---|---|---|
| `name` | path | ✅ | Building block name from the catalog |
| `ref` | query | | Git ref to pin module sources to (default `main`) |

**Response**

```json
{
  "building_block": "bucket",
  "modules": ["gcs"],
  "variables_tf": "variable \"project_id\" {\n ..."
}
```

**Available building blocks** (from catalog):
`analytics`, `bastion`, `bucket`, `delivery`, `environment`, `iam`, `integration`,
`k8s`, `keys`, `network`, `network-policy`, `platform-operations`, `pubsub`,
`security-operations`, `security-policy`, `serverless_app`, `sql`, `vm_workload`, `workflow`

```bash
curl -s "http://localhost:8080/buildingblock/bucket?ref=main" | jq .
```

---

## Project structure

```
catalog-api/
├── app/
│   ├── __init__.py
│   ├── main.py              # FastAPI app, routes, response models
│   ├── catalog_resolver.py  # Building-block → GCP module resolver
│   └── file_client.py       # Client for the GitHub file service
├── Dockerfile
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


