"""
Tests for the Terraform Resolver.
Run with: pytest tests/ -v
"""

import pytest
from unittest.mock import patch, MagicMock

from app.resolver import TerraformResolver, ModuleSpec, Variable
from app.main import ResolveRequest, ModuleInput


# ── Helpers ───────────────────────────────────────────────────────────────────

def make_request(**kwargs) -> ResolveRequest:
    defaults = dict(
        modules=[ModuleInput(source="terraform-aws-modules/vpc/aws", version="~> 5.0")],
        terraform_version="~> 1.6",
        backend=None,
        provider_overrides={},
    )
    defaults.update(kwargs)
    return ResolveRequest(**defaults)


# ── Variable rendering ────────────────────────────────────────────────────────

class TestVariableRendering:
    def test_string_default(self):
        v = Variable("name", "string", default="us-east-1")
        assert v.rendered_default() == '"us-east-1"'

    def test_bool_true(self):
        v = Variable("enabled", "bool", default=True)
        assert v.rendered_default() == "true"

    def test_bool_false(self):
        v = Variable("enabled", "bool", default=False)
        assert v.rendered_default() == "false"

    def test_number_default(self):
        v = Variable("count", "number", default=3)
        assert v.rendered_default() == "3"

    def test_list_default(self):
        v = Variable("azs", "list(string)", default=["a", "b"])
        assert v.rendered_default() == '["a", "b"]'

    def test_no_default_string(self):
        v = Variable("name", "string", default=None)
        assert v.rendered_default() == '""'

    def test_no_default_bool(self):
        v = Variable("flag", "bool", default=None)
        assert v.rendered_default() == "false"

    def test_no_default_list(self):
        v = Variable("items", "list(string)", default=None)
        assert v.rendered_default() == "[]"

    def test_required_flag(self):
        v = Variable("required_var", "string", default=None)
        assert v.required is True

    def test_not_required_with_default(self):
        v = Variable("optional_var", "string", default="foo")
        assert v.required is False


# ── Alias derivation ──────────────────────────────────────────────────────────

class TestAliasDerivation:
    def _spec(self, source, alias=None):
        return ModuleSpec(ModuleInput(source=source, alias=alias))

    def test_registry_source(self):
        s = self._spec("terraform-aws-modules/vpc/aws")
        assert s.alias == "aws"

    def test_github_source(self):
        s = self._spec("github.com/myorg/terraform-vpc-module.git")
        assert "vpc" in s.alias or "module" in s.alias

    def test_explicit_alias(self):
        s = self._spec("terraform-aws-modules/vpc/aws", alias="my_vpc")
        assert s.alias == "my_vpc"

    def test_sanitised_alias(self):
        s = self._spec("some/module-with-dashes/provider")
        assert "-" not in s.alias


# ── Unique alias enforcement ──────────────────────────────────────────────────

class TestUniqueAliases:
    def test_duplicate_aliases_resolved(self):
        req = make_request(modules=[
            ModuleInput(source="terraform-aws-modules/vpc/aws"),
            ModuleInput(source="terraform-aws-modules/vpc/aws"),
        ])
        resolver = TerraformResolver(req)
        resolver._ensure_unique_aliases()
        aliases = [s.alias for s in resolver.modules]
        assert len(set(aliases)) == len(aliases)


# ── Registry ID detection ─────────────────────────────────────────────────────

class TestRegistryIdDetection:
    def _resolver(self):
        return TerraformResolver(make_request())

    def test_valid_registry_id(self):
        r = self._resolver()
        assert r._registry_id("terraform-aws-modules/vpc/aws") == "terraform-aws-modules/vpc/aws"

    def test_with_registry_prefix(self):
        r = self._resolver()
        result = r._registry_id("registry.terraform.io/terraform-aws-modules/vpc/aws")
        assert result == "terraform-aws-modules/vpc/aws"

    def test_github_not_registry(self):
        r = self._resolver()
        assert r._registry_id("github.com/myorg/module") is None

    def test_git_not_registry(self):
        r = self._resolver()
        assert r._registry_id("git::https://example.com/module.git") is None


# ── Provider inference ────────────────────────────────────────────────────────

class TestProviderInference:
    def test_aws_inferred(self):
        req = make_request(modules=[ModuleInput(source="github.com/myorg/terraform-aws-vpc")])
        resolver = TerraformResolver(req)
        spec = resolver.modules[0]
        resolver._infer_from_source(spec)
        assert "aws" in spec.providers_needed

    def test_google_inferred(self):
        req = make_request(modules=[ModuleInput(source="github.com/myorg/google-gke-module")])
        resolver = TerraformResolver(req)
        spec = resolver.modules[0]
        resolver._infer_from_source(spec)
        assert "google" in spec.providers_needed


# ── HCL rendering ─────────────────────────────────────────────────────────────

class TestHCLRendering:
    def _resolver_with_vars(self, variables: list[Variable]) -> TerraformResolver:
        req = make_request()
        resolver = TerraformResolver(req)
        resolver.modules[0].variables = variables
        return resolver

    def test_main_tf_contains_module_block(self):
        resolver = self._resolver_with_vars([])
        main = resolver._render_main({})
        assert 'module "' in main
        assert "terraform {" in main

    def test_main_tf_with_backend(self):
        req = make_request(backend="s3")
        resolver = TerraformResolver(req)
        main = resolver._render_main({})
        assert 'backend "s3"' in main

    def test_variables_tf_contains_variable(self):
        resolver = self._resolver_with_vars([
            Variable("vpc_cidr", "string", description="VPC CIDR block", default="10.0.0.0/16")
        ])
        vtf = resolver._render_variables(resolver.modules[0].variables)
        assert 'variable "vpc_cidr"' in vtf
        assert '"10.0.0.0/16"' in vtf

    def test_variables_tf_empty(self):
        resolver = self._resolver_with_vars([])
        vtf = resolver._render_variables([])
        assert "No variables" in vtf

    def test_variables_sorted_alphabetically(self):
        resolver = self._resolver_with_vars([
            Variable("zzz", "string", default="a"),
            Variable("aaa", "string", default="b"),
        ])
        vtf = resolver._render_variables(resolver.modules[0].variables)
        assert vtf.index("aaa") < vtf.index("zzz")

    def test_sensitive_variable(self):
        resolver = self._resolver_with_vars([
            Variable("db_password", "string", description="sensitive password", default=None, sensitive=True)
        ])
        vtf = resolver._render_variables(resolver.modules[0].variables)
        assert "sensitive   = true" in vtf

    def test_required_variable_comment(self):
        resolver = self._resolver_with_vars([
            Variable("must_set", "string", default=None)
        ])
        vtf = resolver._render_variables(resolver.modules[0].variables)
        assert "NOTE: This variable had no upstream default" in vtf

    def test_provider_block_rendered(self):
        resolver = self._resolver_with_vars([])
        main = resolver._render_main({"hashicorp/aws": "~> 5.0"})
        assert 'provider "aws"' in main
        assert 'source  = "hashicorp/aws"' in main

    def test_module_input_override(self):
        req = make_request(modules=[
            ModuleInput(
                source="terraform-aws-modules/vpc/aws",
                inputs={"vpc_cidr": "192.168.0.0/16"}
            )
        ])
        resolver = TerraformResolver(req)
        resolver.modules[0].variables = [Variable("vpc_cidr", "string", default="10.0.0.0/16")]
        main = resolver._render_main({})
        assert '"192.168.0.0/16"' in main


# ── Variable deduplication ────────────────────────────────────────────────────

class TestVariableDeduplication:
    def test_deduplication_keeps_required(self):
        req = make_request(modules=[
            ModuleInput(source="terraform-aws-modules/vpc/aws"),
            ModuleInput(source="terraform-aws-modules/eks/aws"),
        ])
        resolver = TerraformResolver(req)
        resolver.modules[0].variables = [Variable("cluster_name", "string", default="my-cluster")]
        resolver.modules[1].variables = [Variable("cluster_name", "string", default=None)]
        merged = resolver._collect_variables()
        assert len(merged) == 1
        assert merged[0].required is True


# ── End-to-end (mocked registry) ─────────────────────────────────────────────

class TestEndToEnd:
    def test_full_resolve_no_registry(self):
        """Non-registry source should resolve without errors."""
        req = make_request(modules=[
            ModuleInput(source="github.com/myorg/terraform-aws-vpc.git", version=None)
        ])
        resolver = TerraformResolver(req)
        result = resolver.resolve()
        assert "main_tf" in result
        assert "variables_tf" in result
        assert result["summary"]["modules_resolved"] == 1

    @patch("app.resolver.httpx.Client")
    def test_full_resolve_with_mocked_registry(self, mock_client_cls):
        mock_resp_versions = MagicMock()
        mock_resp_versions.json.return_value = {
            "modules": [{"versions": [{"version": "5.1.2"}]}]
        }
        mock_resp_versions.raise_for_status = MagicMock()

        mock_resp_module = MagicMock()
        mock_resp_module.json.return_value = {
            "root": {
                "inputs": [
                    {"name": "vpc_cidr", "type": "string", "description": "VPC CIDR", "default": "10.0.0.0/16"},
                    {"name": "enable_dns", "type": "bool", "description": "Enable DNS", "default": None},
                ]
            }
        }
        mock_resp_module.raise_for_status = MagicMock()

        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.get.side_effect = [mock_resp_versions, mock_resp_module]
        mock_client_cls.return_value = mock_client

        req = make_request(modules=[
            ModuleInput(source="terraform-aws-modules/vpc/aws", version=None)
        ])
        resolver = TerraformResolver(req)
        result = resolver.resolve()

        assert "vpc_cidr" in result["variables_tf"]
        assert "enable_dns" in result["variables_tf"]
        assert 'module "aws"' in result["main_tf"]
        assert result["summary"]["variables_extracted"] == 2
