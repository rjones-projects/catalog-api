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

## Project structure

```
resolver/
├── app/
│   ├── __init__.py
│   ├── main.py          # FastAPI app, request/response models
│   └── resolver.py      # Core resolution & HCL generation logic
├── tests/
│   └── test_resolver.py
├── Dockerfile           # Multi-stage Alpine build
├── docker-compose.yml
├── requirements.txt
└── README.md
```
# branching strategy enforced
