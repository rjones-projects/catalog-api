"""
Catalog API — serves the GitHub catalog and building-block definitions via the
configured file service.
"""

import logging
import os
import yaml

import httpx
from dotenv import load_dotenv
load_dotenv()

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel

from app.catalog_resolver import CatalogResolver
from app.file_client import get_client

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ── App setup ───────────────────────────────────────────────────────────────

app = FastAPI(
    title="Catalog API",
    description="Serve the catalog and building-block definitions as YAML",
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
        # Surface the upstream file-service detail so genuine causes (e.g. a
        # GitHub 403 rate-limit relayed as a 404) aren't masked as "not found".
        upstream = exc.response.text.strip()
        detail = f"Catalog file '{CATALOG_FILE}' not found or inaccessible"
        if upstream:
            detail = f"{detail} (file service said: {upstream})"
        raise HTTPException(status_code=exc.response.status_code, detail=detail)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"File service error: {exc}")


# ── Routes ───────────────────────────────────────────────────────────────────

@app.get("/", include_in_schema=False)
def root():
    return {"message": "Catalog API — visit /docs for usage"}

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


# ── Building block endpoint ───────────────────────────────────────────────────

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
