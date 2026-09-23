"""Shared data models: request, evidence, checks, verdict."""
from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class Verdict(str, Enum):
    TRUE = "VERIFIED_TRUE"
    FALSE = "VERIFIED_FALSE"
    UNVERIFIABLE = "UNVERIFIABLE"


class VerifyRequest(BaseModel):
    """Internal request for ONE URL (the MCP interface takes a `source`, see source/run.py)."""
    project: str
    url: str
    description: str = ""
    options: dict[str, Any] | None = None
    seeds: list[dict[str, Any]] = Field(default_factory=list)   # deterministic facts found while resolving the source


class Evidence(BaseModel):
    kind: str                      # e.g. wikidata, wikipedia, package_registry, media, github, wayback, page
    source: str                    # URL of the evidence
    tier: int = 3
    claim: str                     # what it supports, e.g. "official domain is lmstudio.ai"
    quote: str = ""                # verbatim excerpt that must exist in the fetched content
    summary: str = ""
    supports: bool = True
    verified_quote: bool | None = None   # set by rules engine
    notes: list[str] = Field(default_factory=list)


class CheckResult(BaseModel):
    name: str
    status: str                    # pass | fail | warn | skip | error
    fatal: bool = False            # a fatal fail forbids VERIFIED_TRUE
    detail: dict[str, Any] = Field(default_factory=dict)
    message: str = ""


class L0Result(BaseModel):
    normalized_url: str
    host: str
    etld1: str
    platform: str | None = None            # e.g. github, huggingface, pypi, npm (from anchors)
    platform_scope: str | None = None      # "user_content" | "company_site" (see cache/anchors.resolve)
    platform_owner: str | None = None      # e.g. "lmstudio-ai" for github.com/lmstudio-ai/..., "evil" for evil.github.io
    platform_repo: str | None = None
    final_url: str | None = None
    final_etld1: str | None = None
    checks: list[CheckResult] = Field(default_factory=list)
    risk_signals: list[str] = Field(default_factory=list)
    tls_org: str | None = None
    fetched_target_text: str | None = None

    @property
    def incomplete_required_checks(self) -> list[str]:
        required = ("scheme", "public_address", "dns", "tls", "redirects")
        return [name for name in required
                if not any(c.name == name and c.status in ("pass", "warn") for c in self.checks)
                or any(c.name == name and c.status not in ("pass", "warn") for c in self.checks)]

    @property
    def fatal_failures(self) -> list[CheckResult]:
        return [c for c in self.checks if c.status == "fail" and c.fatal]


class IdentityGraph(BaseModel):
    """LLM-proposed identity chain: product -> developer -> aliases -> official properties."""
    product: str
    developer: str | None = None
    aliases: list[str] = Field(default_factory=list)
    official_domains: list[str] = Field(default_factory=list)      # eTLD+1 or full hosts
    official_repos: list[str] = Field(default_factory=list)        # github.com/org/repo, huggingface.co/org
    official_orgs: dict[str, list[str]] = Field(default_factory=dict)   # {"github": ["lmstudio-ai"], "huggingface": [...]}
    narrative: str = ""


class LLMSubmission(BaseModel):
    identity: IdentityGraph
    evidence: list[Evidence] = Field(default_factory=list)
    proposed_verdict: str = Verdict.UNVERIFIABLE.value
    proposed_reason: str = ""
    risk_notes: list[str] = Field(default_factory=list)


SCHEMA_VERSION = 1


class VerifyResult(BaseModel):
    schema_version: int = SCHEMA_VERSION
    verdict: Verdict
    confidence: float
    reason: str
    evidence: list[Evidence] = Field(default_factory=list)
    checks: dict[str, Any] = Field(default_factory=dict)
    identity: IdentityGraph | None = None
    risk_signals: list[str] = Field(default_factory=list)
    cache_hits: list[str] = Field(default_factory=list)
    engine_notes: list[str] = Field(default_factory=list)
    trace_id: str = ""
    duration_s: float = 0.0
    path: str = "full"                     # full | registry_fast_path | l0_fatal | timeout
    degraded: list[str] = Field(default_factory=list)   # dependencies that failed during this verification (observed, not probed)
