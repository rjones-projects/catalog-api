"""
GitHub File API — fetches files from GitHub repos and returns them as YAML.
"""

import base64
import json
import logging
import os
import yaml
from typing import Any, Optional

from dotenv import load_dotenv
load_dotenv()

from fastapi import FastAPI, HTTPException, Query, Depends
from fastapi.responses import Response
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from github import Github, GithubException
from pydantic import BaseModel, Field

from app.catalog_resolver import CatalogResolver

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ── App setup ───────────────────────────────────────────────────────────────

app = FastAPI(
    title="GitHub File API",
    description="Fetch files from GitHub repositories and return them as YAML",
    version="1.0.0",
)

security = HTTPBearer(auto_error=False)

# ── Catalog config ───────────────────────────────────────────────────────────
# Override these via environment variables:
#   CATALOG_OWNER  — GitHub user/org that owns the catalog repo
#   CATALOG_REPO   — repo name (default: "catalog")
#   CATALOG_FILE   — path to the YAML file inside the repo (default: "catalog.yaml")
#   CATALOG_REF    — branch/tag/SHA (default: "HEAD")

CATALOG_OWNER = os.getenv("CATALOG_OWNER", "rjones-projects")
CATALOG_REPO  = os.getenv("CATALOG_REPO",  "catalog")
CATALOG_FILE  = os.getenv("CATALOG_FILE",  "catalog.yaml")
CATALOG_REF   = os.getenv("CATALOG_REF",   "HEAD")


# ── Models ───────────────────────────────────────────────────────────────────

class FileResponse(BaseModel):
    repo: str
    path: str
    branch: str
    sha: str
    size: int
    content: object  # parsed file content (dict/list/str)
    encoding: str


# ── Helpers ──────────────────────────────────────────────────────────────────

def get_github_client(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
    token: Optional[str] = Query(None, description="GitHub Personal Access Token"),
) -> Github:
    """Resolve GitHub token from Bearer header or ?token= query param."""
    resolved_token = None
    if credentials:
        resolved_token = credentials.credentials
    elif token:
        resolved_token = token

    if resolved_token:
        return Github(resolved_token)
    # Anonymous — 60 req/hr rate limit, public repos only
    return Github()


def decode_content(content_bytes: bytes, path: str) -> object:
    """
    Attempt to parse the raw bytes into a Python object.
    Priority: JSON → YAML → plain text
    """
    text = content_bytes.decode("utf-8", errors="replace")

    # JSON files → parsed dict/list
    if path.endswith(".json"):
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass

    # YAML / YML files → already structured; handle multi-document streams
    if path.endswith((".yaml", ".yml")):
        try:
            docs = [d for d in yaml.safe_load_all(text) if d is not None]
            return docs[0] if len(docs) == 1 else docs
        except yaml.YAMLError:
            pass

    # Everything else: return as plain string (YAML will quote it)
    return text


def to_yaml_response(data: dict) -> Response:
    """Serialize a dict to YAML and return as text/yaml."""
    yaml_str = yaml.dump(data, allow_unicode=True, sort_keys=False, default_flow_style=False)
    return Response(content=yaml_str, media_type="text/yaml; charset=utf-8")


# ── Catalog helper ────────────────────────────────────────────────────────────

def fetch_catalog(gh: Github) -> list:
    """
    Pull catalog.yaml from the catalog repo and parse all --- separated documents.
    Always returns a list — one entry per YAML document in the file.
    Raises HTTPException on any GitHub or YAML error.
    """
    try:
        repo = gh.get_repo(f"{CATALOG_OWNER}/{CATALOG_REPO}")
    except GithubException as exc:
        raise HTTPException(
            status_code=404,
            detail=f"Catalog repo '{CATALOG_OWNER}/{CATALOG_REPO}' not found: {exc.data.get('message', exc)}",
        )

    try:
        fc = repo.get_contents(CATALOG_FILE, ref=CATALOG_REF)
    except GithubException as exc:
        raise HTTPException(
            status_code=404,
            detail=f"Catalog file '{CATALOG_FILE}' not found: {exc.data.get('message', exc)}",
        )

    raw_bytes = base64.b64decode(fc.content)
    try:
        documents = list(yaml.safe_load_all(raw_bytes.decode("utf-8", errors="replace")))
    except yaml.YAMLError as exc:
        raise HTTPException(status_code=500, detail=f"Failed to parse catalog YAML: {exc}")

    # Filter out empty documents (e.g. trailing ---)
    documents = [d for d in documents if d is not None]

    if not documents:
        raise HTTPException(status_code=500, detail="Catalog file is empty or contains no valid documents")

    return documents


# ── Routes ───────────────────────────────────────────────────────────────────

@app.get("/", include_in_schema=False)
def root():
    return {"message": "GitHub File API — visit /docs for usage"}

@app.get("/health")
def health():
    return {"status": "ok"}

# ── Catalog endpoints ────────────────────────────────────────────────────────

@app.get(
    "/catalog",
    summary="Return the full catalog as YAML",
    response_class=Response,
    responses={
        200: {"content": {"text/yaml": {}}},
        404: {"description": "Catalog repo or file not found"},
    },
)
def get_catalog(gh: Github = Depends(get_github_client)):
    """
    Fetches **all documents** from the multi-document `catalog.yaml` and returns them
    as a YAML stream separated by `---`, preserving the original structure.

    The catalog file location is controlled by environment variables:
    - `CATALOG_OWNER` — GitHub user/org
    - `CATALOG_REPO`  — repo name (default: `catalog`)
    - `CATALOG_FILE`  — file path inside the repo (default: `catalog.yaml`)
    - `CATALOG_REF`   — branch/tag/SHA (default: `HEAD`)
    """
    documents = fetch_catalog(gh)
    yaml_str = yaml.dump_all(documents, allow_unicode=True, sort_keys=False, default_flow_style=False, explicit_start=True)
    return Response(content=yaml_str, media_type="text/yaml; charset=utf-8")


@app.get(
    "/catalog/{index}",
    summary="Return a single indexed item from the catalog",
    response_class=Response,
    responses={
        200: {"content": {"text/yaml": {}}},
        400: {"description": "Catalog is not a list — use a key instead"},
        404: {"description": "Index out of range or key not found"},
    },
)
def get_catalog_item(
    index: str,
    gh: Github = Depends(get_github_client),
):
    """
    Returns **a single document** from the multi-document catalog by 0-based integer index.

    - `GET /catalog/0` → first `---` document
    - `GET /catalog/1` → second `---` document
    - etc.

    Returns a 404 if the index is out of range.
    """
    documents = fetch_catalog(gh)

    try:
        i = int(index)
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail=f"Index must be an integer, got '{index}'",
        )

    if i < 0 or i >= len(documents):
        raise HTTPException(
            status_code=404,
            detail=f"Index {i} is out of range — catalog has {len(documents)} documents (0–{len(documents)-1})",
        )

    item = documents[i]

    if isinstance(item, (dict, list)):
        return to_yaml_response(item)
    return to_yaml_response({"value": item})


@app.get(
    "/repos/{owner}/{repo}/file",
    summary="Fetch a single file as YAML",
    response_class=Response,
    responses={
        200: {"content": {"text/yaml": {}}},
        404: {"description": "File or repo not found"},
    },
)
def get_file(
    owner: str,
    repo: str,
    path: str = Query(..., description="Path to the file within the repo, e.g. src/config.json"),
    ref: str = Query("HEAD", description="Branch, tag, or commit SHA (default: HEAD)"),
    raw: bool = Query(False, description="If true, return raw YAML without metadata wrapper"),
    gh: Github = Depends(get_github_client),
):
    """
    Fetch **a single file** from a GitHub repository and return it as YAML.

    - Supports any text-based file (JSON, YAML, plain text, config files, etc.)
    - JSON and YAML files are parsed into structured objects before serialising
    - Set `raw=true` to get just the file content (no metadata envelope)
    """
    try:
        repository = gh.get_repo(f"{owner}/{repo}")
    except GithubException as exc:
        raise HTTPException(status_code=404, detail=f"Repository not found: {exc.data.get('message', exc)}")

    try:
        file_content = repository.get_contents(path, ref=ref)
    except GithubException as exc:
        raise HTTPException(status_code=404, detail=f"File not found: {exc.data.get('message', exc)}")

    if isinstance(file_content, list):
        raise HTTPException(status_code=400, detail="Path points to a directory — use /tree endpoint instead")

    raw_bytes = base64.b64decode(file_content.content)
    parsed = decode_content(raw_bytes, path)

    if raw:
        return to_yaml_response(parsed if isinstance(parsed, dict) else {"content": parsed})

    envelope = {
        "repo": f"{owner}/{repo}",
        "path": file_content.path,
        "branch": ref,
        "sha": file_content.sha,
        "size": file_content.size,
        "html_url": file_content.html_url,
        "content": parsed,
    }
    return to_yaml_response(envelope)


@app.get(
    "/repos/{owner}/{repo}/tree",
    summary="List directory contents as YAML",
    response_class=Response,
    responses={200: {"content": {"text/yaml": {}}}},
)
def get_tree(
    owner: str,
    repo: str,
    path: str = Query("", description="Directory path (empty string = repo root)"),
    ref: str = Query("HEAD", description="Branch, tag, or commit SHA"),
    gh: Github = Depends(get_github_client),
):
    """
    List the **contents of a directory** in a GitHub repository as YAML.
    Returns file names, paths, types, sizes, and SHAs.
    """
    try:
        repository = gh.get_repo(f"{owner}/{repo}")
    except GithubException as exc:
        raise HTTPException(status_code=404, detail=f"Repository not found: {exc.data.get('message', exc)}")

    try:
        contents = repository.get_contents(path or "/", ref=ref)
    except GithubException as exc:
        raise HTTPException(status_code=404, detail=f"Path not found: {exc.data.get('message', exc)}")

    if not isinstance(contents, list):
        contents = [contents]

    items = [
        {
            "name": item.name,
            "path": item.path,
            "type": item.type,  # "file" or "dir"
            "size": item.size,
            "sha": item.sha,
            "html_url": item.html_url,
        }
        for item in sorted(contents, key=lambda x: (x.type != "dir", x.name))
    ]

    result = {
        "repo": f"{owner}/{repo}",
        "path": path or "/",
        "ref": ref,
        "count": len(items),
        "entries": items,
    }
    return to_yaml_response(result)


@app.get(
    "/repos/{owner}/{repo}/files",
    summary="Fetch multiple files at once as YAML",
    response_class=Response,
    responses={200: {"content": {"text/yaml": {}}}},
)
def get_multiple_files(
    owner: str,
    repo: str,
    paths: list[str] = Query(..., description="One or more file paths (repeat ?paths= for each)"),
    ref: str = Query("HEAD", description="Branch, tag, or commit SHA"),
    gh: Github = Depends(get_github_client),
):
    """
    Fetch **multiple files** in a single request.
    Returns all files in one YAML document, keyed by path.
    Files that cannot be found are included with an `error` field.
    """
    try:
        repository = gh.get_repo(f"{owner}/{repo}")
    except GithubException as exc:
        raise HTTPException(status_code=404, detail=f"Repository not found: {exc.data.get('message', exc)}")

    results = {}
    for path in paths:
        try:
            fc = repository.get_contents(path, ref=ref)
            if isinstance(fc, list):
                results[path] = {"error": "path is a directory"}
                continue
            raw_bytes = base64.b64decode(fc.content)
            parsed = decode_content(raw_bytes, path)
            results[path] = {
                "sha": fc.sha,
                "size": fc.size,
                "content": parsed,
            }
        except GithubException as exc:
            results[path] = {"error": exc.data.get("message", str(exc))}

    envelope = {
        "repo": f"{owner}/{repo}",
        "ref": ref,
        "files": results,
    }
    return to_yaml_response(envelope)


# ── Catalog resolve endpoint ─────────────────────────────────────────────────

class DeploymentPayload(BaseModel):
    patternId: Optional[str] = None
    projectId: Optional[str] = None
    projectName: Optional[str] = None
    building_blocks: dict[str, Any] = Field(
        ...,
        description="Map of building block name to override values dict (empty dict {} for no overrides).",
    )
    terraform_version: Optional[str] = Field("~> 1.9")
    backend: Optional[str] = None
    modules_ref: Optional[str] = Field("main")
    estimatedMonthlyCost: Optional[float] = None
    createdBy: Optional[str] = None
    timestamp: Optional[str] = None


class DeploymentRequest(BaseModel):
    deploymentId: Optional[str] = None
    status: Optional[str] = None
    payload: DeploymentPayload
    message: Optional[str] = None
    createdBy: Optional[str] = None
    timestamp: Optional[str] = None


class DeploymentResolveResponse(BaseModel):
    deploymentId: Optional[str] = None
    status: str = "resolved"
    projectId: Optional[str] = None
    projectName: Optional[str] = None
    main_tf: str
    variables_tf: str
    terraform_tfvars: str
    summary: dict


@app.post(
    "/catalog/resolve",
    response_model=DeploymentResolveResponse,
    summary="Resolve building blocks into Terraform files",
    responses={
        502: {"description": "Failed to fetch catalog mapping or module variables from GitHub"},
    },
)
def resolve_catalog(request: DeploymentRequest):
    """
    Accepts a deployment payload containing **building block** names mapped to their
    variable overrides. Resolves each block to GCP Terraform modules via the catalog
    mapping YAML, fetches each module's `variables.tf` from GitHub, and returns
    ready-to-use `main.tf`, `variables.tf`, and `terraform.tfvars`.

    The `projectId` from the payload is written as `project_id` at the top of
    `terraform.tfvars`. Override values for each block are routed to the correct
    module config variable and deduplicated across blocks.
    """
    try:
        p = request.payload
        overrides_map: dict[str, Any] = p.building_blocks
        block_names = list(overrides_map.keys())

        # Preamble vars written at the top of terraform.tfvars before block sections.
        preamble: dict[str, Any] = {}
        if p.projectId:
            preamble["project_id"] = p.projectId

        resolver = CatalogResolver(
            building_blocks=block_names,
            terraform_version=p.terraform_version or "~> 1.9",
            backend=p.backend,
            modules_ref=p.modules_ref or "main",
        )
        result = resolver.resolve(overrides_map=overrides_map, tfvars_preamble=preamble or None)

        return {
            "deploymentId": request.deploymentId,
            "status": "resolved",
            "projectId": p.projectId,
            "projectName": p.projectName,
            **result,
        }
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except Exception as exc:
        logger.exception("Unexpected error during catalog resolution")
        raise HTTPException(status_code=500, detail=str(exc))


class BuildingBlockResponse(BaseModel):
    building_block: str
    modules: list[str]
    variables_tf: str


@app.get(
    "/buildingblock/{name}",
    response_model=BuildingBlockResponse,
    summary="Return variables.tf for a single building block",
    responses={
        404: {"description": "Building block not found in catalog"},
        502: {"description": "Failed to fetch catalog mapping or module variables from GitHub"},
    },
)
def get_building_block(
    name: str,
    ref: str = Query("main", description="Git ref to pin module sources to"),
):
    """
    Resolves a single **building block** (e.g. `bucket`, `sql`, `network`) to its
    constituent GCP Terraform modules and returns the merged `variables.tf`.
    """
    try:
        resolver = CatalogResolver(building_blocks=[name], modules_ref=ref)
        result = resolver.resolve()
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    except Exception as exc:
        logger.exception("Unexpected error resolving building block '%s'", name)
        raise HTTPException(status_code=500, detail=str(exc))

    if name in result["summary"]["building_blocks_unresolved"]:
        raise HTTPException(status_code=404, detail=f"Building block '{name}' not found in catalog")

    return {
        "building_block": name,
        "modules": result["summary"]["modules_resolved"],
        "variables_tf": result["variables_tf"],
    }


@app.get(
    "/repos/{owner}/{repo}/info",
    summary="Repository metadata as YAML",
    response_class=Response,
    responses={200: {"content": {"text/yaml": {}}}},
)
def get_repo_info(
    owner: str,
    repo: str,
    gh: Github = Depends(get_github_client),
):
    """Return basic repository metadata as YAML."""
    try:
        repository = gh.get_repo(f"{owner}/{repo}")
    except GithubException as exc:
        raise HTTPException(status_code=404, detail=f"Repository not found: {exc.data.get('message', exc)}")

    info = {
        "name": repository.name,
        "full_name": repository.full_name,
        "description": repository.description,
        "default_branch": repository.default_branch,
        "private": repository.private,
        "language": repository.language,
        "stars": repository.stargazers_count,
        "forks": repository.forks_count,
        "open_issues": repository.open_issues_count,
        "topics": repository.get_topics(),
        "created_at": str(repository.created_at),
        "updated_at": str(repository.updated_at),
        "clone_url": repository.clone_url,
        "html_url": repository.html_url,
    }
    return to_yaml_response(info)
