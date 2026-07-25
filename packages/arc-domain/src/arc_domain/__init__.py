"""Durable research-domain construction for ARC."""

from .build import (
    DOMAIN_BUILD_HANDLER,
    DOMAIN_BUILD_SEMANTIC_SCHEMA_VERSION,
    DOMAIN_NETWORK_RENDER_RECIPE,
    DomainBuildHandler,
    DomainBuildRunner,
    domain_build_run_id,
)
from .contracts import (
    DOMAIN_BUILD_POLICY_SCHEMA_VERSION,
    DOMAIN_BUILD_REQUEST_SCHEMA_VERSION,
    DOMAIN_BUILD_RESULT_SCHEMA_VERSION,
    DomainBuildPolicy,
    DomainBuildRequest,
    DomainBuildResult,
    DomainBuildWarning,
    decode_domain_build_policy,
    decode_domain_build_request,
    decode_domain_build_result,
    decode_domain_build_warning,
    encode_domain_build_policy,
    encode_domain_build_request,
    encode_domain_build_result,
    encode_domain_build_warning,
)
from .paths import domain_id_for
from .package_view import (
    DomainPackageValidationError,
    DomainPackageView,
    DomainPaperPackView,
    DomainSummaryView,
    decode_domain_package,
    decode_domain_paper_pack,
    decode_domain_summary,
)
from .render import render_network_html
from .summary import mathematical_opportunities_validation_error

__all__ = [
    "DOMAIN_BUILD_POLICY_SCHEMA_VERSION",
    "DOMAIN_BUILD_REQUEST_SCHEMA_VERSION",
    "DOMAIN_BUILD_RESULT_SCHEMA_VERSION",
    "DOMAIN_BUILD_HANDLER",
    "DOMAIN_BUILD_SEMANTIC_SCHEMA_VERSION",
    "DOMAIN_NETWORK_RENDER_RECIPE",
    "DomainBuildHandler",
    "DomainBuildPolicy",
    "DomainBuildRequest",
    "DomainBuildResult",
    "DomainBuildRunner",
    "DomainBuildWarning",
    "DomainPackageValidationError",
    "DomainPackageView",
    "DomainPaperPackView",
    "DomainSummaryView",
    "decode_domain_build_policy",
    "decode_domain_build_request",
    "decode_domain_build_result",
    "decode_domain_build_warning",
    "decode_domain_package",
    "decode_domain_paper_pack",
    "decode_domain_summary",
    "domain_build_run_id",
    "domain_id_for",
    "encode_domain_build_policy",
    "encode_domain_build_request",
    "encode_domain_build_result",
    "encode_domain_build_warning",
    "mathematical_opportunities_validation_error",
    "render_network_html",
]
