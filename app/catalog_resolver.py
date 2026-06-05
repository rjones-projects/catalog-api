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

import os
import re
import logging
from dataclasses import dataclass, field
from typing import Any, Optional

import httpx

from app.file_client import FileServiceClient, get_client

logger = logging.getLogger(__name__)

# Repository coordinates — all configurable via environment variables.
CATALOG_OWNER        = os.getenv("CATALOG_OWNER",        "rjones-projects")
CATALOG_REPO         = os.getenv("CATALOG_REPO",         "catalog")
CATALOG_MAPPING_FILE = os.getenv("CATALOG_MAPPING_FILE", "gcp-mapping.yaml")
MODULES_OWNER        = os.getenv("MODULES_OWNER",        "rjones-projects")
MODULES_REPO         = os.getenv("MODULES_REPO",         "gcp_terraform-modules")
MODULES_SUBDIR       = os.getenv("MODULES_SUBDIR",       "terraform/modules")

# Used in generated main.tf module source URLs (not for HTTP calls).
GCP_MODULES_SOURCE = f"github.com/{MODULES_OWNER}/{MODULES_REPO}"

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

    def resolve(
        self,
        overrides_map: Optional[dict[str, Any]] = None,
        tfvars_preamble: Optional[dict[str, Any]] = None,
    ) -> dict:
        """
        Resolve building blocks into main.tf + variables.tf.
        If overrides_map is provided (block → override dict or []), also produces terraform.tfvars.
        """
        mapping = self._fetch_mapping()

        block_names = list(overrides_map.keys()) if overrides_map is not None else self.building_blocks
        modules = self._resolve_modules(mapping, block_names)
        modules_by_name = {m.name: m for m in modules}

        all_vars = self._collect_variables(modules)
        main_tf = self._render_main(modules)
        variables_tf = self._render_variables(all_vars)
        terraform_tfvars = (
            self._render_tfvars(overrides_map, mapping, modules_by_name, preamble=tfvars_preamble)
            if overrides_map
            else ""
        )

        unresolved = [b for b in block_names if b not in mapping]
        return {
            "main_tf": main_tf,
            "variables_tf": variables_tf,
            "terraform_tfvars": terraform_tfvars,
            "summary": {
                "building_blocks_requested": block_names,
                "building_blocks_resolved": [b for b in block_names if b in mapping],
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
        """Fetch the Backstage catalog YAML via the file service and return building_block -> [module_names]."""
        try:
            docs = get_client().proxy_catalog_file(
                CATALOG_OWNER, CATALOG_REPO, CATALOG_MAPPING_FILE
            )
        except Exception as exc:
            raise RuntimeError(f"Failed to fetch catalog mapping: {exc}") from exc

        mapping: dict[str, list[str]] = {}
        for doc in docs:
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

    def _resolve_modules(
        self,
        mapping: dict[str, list[str]],
        block_names: Optional[list[str]] = None,
    ) -> list[ResolvedModule]:
        seen: dict[str, ResolvedModule] = {}
        for block in (block_names if block_names is not None else self.building_blocks):
            for module_name in mapping.get(block, []):
                if module_name in seen:
                    continue
                source = f"{GCP_MODULES_SOURCE}//{MODULES_SUBDIR}/{module_name}?ref={self.modules_ref}"
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
        path = f"{MODULES_SUBDIR}/{module_name}/variables.tf"
        try:
            content = get_client().get_text_file(MODULES_OWNER, MODULES_REPO, path, ref=self.modules_ref)
            return self._parse_variables_tf(content), None
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                return [], f"variables.tf not found for module '{module_name}'"
            logger.warning("Failed to fetch variables.tf for %s: %s", module_name, exc)
            return [], str(exc)
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
            '      version = ">= 7.17.0"',
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

    # ------------------------------------------------------------------
    # terraform.tfvars generation
    # ------------------------------------------------------------------

    def _render_tfvars(
        self,
        overrides_map: dict[str, Any],
        mapping: dict[str, list[str]],
        modules_by_name: dict[str, ResolvedModule],
        preamble: Optional[dict[str, Any]] = None,
    ) -> str:
        """
        For each building block that carries override values, route each key to the
        correct any-typed config variable (e.g. 'tier' under 'sql' becomes
        cloud_sql = { tier = "premium" }) and render a terraform.tfvars block.

        Top-level (unmatched) keys are deduplicated across building blocks — if the
        same key appears in multiple blocks with the same value it is written once;
        if values conflict, the later value wins and a warning comment is emitted.

        Optional preamble dict is written first (e.g. project_id, region).
        """
        lines: list[str] = []
        # Tracks top-level var names already written to avoid duplicates.
        written_top_level: dict[str, Any] = {}

        # Write preamble vars (project_id, region, etc.) before block sections.
        if preamble:
            for key, value in preamble.items():
                lines.append(f"{key} = {self._render_hcl_value(value)}")
                written_top_level[key] = value
            lines.append("")

        for block_name, overrides in overrides_map.items():
            if isinstance(overrides, list):
                overrides = {}
            if not overrides:
                continue
            if block_name not in mapping:
                lines.append(f"# WARNING: building block '{block_name}' not found in catalog — skipped.")
                lines.append("")
                continue

            module_names = mapping[block_name]
            lines.append(f"# {block_name} (modules: {', '.join(module_names) or 'none'})")

            # Route each key to the matching any-typed config variable across modules.
            var_assignments: dict[str, dict[str, Any]] = {}
            unmatched: dict[str, Any] = dict(overrides)

            for module_name in module_names:
                mod = modules_by_name.get(module_name)
                if not mod:
                    continue
                still_unmatched: dict[str, Any] = {}
                for key, value in unmatched.items():
                    config_var = self._find_config_var_for_key(key, mod)
                    if config_var:
                        var_assignments.setdefault(config_var, {})[key] = value
                    else:
                        still_unmatched[key] = value
                unmatched = still_unmatched

            for var_name, kv in var_assignments.items():
                lines.append(f"{var_name} = {{")
                for k, v in kv.items():
                    lines.append(f"  {k} = {self._render_hcl_value(v, indent=1)}")
                lines.append("}")

            for key, value in unmatched.items():
                if key in written_top_level:
                    if written_top_level[key] != value:
                        lines.append(
                            f"# WARNING: '{key}' conflict — "
                            f"was {self._render_hcl_value(written_top_level[key])}, "
                            f"now {self._render_hcl_value(value)}."
                        )
                        lines.append(f"{key} = {self._render_hcl_value(value)}")
                        written_top_level[key] = value
                    # Same value already written — skip silently.
                else:
                    lines.append(f"{key} = {self._render_hcl_value(value)}")
                    written_top_level[key] = value

            lines.append("")

        return "\n".join(lines)

    def _find_config_var_for_key(self, key: str, mod: ResolvedModule) -> Optional[str]:
        """
        Find the any-typed config variable whose _default object contains 'key'
        as a direct child field.
        e.g. key='machine_type', mod=gke_standard_cluster  →  'gke_standard_cluster'
        """
        for var in mod.variables:
            if not (var.name.endswith("_default") and "object(" in var.type_hcl):
                continue
            if key in self._extract_object_field_names(var.type_hcl):
                config_var_name = var.name[: -len("_default")]
                if any(v.name == config_var_name for v in mod.variables):
                    return config_var_name
        return None

    @staticmethod
    def _extract_object_field_names(type_hcl: str) -> set[str]:
        """Extract direct (first-level) field names from an object({...}) HCL type."""
        m = re.match(r"object\s*\(\s*\{", type_hcl)
        if not m:
            return set()
        i, depth, chars = m.end(), 1, []
        while i < len(type_hcl) and depth > 0:
            c = type_hcl[i]
            if c in ("{", "("):
                depth += 1
            elif c in ("}", ")"):
                depth -= 1
                if depth == 0:
                    break
            if depth == 1:
                chars.append(c)
            i += 1
        return set(re.findall(r"^\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*=", "".join(chars), re.MULTILINE))

    @staticmethod
    def _render_hcl_value(value: Any, indent: int = 0) -> str:
        """Render a Python value as a valid HCL literal."""
        pad = "  " * indent
        inner = "  " * (indent + 1)
        if value is None:
            return "null"
        if isinstance(value, bool):
            return "true" if value else "false"
        if isinstance(value, (int, float)):
            return str(value)
        if isinstance(value, str):
            return f'"{value}"'
        if isinstance(value, list):
            if not value:
                return "[]"
            if all(not isinstance(v, (list, dict)) for v in value):
                return "[" + ", ".join(CatalogResolver._render_hcl_value(v) for v in value) + "]"
            rendered = [CatalogResolver._render_hcl_value(v, indent + 1) for v in value]
            return "[\n" + ",\n".join(f"{inner}{r}" for r in rendered) + f"\n{pad}]"
        if isinstance(value, dict):
            if not value:
                return "{}"
            body = "\n".join(
                f"{inner}{k} = {CatalogResolver._render_hcl_value(v, indent + 1)}"
                for k, v in value.items()
            )
            return "{\n" + body + f"\n{pad}}}"
        return f'"{value}"'
