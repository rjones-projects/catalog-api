from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from typing import Optional
import logging

from resolver import TerraformResolver

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(
    title="Terraform Module Resolver",
    description="Resolves Terraform modules and generates main.tf + variables.tf",
    version="1.0.0",
)


class ModuleInput(BaseModel):
    source: str = Field(..., description="Terraform module source (registry, git, or local path)")
    version: Optional[str] = Field(None, description="Module version constraint (e.g. '~> 3.0')")
    alias: Optional[str] = Field(None, description="Local name for the module block")
    inputs: Optional[dict] = Field(default_factory=dict, description="Variable overrides to pass into the module")


class ResolveRequest(BaseModel):
    modules: list[ModuleInput] = Field(..., min_length=1, description="List of modules to include")
    terraform_version: Optional[str] = Field("~> 1.5", description="Required Terraform version constraint")
    backend: Optional[str] = Field(None, description="Backend type, e.g. 's3', 'gcs', 'azurerm', 'local'")
    provider_overrides: Optional[dict] = Field(
        default_factory=dict,
        description="Provider version overrides, e.g. {'aws': '~> 5.0'}"
    )


class ResolveResponse(BaseModel):
    main_tf: str
    variables_tf: str
    summary: dict


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/resolve", response_model=ResolveResponse)
def resolve(request: ResolveRequest):
    try:
        resolver = TerraformResolver(request)
        result = resolver.resolve()
        return result
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except Exception as e:
        logger.exception("Unexpected error during resolution")
        raise HTTPException(status_code=500, detail=str(e))
