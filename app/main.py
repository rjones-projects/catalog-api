"""
GitHub File API — proxies all GitHub file access through the configured file service.
"""

import logging
import os
import yaml
from typing import Any, Optional

import httpx
from dotenv import load_dotenv
load_dotenv()

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel, Field

from app.catalog_resolver import CatalogResolver
from app.file_client import FileServiceClient, get_client

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ── App setup ───────────────────────────────────────────────────────────────

app = FastAPI(
    title="GitHub File API",
    description="Fetch files from GitHub repositories and return them as YAML",
    version="1.0.0",
)

# ── Catalog config ───────────────────────────────────────────────────────────
# CATALOG_OWNER / CATALOG_REPO / CATALOG_FILE / CATALOG_REF control which
# file the /catalog endpoint serves. FILE_SERVICE_URL (in file_client.py)
# controls where all GitHub calls are routed.

CATALOG_OWNER = os.getenv("CATALOG_OWNER", "rjones-projects")
CATALOG_REPO  = os.getenv("CATALOG_REPO",  "catalog")
CATALOG_FILE  = os.getenv("CATALOG_FILE",  "catalog.yaml")
CATALOG_REF   = os.getenv("CATALOG_REF",   "HEAD")


# ── Helpers ──────────────────────────────────────────────────────────────────

def to_yaml_response(data: object) -> Response:
    """Serialize a Python object to YAML and return as text/yaml."""
    yaml_str = yaml.dump(data, allow_unicode=True, sort_keys=False, default_flow_style=False)
    return Response(content=yaml_str, media_type="text/yaml; charset=utf-8")


def _proxy_response(upstream: httpx.Response) -> Response:
    """Forward an upstream file-service response verbatim."""
    return Response(content=upstream.content, media_type="text/yaml; charset=utf-8")


def _service_error(exc: Exception, detail: str) -> HTTPException:
    """Map a file-service HTTP error to a FastAPI HTTPException."""
    if isinstance(exc, httpx.HTTPStatusError):
        return HTTPException(status_code=exc.response.status_code, detail=detail)
    return HTTPException(status_code=502, detail=f"File service error: {exc}")


# ── Catalog helper ────────────────────────────────────────────────────────────

def fetch_catalog() -> list:
    """
    Fetch the catalog file from the file service and return parsed documents.
    Raises HTTPException on error.
    """
    try:
        return get_client().proxy_catalog_file(
            CATALOG_OWNER, CATALOG_REPO, CATALOG_FILE, ref=CATALOG_REF
        )
    except httpx.HTTPStatusError as exc:
        raise HTTPException(
            status_code=exc.response.status_code,
            detail=f"Catalog file '{CATALOG_FILE}' not found or inaccessible",
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"File service error: {exc}")


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
def get_catalog():
    """
    Fetches **all documents** from the catalog file via the file service and returns
    them as a YAML stream separated by `---`.

    Controlled by environment variables:
    - `CATALOG_OWNER`, `CATALOG_REPO`, `CATALOG_FILE`, `CATALOG_REF`
    - `FILE_SERVICE_URL` — base URL of the file service (default: https://repo-api-479677124022.europe-west2.run.app)
    """
    documents = fetch_catalog()
    yaml_str = yaml.dump_all(
        documents,
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
        explicit_start=True,
    )
    return Response(content=yaml_str, media_type="text/yaml; charset=utf-8")


@app.get(
    "/catalog/{index}",
    summary="Return a single indexed item from the catalog",
    response_class=Response,
    responses={
        200: {"content": {"text/yaml": {}}},
        400: {"description": "Index is not an integer"},
        404: {"description": "Index out of range"},
    },
)
def get_catalog_item(index: str):
    """
    Returns **a single document** from the multi-document catalog by 0-based integer index.
    """
    documents = fetch_catalog()

    try:
        i = int(index)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Index must be an integer, got '{index}'")

    if i < 0 or i >= len(documents):
        raise HTTPException(
            status_code=404,
            detail=f"Index {i} out of range — catalog has {len(documents)} documents (0–{len(documents)-1})",
        )

    item = documents[i]
    return to_yaml_response(item if isinstance(item, (dict, list)) else {"value": item})


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
    path: str = Query(..., description="Path to the file within the repo"),
    ref: str = Query("HEAD", description="Branch, tag, or commit SHA"),
    raw: bool = Query(False, description="If true, return content only (no metadata wrapper)"),
):
    """Fetch a single file from a GitHub repository via the file service."""
    try:
        return _proxy_response(get_client().proxy_file(owner, repo, path, ref=ref, raw=raw))
    except Exception as exc:
        raise _service_error(exc, f"File not found: {owner}/{repo}/{path}@{ref}")


@app.get(
    "/repos/{owner}/{repo}/tree",
    summary="List directory contents as YAML",
    response_class=Response,
    responses={200: {"content": {"text/yaml": {}}}},
)
def get_tree(
    owner: str,
    repo: str,
    path: str = Query("", description="Directory path (empty = repo root)"),
    ref: str = Query("HEAD", description="Branch, tag, or commit SHA"),
):
    """List the contents of a directory in a GitHub repository."""
    try:
        return _proxy_response(get_client().proxy_tree(owner, repo, path=path, ref=ref))
    except Exception as exc:
        raise _service_error(exc, f"Path not found: {owner}/{repo}/{path}@{ref}")


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
):
    """Fetch multiple files in a single request via the file service."""
    try:
        return _proxy_response(get_client().proxy_files(owner, repo, paths, ref=ref))
    except Exception as exc:
        raise _service_error(exc, f"Failed to fetch files from {owner}/{repo}@{ref}")


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
def get_repo_info(owner: str, repo: str):
    """Return basic repository metadata via the file service."""
    try:
        return _proxy_response(get_client().proxy_repo_info(owner, repo))
    except Exception as exc:
        raise _service_error(exc, f"Repository not found: {owner}/{repo}")
