"""verify_source orchestration: validate input -> parse source -> resolve -> verify each subject -> aggregate.

The per-URL pipeline (pipeline.verify) is unchanged; this layer decides WHAT is verified and turns the results into
fixed verdict / next_action / codes (AGENTS.md §13)."""
from __future__ import annotations

import re
import time
import uuid
from typing import Any

from pydantic import BaseModel, Field

from .. import progress
from ..config import Config
from ..models import Verdict, VerifyRequest, VerifyResult
from ..storage import Storage
from ..tracelog import TRACE
from .model import Subject
from .parse import parse_source
from .resolve import Resolver

SCHEMA_VERSION = 2

# next_action, most severe first
NEXT_ACTIONS = ("DO_NOT_PROCEED", "FIX_INPUT_AND_RETRY", "INFORM_USER_AND_CONFIRM", "PROCEED")
# blocking codes the caller can fix by changing the call
_FIXABLE = ("INPUT_", "SOURCE_", "UNSUPPORTED_FLAG", "UNSUPPORTED_ENV", "FLAG_MISSING_VALUE", "INVALID_PACKAGE_SPEC",
            "LOCAL_PATH_UNSUPPORTED", "REQUIREMENTS_FILE_UNSUPPORTED", "LOCKFILE_INSTALL_UNSUPPORTED", "REGISTRY_AMBIGUOUS",
            "VERSION_NOT_FOUND", "VERSION_RANGE_UNSUPPORTED", "WINGET_QUERY_NOT_EXACT", "WINGET_TOO_MANY_INSTALLERS",
            "WINGET_MANIFEST_NOT_FOUND", "WINGET_INSTALLER_NOT_FOUND", "TOO_MANY_SUBJECTS", "GIT_REMOTE_UNSUPPORTED",
            "GIT_PROTOCOL_INSECURE", "PACKAGE_HAS_NO_RELEASE", "HOMEBREW_NOT_FOUND", "SCOOP_BUCKET_AMBIGUOUS",
            "SCOOP_MANIFEST_NOT_FOUND", "SCOOP_VERSION_PIN_UNSUPPORTED", "GO_IMPORT_NOT_FOUND")
# notices that turn a TRUE into "tell the user first"
_CAUTION = ("SCRIPT_MAY_DOWNLOAD_MORE", "HASH_CHECK_DISABLED", "MALWARE_SCAN_DISABLED", "TLS_VERIFICATION_DISABLED",
            "PRIVILEGED_CONTAINER", "SUBMODULES_NOT_VERIFIED", "RELEASE_COOLDOWN_ACTIVE", "RELEASE_COOLDOWN_UNKNOWN",
            "SELF_PUBLISHED_CAP", "CONTENT_TRUST_DISABLED", "INSTALLER_ARGUMENTS_OVERRIDDEN", "LOW_CONFIDENCE",
            "QUARANTINE_DISABLED")
CONFIDENCE_FOR_PROCEED = 0.8


class SourceRequest(BaseModel):
    project: str = ""
    source: str = ""
    artifact: str = ""
    description: str = ""
    version: str = ""
    options: dict[str, Any] | None = None


class SubjectResult(BaseModel):
    index: int
    subject: Subject
    verdict: Verdict
    confidence: float
    next_action: str
    codes: list[str] = Field(default_factory=list)       # why it is not TRUE / what blocked it
    notices: list[str] = Field(default_factory=list)     # informational (executes code, cooldown, ...)
    result: VerifyResult | None = None                  # full per-URL pipeline result (evidence, checks, reason)


class SourceResult(BaseModel):
    schema_version: int = SCHEMA_VERSION
    trace_id: str
    request: SourceRequest
    verdict: Verdict
    confidence: float
    next_action: str
    codes: list[str] = Field(default_factory=list)
    notices: list[str] = Field(default_factory=list)
    message: str = ""                                   # parser / validation message (deterministic)
    subjects: list[SubjectResult] = Field(default_factory=list)
    duration_s: float = 0.0


def _is_fixable(code: str) -> bool:
    return code.startswith(_FIXABLE)


def _worst(actions: list[str]) -> str:
    return min(actions, key=NEXT_ACTIONS.index) if actions else "FIX_INPUT_AND_RETRY"


def _validation_codes(req: SourceRequest) -> list[str]:
    codes = []
    if not req.project.strip():
        codes.append("INPUT_PROJECT_MISSING")
    if not req.source.strip():
        codes.append("INPUT_SOURCE_MISSING")
    if not req.artifact.strip() and not req.description.strip():
        codes.append("INPUT_ARTIFACT_OR_DESCRIPTION_MISSING")
    return codes


def _apply_version(s: Subject, version: str) -> None:
    """Merge the `version` argument into a subject: fill an unpinned spec, or record the expectation so a conflicting
    pin in the command is reported instead of silently overridden."""
    v = version.strip()
    if not v:
        return
    if s.ecosystem in ("pypi", "npm", "nuget", "winget"):
        if not s.version_spec:
            s.version_spec = f"=={v}" if s.ecosystem == "pypi" else v
        s.options["expected_version"] = v
    elif s.ecosystem in ("git", "huggingface") and not s.version_spec:
        s.version_spec = v
        s.notes.append("GIT_REF_NOT_VERIFIED")
    else:
        s.notes.append("VERSION_NOT_ENFORCED")


def _same_version(eco: str, a: str, b: str) -> bool:
    if eco == "pypi":
        from packaging.version import InvalidVersion, Version
        try:
            return Version(a) == Version(b)
        except InvalidVersion:
            pass
    return a.lstrip("v").lower() == b.lstrip("v").lower()


def result_codes(res: VerifyResult) -> tuple[list[str], list[str]]:
    """Fixed codes derived from a per-URL result. (v0.2: derived from checks / risk signals / engine notes; the
    identity-graph rewrite will emit them natively.)"""
    codes, notices = [], []
    for name, c in (res.checks or {}).items():
        if c.get("status") == "fail":
            codes.append(f"CHECK_FAILED:{name}")
    for sig in res.risk_signals:
        if str(sig).startswith("RELEASE_COOLDOWN_PERIOD"):
            notices.append("RELEASE_COOLDOWN_ACTIVE")
        elif sig == "release_cooldown_unknown":
            notices.append("RELEASE_COOLDOWN_UNKNOWN")
    notes = " ".join(res.engine_notes)
    sigs = " ".join(str(x) for x in res.risk_signals)
    if "npm_security_holding_package" in sigs or "security holding package" in notes:
        codes.append("PACKAGE_SECURITY_HOLDING")
    if "does not exist on" in notes or "registry state: missing" in notes:
        codes.append("PACKAGE_NOT_FOUND")
    if "registry_typosquat:" in sigs:
        codes.append("PACKAGE_NAME_LOOKALIKE")
    codes.extend(c for c in res.rule_codes if c not in codes)
    if res.verdict == Verdict.UNVERIFIABLE:
        # native gap codes (AGENTS §6.3): one per missing identity edge, argument-free so callers can switch on them
        for m in res.missing_edges:
            edge = m.get("edge", "")
            kind = edge.split(":", 1)[0]
            if kind == "PROJECT_NAME_MATCH":
                codes.append("PROJECT_NAME_NOT_MATCHED")
            elif kind != "TARGET_CHECKS":
                codes.append(f"MISSING_EDGE:{kind}")
    if "self-published" in notes:
        notices.append("SELF_PUBLISHED_CAP")
    if res.verdict == Verdict.FALSE and not codes:
        codes.append("OFFICIAL_CHANNEL_CONTRADICTED")
    if res.verdict == Verdict.UNVERIFIABLE and not codes:
        if res.path == "timeout":
            codes.append("TIME_BUDGET_EXCEEDED")
        else:
            codes.append("IDENTITY_NOT_ESTABLISHED")
    return list(dict.fromkeys(codes)), list(dict.fromkeys(notices))


def _next_action(verdict: Verdict, confidence: float, codes: list[str], notices: list[str]) -> str:
    if verdict == Verdict.FALSE:
        return "DO_NOT_PROCEED"
    if codes and all(_is_fixable(c) for c in codes) and verdict != Verdict.TRUE:
        return "FIX_INPUT_AND_RETRY"
    if verdict != Verdict.TRUE:
        return "INFORM_USER_AND_CONFIRM"
    if confidence < CONFIDENCE_FOR_PROCEED or any(n.startswith(_CAUTION) for n in notices):
        return "INFORM_USER_AND_CONFIRM"
    return "PROCEED"


def _llm_description(req: SourceRequest, s: Subject) -> str:
    parts = []
    if req.artifact.strip():
        parts.append(f"artifact: {req.artifact.strip()}")
    if req.description.strip():
        parts.append(f"purpose: {req.description.strip()}")
    parts.append(f"requested as: {req.source.strip()}")
    what = " ".join(x for x in (s.ecosystem, s.name or "", s.version or "") if x)
    parts.append(f"resolved to: {what} -> {s.url}")
    return "; ".join(parts)


def _blocked_result(i: int, s: Subject) -> SubjectResult:
    codes = list(dict.fromkeys(s.codes))
    verdict = Verdict.UNVERIFIABLE
    return SubjectResult(index=i, subject=s, verdict=verdict, confidence=0.0, codes=codes, notices=list(s.notes),
                         next_action=_next_action(verdict, 0.0, codes, s.notes))


async def verify_source(req: SourceRequest, cfg: Config, store: Storage) -> SourceResult:
    from ..pipeline import verify
    t0 = time.time()
    trace_id = uuid.uuid4().hex[:12]
    TRACE.log("source_request", trace_id=trace_id, request=req.model_dump())

    def finish(out: SourceResult) -> SourceResult:
        out.duration_s = round(time.time() - t0, 1)
        store.add_history(trace_id, req.project, req.source, req.artifact or req.description, out.verdict.value,
                          out.confidence, out.model_dump(mode="json"))
        TRACE.log("source_result", result=out.model_dump(mode="json"))
        return out

    bad = _validation_codes(req)
    if bad:
        return finish(SourceResult(trace_id=trace_id, request=req, verdict=Verdict.UNVERIFIABLE, confidence=0.0,
                                   next_action="FIX_INPUT_AND_RETRY", codes=bad,
                                   message="project and source are required; give artifact (what form) or description (what for), or both"))
    parsed = parse_source(req.source, cfg.source.registries, cfg.source.max_subjects)
    if parsed.codes:
        action = "FIX_INPUT_AND_RETRY" if all(_is_fixable(c) for c in parsed.codes) else "INFORM_USER_AND_CONFIRM"
        return finish(SourceResult(trace_id=trace_id, request=req, verdict=Verdict.UNVERIFIABLE, confidence=0.0,
                                   next_action=action, codes=parsed.codes, message=parsed.message))
    subjects = parsed.subjects
    if req.version.strip() and len(subjects) > 1:
        return finish(SourceResult(trace_id=trace_id, request=req, verdict=Verdict.UNVERIFIABLE, confidence=0.0,
                                   next_action="FIX_INPUT_AND_RETRY", codes=["INPUT_VERSION_AMBIGUOUS"],
                                   message="`version` cannot apply to several packages; pin each one in the command instead"))
    for s in subjects:
        _apply_version(s, req.version)

    await progress.report(f"resolving {len(subjects)} subject(s) from the source", 0.01)
    resolver = Resolver(max(cfg.net.timeout_s, 20), cfg.net.user_agent, cfg.identity.github_token)
    resolved: list[Subject] = []
    try:
        for s in subjects:
            resolved += await resolver.resolve(s, f"{req.artifact} {req.description}")
    finally:
        await resolver.close()
    for s in resolved:
        exp = s.options.get("expected_version")
        if exp and s.version and not s.codes and not _same_version(s.ecosystem, s.version, exp):
            s.codes.append("INPUT_VERSION_CONFLICT")
        if not s.codes and not s.url:
            s.codes.append("SOURCE_UNPARSABLE")
    TRACE.log("source_resolved", subjects=[s.model_dump() for s in resolved])

    results: list[SubjectResult] = []
    for i, s in enumerate(resolved, 1):
        if s.blocked:
            results.append(_blocked_result(i, s))
            continue
        await progress.report(f"verifying {i}/{len(resolved)}: {s.url}", 0.02)
        vr = VerifyRequest(project=req.project, url=s.url or "", description=_llm_description(req, s), options=req.options,
                           seeds=[x.model_dump() for x in s.seeds])
        res = await verify(vr, cfg, store, record=False)
        codes, notices = result_codes(res)
        notices = list(dict.fromkeys(s.notes + notices))
        if res.verdict == Verdict.TRUE and res.confidence < CONFIDENCE_FOR_PROCEED:
            notices.append("LOW_CONFIDENCE")
        results.append(SubjectResult(index=i, subject=s, verdict=res.verdict, confidence=res.confidence, codes=codes,
                                     notices=notices, result=res,
                                     next_action=_next_action(res.verdict, res.confidence, codes, notices)))

    verdicts = [r.verdict for r in results]
    overall = (Verdict.FALSE if Verdict.FALSE in verdicts else
               Verdict.TRUE if verdicts and all(v == Verdict.TRUE for v in verdicts) else Verdict.UNVERIFIABLE)
    conf = min((r.confidence for r in results), default=0.0)
    out = SourceResult(trace_id=trace_id, request=req, verdict=overall, confidence=round(conf, 2),
                       next_action=_worst([r.next_action for r in results]),
                       codes=list(dict.fromkeys(c for r in results for c in r.codes)),
                       notices=list(dict.fromkeys(n for r in results for n in r.notices)),
                       message=parsed.message, subjects=results)
    return finish(out)


def is_cjk(*texts: str) -> bool:
    return any(re.search(r"[㐀-鿿]", t or "") for t in texts)
