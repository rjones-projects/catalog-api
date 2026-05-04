# GitHub File API

A RESTful API that fetches files from GitHub repositories and returns them as YAML.

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env   # add your GitHub token
```

## Run

```bash
uvicorn main:app --reload --port 8000
```

Interactive docs: http://localhost:8000/docs

---

## Endpoints

### GET `/repos/{owner}/{repo}/file`
Fetch a single file as YAML.

| Parameter | Type   | Description                          |
|-----------|--------|--------------------------------------|
| `path`    | string | File path in repo (required)         |
| `ref`     | string | Branch/tag/SHA (default: HEAD)       |
| `raw`     | bool   | Skip metadata envelope (default: false) |

**Examples:**

```bash
# Fetch a JSON config file (parsed → structured YAML)
curl "http://localhost:8000/repos/octocat/Hello-World/file?path=.github/workflows/ci.yml"

# With auth token
curl -H "Authorization: Bearer ghp_..." \
     "http://localhost:8000/repos/myorg/private-repo/file?path=config/settings.json"

# From a specific branch
curl "http://localhost:8000/repos/owner/repo/file?path=README.md&ref=develop"

# Raw content only (no metadata)
curl "http://localhost:8000/repos/owner/repo/file?path=config.json&raw=true"
```

**Response:**
```yaml
repo: owner/repo
path: config/settings.json
branch: HEAD
sha: abc123...
size: 1024
html_url: https://github.com/owner/repo/blob/main/config/settings.json
content:
  database:
    host: localhost
    port: 5432
  debug: false
```

---

### GET `/repos/{owner}/{repo}/files`
Fetch multiple files in one request.

```bash
curl "http://localhost:8000/repos/owner/repo/files?paths=README.md&paths=package.json&paths=src/config.yaml"
```

**Response:**
```yaml
repo: owner/repo
ref: HEAD
files:
  README.md:
    sha: abc...
    size: 512
    content: "# My Project\n..."
  package.json:
    sha: def...
    size: 800
    content:
      name: my-project
      version: 1.0.0
  src/config.yaml:
    sha: ghi...
    size: 256
    content:
      env: production
```

---

### GET `/repos/{owner}/{repo}/tree`
List directory contents as YAML.

```bash
curl "http://localhost:8000/repos/owner/repo/tree?path=src"
```

---

### GET `/repos/{owner}/{repo}/info`
Repository metadata as YAML.

```bash
curl "http://localhost:8000/repos/owner/repo/info"
```

---

## Authentication

Pass your GitHub token in one of two ways:

```bash
# Bearer header (recommended)
curl -H "Authorization: Bearer ghp_yourtoken" ...

# Query parameter
curl "http://localhost:8000/...?token=ghp_yourtoken"
```

Without a token: 60 requests/hour, public repos only.  
With a token: 5,000 requests/hour, private repos accessible.

#docker build -t resolver-api .
#docker tag resolver-api europe-west2-docker.pkg.dev/idp-poc-495014/resolver-api/resolver-api:latest
#docker push europe-west2-docker.pkg.dev/idp-poc-495014/resolver-api/resolver-api:latest
#docker build -t resolver-api . && docker run -p 8080:8080 resolver-api