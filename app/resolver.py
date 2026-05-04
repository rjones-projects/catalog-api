"""
TerraformResolver

Fetches module metadata from the Terraform Registry and/or parses local
module sources to extract variable definitions, then produces:
  - main.tf   : terraform{} block + provider placeholders + module blocks
  - variables.tf : all referenced variables with sensible defaults
"""

from __future__ import annotations

import re
import textwrap
import logging
import httpx
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .main import ResolveRequest

logger = logging.getLogger(__name__)

REGISTRY_API = "https://registry.terraform.io/v1/modules"

# Default values used when a variable has no default and we must invent one
_TYPE_DEFAULTS: dict[str, str] = {
    "string":      '""',
    "number":      "0",
    "bool":        "false",
    "list":        "[]",
    "map":         "{}",
    "list(string)": "[]",
    "list(number)": "[]",
    "map(string)":  "{}",
    "map(any)":     "{}",
    "set(string)":  "[]",
    "any":          "null",
}

# Known providers we recognise from common module sources
_SOURCE_PROVIDER_MAP = {
    "aws": "hashicorp/aws",
    "google": "hashicorp/google",
    "azurerm": "hashicorp/azurerm",
    "kubernetes": "hashicorp/kubernetes",
    "helm": "hashicorp/helm",
    "vault": "hashicorp/vault",
    "github": "integrations/github",
    "datadog": "datadog/datadog",
    "cloudflare": "cloudflare/cloudflare",
}

_DEFAULT_PROVIDER_VERSIONS: dict[str, str] = {
    "hashicorp/aws":        "~> 5.0",
    "hashicorp/google":     "~> 5.0",
    "hashicorp/azurerm":    "~> 3.0",
    "hashicorp/kubernetes": "~> 2.0",
    "hashicorp/helm":       "~> 2.0",
    "hashicorp/vault":      "~> 3.0",
    "integrations/github":  "~> 5.0",
    "datadog/datadog":      "~> 3.0",
    "cloudflare/cloudflare":"~> 4.0",
}


class Variable:
    __slots__ = ("name", "type", "description", "default", "sensitive", "required")

    def __init__(
        self,
        name: str,
        type: str = "string",
        description: str = "",
        default=None,
        sensitive: bool = False,
    ):
        self.name = name
        self.type = type.strip() if type else "string"
        self.description = description.strip() if description else ""
        self.default = default          # None means "no default" (required)
        self.sensitive = sensitive
        self.required = default is None

    def rendered_default(self) -> str:
        """Return the HCL-ready default value string."""
        if self.default is None:
            # Invent a safe default so variables.tf is always valid
            return _TYPE_DEFAULTS.get(self.type.lower(), '""')
        if isinstance(self.default, bool):
            return "true" if self.default else "false"
        if isinstance(self.default, (int, float)):
            return str(self.default)
        if isinstance(self.default, str):
            if self.default in ("true", "false") or re.match(r"^[\[\{]", self.default):
                return self.default
            return f'"{self.default}"'
        if isinstance(self.default, list):
            items = ", ".join(f'"{i}"' if isinstance(i, str) else str(i) for i in self.default)
            return f"[{items}]"
        if isinstance(self.default, dict):
            if not self.default:
                return "{}"
            pairs = "\n".join(
                f'    {k} = "{v}"' if isinstance(v, str) else f"    {k} = {v}"
                for k, v in self.default.items()
            )
            return "{\n" + pairs + "\n  }"
        return '""'


class ModuleSpec:
    """Parsed representation of a single module entry."""

    def __init__(self, raw):
        self.source: str = raw.source
        self.version: str | None = raw.version
        self.alias: str = raw.alias or self._derive_alias()
        self.inputs: dict = raw.inputs or {}
        self.variables: list[Variable] = []
        self.providers_needed: list[str] = []

    def _derive_alias(self) -> str:
        # Strip git/https prefix, take last path segment, sanitise
        src = self.source
        src = re.sub(r"^(git::|https?://|github\.com/|git@[^:]+:)", "", src)
        src = re.sub(r"\.git$", "", src)
        parts = [p for p in re.split(r"[/\\]", src) if p]
        base = parts[-1] if parts else "module"
        # Remove version suffixes like //modules/vpc
        base = base.split("//")[0]
        # Sanitise to valid HCL identifier
        base = re.sub(r"[^a-zA-Z0-9_]", "_", base)
        base = re.sub(r"_+", "_", base).strip("_")
        return base or "module"


class TerraformResolver:
    def __init__(self, request: "ResolveRequest"):
        self.modules: list[ModuleSpec] = [ModuleSpec(m) for m in request.modules]
        self.tf_version: str = request.terraform_version or "~> 1.5"
        self.backend: str | None = request.backend
        self.provider_overrides: dict = request.provider_overrides or {}

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def resolve(self) -> dict:
        self._ensure_unique_aliases()

        for spec in self.modules:
            self._fetch_module_meta(spec)

        all_vars = self._collect_variables()
        providers = self._collect_providers()

        main_tf = self._render_main(providers)
        variables_tf = self._render_variables(all_vars)

        summary = {
            "modules_resolved": len(self.modules),
            "variables_extracted": len(all_vars),
            "providers_detected": list(providers.keys()),
        }

        return {
            "main_tf": main_tf,
            "variables_tf": variables_tf,
            "summary": summary,
        }

    # ------------------------------------------------------------------
    # Alias de-duplication
    # ------------------------------------------------------------------

    def _ensure_unique_aliases(self):
        seen: dict[str, int] = {}
        for spec in self.modules:
            base = spec.alias
            if base in seen:
                seen[base] += 1
                spec.alias = f"{base}_{seen[base]}"
            else:
                seen[base] = 0

    # ------------------------------------------------------------------
    # Registry / source metadata fetch
    # ------------------------------------------------------------------

    def _fetch_module_meta(self, spec: ModuleSpec):
        """Try to pull variable info from the Terraform Registry."""
        registry_id = self._registry_id(spec.source)
        if registry_id:
            self._fetch_from_registry(spec, registry_id)
        else:
            # Non-registry source: infer provider only
            self._infer_from_source(spec)

    def _registry_id(self, source: str) -> str | None:
        """
        Detect Terraform Registry format: <namespace>/<module>/<provider>
        or registry.terraform.io/<namespace>/<module>/<provider>
        """
        source = re.sub(r"^registry\.terraform\.io/", "", source)
        parts = source.split("/")
        if len(parts) == 3 and not any(p in source for p in ("github.com", "git::", "bitbucket")):
            return "/".join(parts)
        return None

    def _fetch_from_registry(self, spec: ModuleSpec, registry_id: str):
        namespace, module_name, provider = registry_id.split("/")
        version_path = spec.version.lstrip("~>=! ") if spec.version else None

        # Determine the version to query
        if not version_path:
            version_path = self._latest_registry_version(namespace, module_name, provider)

        if not version_path:
            logger.warning("Could not determine version for %s, using variable stubs", registry_id)
            self._infer_from_source(spec)
            return

        url = f"{REGISTRY_API}/{namespace}/{module_name}/{provider}/{version_path}"
        try:
            with httpx.Client(timeout=10) as client:
                resp = client.get(url)
                resp.raise_for_status()
                data = resp.json()
        except Exception as exc:
            logger.warning("Registry fetch failed for %s: %s", registry_id, exc)
            self._infer_from_source(spec)
            return

        root = data.get("root", {})
        raw_vars = root.get("inputs", [])  # list of {name, type, description, default, required}

        spec.providers_needed.append(provider)

        for rv in raw_vars:
            name = rv.get("name", "")
            if not name:
                continue
            default_raw = rv.get("default")  # None = required, else parsed value
            # Registry returns defaults as JSON strings sometimes
            if isinstance(default_raw, str) and default_raw in ("null", ""):
                default_raw = None

            spec.variables.append(Variable(
                name=name,
                type=rv.get("type", "string"),
                description=rv.get("description", ""),
                default=default_raw,
                sensitive="sensitive" in rv.get("description", "").lower() or rv.get("sensitive", False),
            ))

    def _latest_registry_version(self, namespace: str, module: str, provider: str) -> str | None:
        url = f"{REGISTRY_API}/{namespace}/{module}/{provider}/versions"
        try:
            with httpx.Client(timeout=10) as client:
                resp = client.get(url)
                resp.raise_for_status()
                data = resp.json()
            versions = data.get("modules", [{}])[0].get("versions", [])
            if versions:
                return versions[0].get("version")
        except Exception as exc:
            logger.warning("Could not fetch versions: %s", exc)
        return None

    def _infer_from_source(self, spec: ModuleSpec):
        """Heuristically determine provider from source string."""
        src_lower = spec.source.lower()
        for keyword, provider in _SOURCE_PROVIDER_MAP.items():
            if keyword in src_lower:
                short = provider.split("/")[-1]
                if short not in spec.providers_needed:
                    spec.providers_needed.append(short)
                break

    # ------------------------------------------------------------------
    # Variable collection & deduplication
    # ------------------------------------------------------------------

    def _collect_variables(self) -> list[Variable]:
        seen: dict[str, Variable] = {}
        for spec in self.modules:
            for var in spec.variables:
                if var.name not in seen:
                    seen[var.name] = var
                else:
                    # Merge: if one is required, mark required; keep first description
                    existing = seen[var.name]
                    if var.required:
                        existing.required = True
                        existing.default = None
        return list(seen.values())

    # ------------------------------------------------------------------
    # Provider collection
    # ------------------------------------------------------------------

    def _collect_providers(self) -> dict[str, str]:
        providers: dict[str, str] = {}
        for spec in self.modules:
            for p in spec.providers_needed:
                fqn = _SOURCE_PROVIDER_MAP.get(p, f"hashicorp/{p}")
                if fqn not in providers:
                    version = (
                        self.provider_overrides.get(p)
                        or self.provider_overrides.get(fqn)
                        or _DEFAULT_PROVIDER_VERSIONS.get(fqn, "~> 1.0")
                    )
                    providers[fqn] = version
        return providers

    # ------------------------------------------------------------------
    # HCL rendering
    # ------------------------------------------------------------------

    def _render_main(self, providers: dict[str, str]) -> str:
        lines: list[str] = []

        # ── terraform{} block ─────────────────────────────────────────
        lines += [
            "terraform {",
            f'  required_version = "{self.tf_version}"',
        ]

        if providers:
            lines += [
                "",
                "  required_providers {",
            ]
            for fqn, version in providers.items():
                short = fqn.split("/")[-1]
                lines += [
                    f"    {short} = {{",
                    f'      source  = "{fqn}"',
                    f'      version = "{version}"',
                    "    }",
                ]
            lines.append("  }")

        if self.backend:
            lines += [
                "",
                f"  backend \"{self.backend}\" {{",
                "    # TODO: configure backend settings",
                "  }",
            ]

        lines += ["}", ""]

        # ── provider blocks ───────────────────────────────────────────
        for fqn in providers:
            short = fqn.split("/")[-1]
            lines += [
                f"provider \"{short}\" {{",
                "  # TODO: configure provider",
                "}",
                "",
            ]

        # ── module blocks ─────────────────────────────────────────────
        for spec in self.modules:
            lines.append(f'module "{spec.alias}" {{')
            lines.append(f'  source = "{spec.source}"')
            if spec.version:
                lines.append(f'  version = "{spec.version}"')

            # Inputs: use override if provided, else reference the variable
            for var in spec.variables:
                if var.name in spec.inputs:
                    val = spec.inputs[var.name]
                    if isinstance(val, str):
                        rendered = f'"{val}"'
                    elif isinstance(val, bool):
                        rendered = "true" if val else "false"
                    else:
                        rendered = str(val)
                else:
                    rendered = f"var.{var.name}"
                lines.append(f"  {var.name:<30} = {rendered}")

            # Any extra inputs not in variables (passed directly)
            for k, v in spec.inputs.items():
                if not any(var.name == k for var in spec.variables):
                    if isinstance(v, str):
                        rendered = f'"{v}"'
                    elif isinstance(v, bool):
                        rendered = "true" if v else "false"
                    else:
                        rendered = str(v)
                    lines.append(f"  {k:<30} = {rendered}")

            lines += ["}", ""]

        return "\n".join(lines)

    def _render_variables(self, variables: list[Variable]) -> str:
        if not variables:
            return "# No variables extracted from the provided modules.\n"

        lines: list[str] = []

        for var in sorted(variables, key=lambda v: v.name):
            lines.append(f'variable "{var.name}" {{')

            if var.description:
                escaped = var.description.replace('"', '\\"')
                lines.append(f'  description = "{escaped}"')
            else:
                lines.append(f'  description = "Value for {var.name}"')

            lines.append(f"  type        = {var.type}")

            default_val = var.rendered_default()
            if "\n" in default_val:
                lines.append(f"  default     = {default_val}")
            else:
                lines.append(f"  default     = {default_val}")

            if var.sensitive:
                lines.append("  sensitive   = true")

            if var.required:
                lines.append("")
                lines.append("  # NOTE: This variable had no upstream default.")
                lines.append("  #       A safe placeholder has been set — override before applying.")

            lines += ["}", ""]

        return "\n".join(lines)
