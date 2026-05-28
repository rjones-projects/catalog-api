"""
CatalogResolver

Fetches the building-block → module mapping from the catalog YAML at:
  https://raw.githubusercontent.com/rjones-projects/catalog/main/gcp-mapping.yaml

For each resolved module, pulls variables.tf from:
  https://raw.githubusercontent.com/rjones-projects/gcp_terraform-modules/main/<module>/variables.tf

Produces:
  - main.tf      : terraform{} block, google provider stub, module blocks
  - variables.tf : merged variables with defaults / required-variable placeholders
"""

from __future__ import annotations

import re
import logging
from dataclasses import dataclass, field
from typing import Optional

import os

import httpx
import yaml

logger = logging.getLogger(__name__)

CATALOG_YAML_URL = "https://raw.githubusercontent.com/rjones-projects/catalog/main/gcp-mapping.yaml"
GCP_MODULES_RAW = "https://raw.githubusercontent.com/rjones-projects/gcp_terraform-modules/main/terraform/modules"
GCP_MODULES_SOURCE = "github.com/rjones-projects/gcp_terraform-modules"
GCP_MODULES_SUBDIR = "terraform/modules"


def _github_headers() -> dict[str, str]:
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        return {"Authorization": f"token {token}"}
    return {}

_TYPE_DEFAULTS: dict[str, str] = {
    "string":       '""',
    "number":       "0",
    "bool":         "false",
    "list(string)": "[]",
    "list(number)": "[]",
    "map(string)":  "{}",
    "map(any)":     "{}",
    "set(string)":  "[]",
    "any":          "null",
}


@dataclass
class CatalogVariable:
    name: str
    type_hcl: str = "string"
    description: str = ""
    default_hcl: Optional[str] = None  # None = required (no upstream default)
    sensitive: bool = False

    @property
    def required(self) -> bool:
        return self.default_hcl is None

    def rendered_default(self) -> str:
        if self.default_hcl is None:
            return _TYPE_DEFAULTS.get(self.type_hcl.lower(), '""')
        return self.default_hcl


@dataclass
class ResolvedModule:
    name: str
    source: str
    variables: list[CatalogVariable] = field(default_factory=list)
    fetch_error: Optional[str] = None


class CatalogResolver:
    def __init__(
        self,
        building_blocks: list[str],
        terraform_version: str = "~> 1.9",
        backend: Optional[str] = None,
        modules_ref: str = "main",
    ):
        self.building_blocks = building_blocks
        self.terraform_version = terraform_version
        self.backend = backend
        self.modules_ref = modules_ref

    def resolve(self) -> dict:
        mapping = self._fetch_mapping()
        modules = self._resolve_modules(mapping)
        all_vars = self._collect_variables(modules)
        main_tf = self._render_main(modules)
        variables_tf = self._render_variables(all_vars)

        unresolved = [b for b in self.building_blocks if b not in mapping]
        return {
            "main_tf": main_tf,
            "variables_tf": variables_tf,
            "summary": {
                "building_blocks_requested": self.building_blocks,
                "building_blocks_resolved": [b for b in self.building_blocks if b in mapping],
                "building_blocks_unresolved": unresolved,
                "modules_resolved": [m.name for m in modules],
                "variables_extracted": len(all_vars),
                "modules_with_fetch_errors": [m.name for m in modules if m.fetch_error],
            },
        }

    # ------------------------------------------------------------------
    # Catalog mapping fetch
    # ------------------------------------------------------------------

    def _fetch_mapping(self) -> dict[str, list[str]]:
        """Fetch the multi-document Backstage YAML and return building_block -> [module_names]."""
        try:
            with httpx.Client(timeout=15) as client:
                resp = client.get(CATALOG_YAML_URL, headers=_github_headers())
                resp.raise_for_status()
                content = resp.text
        except Exception as exc:
            raise RuntimeError(f"Failed to fetch catalog mapping: {exc}") from exc

        mapping: dict[str, list[str]] = {}
        for doc in yaml.safe_load_all(content):
            if not isinstance(doc, dict) or doc.get("kind") != "Component":
                continue
            name = (doc.get("metadata") or {}).get("name", "")
            if not name:
                continue
            depends_on = (doc.get("spec") or {}).get("dependsOn") or []
            mapping[name] = self._parse_depends_on(depends_on)

        return mapping

    @staticmethod
    def _parse_depends_on(depends_on: list) -> list[str]:
        modules = []
        for dep in depends_on:
            if isinstance(dep, dict):
                # "Component: module_name" (space after colon) is parsed by YAML
                # as {"Component": "module_name"} — extract the value directly.
                name = str(next(iter(dep.values()), "")).strip()
            else:
                # "Component:module_name" (no space) stays as a plain string.
                name = re.sub(r"^Component:\s*", "", str(dep)).strip()
            if name and re.match(r"^[a-zA-Z][a-zA-Z0-9_]*$", name):
                modules.append(name)
        return modules

    # ------------------------------------------------------------------
    # Module resolution
    # ------------------------------------------------------------------

    def _resolve_modules(self, mapping: dict[str, list[str]]) -> list[ResolvedModule]:
        seen: dict[str, ResolvedModule] = {}
        for block in self.building_blocks:
            for module_name in mapping.get(block, []):
                if module_name in seen:
                    continue
                source = f"{GCP_MODULES_SOURCE}//{GCP_MODULES_SUBDIR}/{module_name}?ref={self.modules_ref}"
                variables, error = self._fetch_module_variables(module_name)
                seen[module_name] = ResolvedModule(
                    name=module_name,
                    source=source,
                    variables=variables,
                    fetch_error=error,
                )
        return list(seen.values())

    # ------------------------------------------------------------------
    # Variable fetch & parse
    # ------------------------------------------------------------------

    def _fetch_module_variables(self, module_name: str) -> tuple[list[CatalogVariable], Optional[str]]:
        url = f"{GCP_MODULES_RAW}/{module_name}/variables.tf"
        try:
            with httpx.Client(timeout=10) as client:
                resp = client.get(url, headers=_github_headers())
                if resp.status_code == 404:
                    return [], f"variables.tf not found for module '{module_name}'"
                resp.raise_for_status()
                return self._parse_variables_tf(resp.text), None
        except Exception as exc:
            logger.warning("Failed to fetch variables.tf for %s: %s", module_name, exc)
            return [], str(exc)

    def _parse_variables_tf(self, content: str) -> list[CatalogVariable]:
        return [
            CatalogVariable(
                name=name,
                type_hcl=fields.get("type", "string"),
                description=fields.get("description", ""),
                default_hcl=fields.get("default"),
                sensitive=fields.get("sensitive", False),
            )
            for name, fields in (
                (name, self._parse_variable_body(body))
                for name, body in self._extract_variable_blocks(content)
            )
        ]

    @staticmethod
    def _extract_variable_blocks(content: str) -> list[tuple[str, str]]:
        results = []
        i = 0
        while i < len(content):
            m = re.search(r'variable\s+"([^"]+)"\s*\{', content[i:])
            if not m:
                break
            var_name = m.group(1)
            start = i + m.end()
            depth, j = 1, start
            while j < len(content) and depth > 0:
                if content[j] == "{":
                    depth += 1
                elif content[j] == "}":
                    depth -= 1
                j += 1
            results.append((var_name, content[start : j - 1]))
            i = j
        return results

    @staticmethod
    def _parse_variable_body(body: str) -> dict:
        result: dict = {}

        m = re.search(r"^\s*type\s*=\s*", body, re.MULTILINE)
        if m:
            type_val = CatalogResolver._extract_hcl_value(body[m.end():])
            if type_val:
                result["type"] = type_val

        m = re.search(r'^\s*description\s*=\s*"([^"]*)"', body, re.MULTILINE)
        if m:
            result["description"] = m.group(1)

        m = re.search(r"^\s*sensitive\s*=\s*(true|false)", body, re.MULTILINE)
        if m:
            result["sensitive"] = m.group(1) == "true"

        m = re.search(r"^\s*default\s*=\s*", body, re.MULTILINE)
        if m:
            value = CatalogResolver._extract_hcl_value(body[m.end():])
            if value:
                result["default"] = value

        return result

    @staticmethod
    def _extract_hcl_value(s: str) -> str:
        """Extract a complete HCL value from the start of s, handling nested braces/parens."""
        s = s.lstrip(" \t")
        if not s:
            return ""
        first = s[0]
        # { or [ — depth-tracked brace/bracket pair
        if first in ("{", "["):
            close = "}" if first == "{" else "]"
            depth, i = 1, 1
            while i < len(s) and depth > 0:
                if s[i] == first:
                    depth += 1
                elif s[i] == close:
                    depth -= 1
                i += 1
            return s[:i]
        # Quoted string
        if first == '"':
            i = 1
            while i < len(s):
                if s[i] == "\\":
                    i += 2
                elif s[i] == '"':
                    return s[: i + 1]
                else:
                    i += 1
            return s
        # Identifier — may be a simple keyword (string, bool, any, null, true, false)
        # or a parameterised type like object({...}), list(...), optional(...)
        id_m = re.match(r"[a-zA-Z_][a-zA-Z0-9_]*", s)
        if id_m:
            rest = s[id_m.end():]
            paren_m = re.match(r"\s*\(", rest)
            if paren_m:
                # Depth-track parentheses so nested list(object({...})) etc. are captured whole
                paren_pos = id_m.end() + paren_m.end() - 1  # index of '(' in s
                depth, i = 1, paren_pos + 1
                while i < len(s) and depth > 0:
                    if s[i] == "(":
                        depth += 1
                    elif s[i] == ")":
                        depth -= 1
                    i += 1
                return s[:i]
            return id_m.group(0)
        # Number or anything else — read to end of line
        m = re.match(r"[^\n\r]+", s)
        return m.group(0).strip() if m else ""

    # ------------------------------------------------------------------
    # Variable collection & deduplication
    # ------------------------------------------------------------------

    def _collect_variables(self, modules: list[ResolvedModule]) -> list[CatalogVariable]:
        seen: dict[str, CatalogVariable] = {}
        for mod in modules:
            for var in mod.variables:
                if var.name not in seen:
                    seen[var.name] = var
                else:
                    # Promote to required if any module treats it as required
                    if var.required:
                        seen[var.name].default_hcl = None
        return list(seen.values())

    # ------------------------------------------------------------------
    # HCL rendering
    # ------------------------------------------------------------------

    def _render_main(self, modules: list[ResolvedModule]) -> str:
        lines: list[str] = [
            "terraform {",
            f'  required_version = "{self.terraform_version}"',
            "",
            "  required_providers {",
            "    google = {",
            '      source  = "hashicorp/google"',
            '      version = "~> 5.0"',
            "    }",
            "  }",
        ]

        if self.backend:
            lines += [
                "",
                f'  backend "{self.backend}" {{',
                "    # TODO: configure backend settings",
                "  }",
            ]

        lines += ["}", ""]

        lines += [
            'provider "google" {',
            "  project = var.project_id",
            "  region  = var.region",
            "}",
            "",
        ]

        for mod in modules:
            lines.append(f'module "{mod.name}" {{')
            lines.append(f'  source = "{mod.source}"')
            if mod.fetch_error:
                lines.append(f"  # WARNING: {mod.fetch_error}")
                lines.append("  # Add module inputs manually.")
            elif mod.variables:
                lines.append("")
                for var in mod.variables:
                    lines.append(f"  {var.name:<30} = var.{var.name}")
            lines += ["}", ""]

        return "\n".join(lines)

    def _render_variables(self, variables: list[CatalogVariable]) -> str:
        # Always include project_id and region; skip any module-defined duplicates of these
        top_level = {"project_id", "region"}
        preamble = [
            CatalogVariable(
                name="project_id",
                type_hcl="string",
                description="The GCP project ID.",
                default_hcl=None,
            ),
            CatalogVariable(
                name="region",
                type_hcl="string",
                description="The GCP region for resources.",
                default_hcl='"us-central1"',
            ),
        ]
        module_vars = sorted(
            (v for v in variables if v.name not in top_level),
            key=lambda v: v.name,
        )
        all_vars = preamble + module_vars

        lines: list[str] = []
        for var in all_vars:
            lines.append(f'variable "{var.name}" {{')
            desc = var.description or f"Value for {var.name}."
            lines.append(f'  description = "{desc.replace(chr(34), chr(92) + chr(34))}"')
            lines.append(f"  type        = {var.type_hcl}")
            lines.append(f"  default     = {var.rendered_default()}")
            if var.sensitive:
                lines.append("  sensitive   = true")
            if var.required:
                lines.append("")
                lines.append("  # NOTE: No upstream default — set this before applying.")
            lines += ["}", ""]

        return "\n".join(lines)
