"""The investigation loop: LLM + tools, with budget enforcement and an evidence store for quote verification."""
from __future__ import annotations

import json
import re
from typing import Any

from ..checks.injection import wrap_untrusted
from ..config import Config
from ..identity.structured import Structured
from ..models import L0Result, LLMSubmission
from ..providers.llm import LLM
from ..providers.search import SearchProvider
from .. import progress
from ..tracelog import TRACE
from .prompts import fallback_action_instructions, submission_schema_text, system_prompt


TOOLS: list[dict[str, Any]] = [
    {"type": "function", "function": {"name": "web_search", "description": "Web search (SearXNG). Returns titles, URLs, snippets.",
        "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}}},
    {"type": "function", "function": {"name": "fetch_url", "description": "Fetch a web page as readable text (truncated). Use for third-party pages, org profiles, README files.",
        "parameters": {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]}}},
    {"type": "function", "function": {"name": "wikidata_lookup", "description": "Structured lookup: entity, official website (P856) with revision-history stability, developer/publisher, aliases.",
        "parameters": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}}},
    {"type": "function", "function": {"name": "wikipedia_history", "description": "Wikipedia article infobox website + developer fields, with revision-history stability over the configured window.",
        "parameters": {"type": "object", "properties": {"title": {"type": "string"}, "lang": {"type": "string", "default": "en"}}, "required": ["title"]}}},
    {"type": "function", "function": {"name": "wayback_first_seen", "description": "Internet Archive first snapshot date for a domain (temporal evidence).",
        "parameters": {"type": "object", "properties": {"domain": {"type": "string"}}, "required": ["domain"]}}},
    {"type": "function", "function": {"name": "github_info", "description": "GitHub owner/org profile (created_at, blog/website, verified) and optional repo info (fork?, parent, stars, homepage).",
        "parameters": {"type": "object", "properties": {"owner": {"type": "string"}, "repo": {"type": "string"}}, "required": ["owner"]}}},
    {"type": "function", "function": {"name": "huggingface_info", "description": "Hugging Face org/user profile (verified badge) and optional repo info (author, createdAt).",
        "parameters": {"type": "object", "properties": {"owner": {"type": "string"}, "repo": {"type": "string"}}, "required": ["owner"]}}},
    {"type": "function", "function": {"name": "package_registry", "description": "PyPI / npm package metadata (homepage, repository).",
        "parameters": {"type": "object", "properties": {"registry": {"type": "string", "enum": ["pypi", "npm"]}, "name": {"type": "string"}}, "required": ["registry", "name"]}}},
    {"type": "function", "function": {"name": "submit_verdict", "description": "Submit the final structured findings. See the submission schema in the system prompt.",
        "parameters": {"type": "object", "properties": {
            "identity": {"type": "object"}, "evidence": {"type": "array", "items": {"type": "object"}},
            "proposed_verdict": {"type": "string"}, "proposed_reason": {"type": "string"},
            "risk_notes": {"type": "array", "items": {"type": "string"}}}, "required": ["identity", "evidence", "proposed_verdict"]}}},
]


class Budget:
    def __init__(self, cfg: Config):
        self.searches = cfg.budget.max_searches
        self.fetches = cfg.budget.max_fetches
        self.api_calls = cfg.budget.max_api_calls

    def take(self, kind: str) -> bool:
        v = getattr(self, kind)
        if v <= 0:
            return False
        setattr(self, kind, v - 1)
        return True

    def summary(self) -> str:
        return f"remaining budget: searches={self.searches} fetches={self.fetches} api_calls={self.api_calls}"


class Investigator:
    def __init__(self, cfg: Config, llm: LLM, search: SearchProvider, structured: Structured, fetcher=None):
        self.cfg = cfg
        self.llm = llm
        self.search = search
        self.fetcher = fetcher or search
        self.structured = structured
        self.budget = Budget(cfg)
        self.evidence_store: dict[str, str] = {}      # source url/key -> raw text (for quote verification)
        self.target_page_text: str | None = None
        self.tool_log: list[dict[str, Any]] = []
        self.target_url: str | None = None
        self.target_etld1: str | None = None

    def _remember(self, key: str, text: str) -> None:
        self.evidence_store[key] = text

    async def run_tool(self, name: str, args: dict[str, Any]) -> str:
        args = args or {}
        await progress.report(f"L1: {name} {_short(args)}")
        TRACE.log("agent_tool_call", tool=name, args=args, budget=self.budget.summary())
        out = await self._run_tool_inner(name, args)
        TRACE.log("agent_tool_result", tool=name, chars=len(out), text=out)
        return out

    async def _run_tool_inner(self, name: str, args: dict[str, Any]) -> str:
        try:
            if name == "web_search":
                if not self.budget.take("searches"):
                    return "BUDGET EXHAUSTED for searches. Use what you have or submit_verdict."
                q = str(args.get("query", "")).strip()
                out = await self.search.search(q)
                self._remember(f"search:{q}", out)
                return wrap_untrusted(f"search:{q}", out[: self.cfg.budget.fetch_max_chars])
            if name == "fetch_url":
                if not self.budget.take("fetches"):
                    return "BUDGET EXHAUSTED for fetches. Use what you have or submit_verdict."
                url = str(args.get("url", "")).strip()
                out = await self.fetcher.fetch(url)
                out = out[: self.cfg.budget.fetch_max_chars]
                self._remember(url, out)
                if self.target_etld1 and _etld1(url) == self.target_etld1 and self.target_page_text is None:
                    self.target_page_text = out
                return wrap_untrusted(url, out)
            if name in ("wikidata_lookup", "wikipedia_history", "wayback_first_seen", "github_info", "huggingface_info", "package_registry"):
                if not self.budget.take("api_calls"):
                    return "BUDGET EXHAUSTED for api_calls. Use what you have or submit_verdict."
                ic = self.cfg.identity
                if name == "wikidata_lookup":
                    r = await self.structured.wikidata(str(args.get("name", "")), ic.history_days, ic.min_stable_revisions)
                    keys = [e["source"] for e in r.get("entities", [])] if r.get("ok") else []
                elif name == "wikipedia_history":
                    r = await self.structured.wikipedia_history(str(args.get("title", "")), ic.history_days, ic.min_stable_revisions, str(args.get("lang") or "en"))
                    keys = [r.get("source")] if r.get("source") else []
                elif name == "wayback_first_seen":
                    r = await self.structured.wayback_first_seen(str(args.get("domain", "")))
                    keys = [r.get("source")] if r.get("source") else []
                elif name == "github_info":
                    r = await self.structured.github(str(args.get("owner", "")), args.get("repo") or None)
                    keys = [r.get("source")] if r.get("source") else []
                elif name == "huggingface_info":
                    r = await self.structured.huggingface(str(args.get("owner", "")), args.get("repo") or None)
                    keys = [r.get("source")] if r.get("source") else []
                else:
                    reg = str(args.get("registry", "pypi"))
                    r = await (self.structured.pypi if reg == "pypi" else self.structured.npm)(str(args.get("name", "")))
                    keys = [r.get("source")] if r.get("source") else []
                text = json.dumps(r, ensure_ascii=False, indent=1)
                for k in keys:
                    self._remember(k, text)
                self._remember(f"{name}:{json.dumps(args, sort_keys=True)}", text)
                return text
            return f"Unknown tool {name}"
        except Exception as e:  # noqa: BLE001
            return f"TOOL ERROR ({name}): {type(e).__name__}: {e}"

    async def investigate(self, project: str, url: str, description: str, l0: L0Result,
                          cached_identity: dict[str, Any] | None = None) -> LLMSubmission:
        self.target_url = l0.normalized_url
        self.target_etld1 = l0.etld1
        l0_summary = {
            "normalized_url": l0.normalized_url, "host": l0.host, "etld1": l0.etld1,
            "platform": l0.platform, "platform_owner": l0.platform_owner, "platform_repo": l0.platform_repo,
            "final_url": l0.final_url, "tls_organization": l0.tls_org,
            "checks": [{"name": c.name, "status": c.status, "message": c.message} for c in l0.checks],
            "risk_signals": l0.risk_signals,
        }
        user = (f"Project: {project}\nURL to verify: {url}\nTarget description: {description}\n\n"
                f"Deterministic pre-checks (L0) already done:\n{json.dumps(l0_summary, ensure_ascii=False, indent=1)}\n\n")
        if cached_identity:
            user += ("A previously verified identity graph for this project exists in cache (use it as a starting hypothesis, "
                     f"still confirm with at least one fresh source):\n{json.dumps(cached_identity, ensure_ascii=False)}\n\n")
        user += (f"Budget: {self.budget.summary()}.\n"
                 "Investigate, then call submit_verdict. Remember: the target must be matched against the official domains/orgs you establish.")
        schema_text = submission_schema_text()
        messages: list[dict[str, Any]] = [{"role": "system", "content": system_prompt() + "\n\n" + schema_text},
                                          {"role": "user", "content": user}]
        native = self.llm.supports_tools is not False
        if not native:
            messages[0]["content"] += "\n\n" + fallback_action_instructions().format(tool_list=_tool_list_text())

        for turn in range(self.cfg.llm.max_iterations):
            await progress.report(f"L1: LLM turn {turn + 1}/{self.cfg.llm.max_iterations}", 0.15 + 0.65 * turn / self.cfg.llm.max_iterations)
            msg = await self.llm.chat(messages, tools=TOOLS if native else None)
            if native and self.llm.supports_tools is False:
                # tools rejected mid-flight: switch to fallback mode
                native = False
                messages[0]["content"] += "\n\n" + fallback_action_instructions().format(tool_list=_tool_list_text())
                continue
            if native and msg.tool_calls:
                messages.append({"role": "assistant", "content": msg.content or "", "tool_calls": [
                    {"id": tc.id, "type": "function", "function": {"name": tc.function.name, "arguments": tc.function.arguments}} for tc in msg.tool_calls]})
                for tc in msg.tool_calls:
                    try:
                        args = json.loads(tc.function.arguments or "{}")
                    except json.JSONDecodeError:
                        args = LLM.extract_json(tc.function.arguments or "") or {}
                    if tc.function.name == "submit_verdict":
                        TRACE.log("agent_submission", raw=args)
                        return self._parse_submission(args, project)
                    out = await self.run_tool(tc.function.name, args)
                    self.tool_log.append({"tool": tc.function.name, "args": args, "chars": len(out)})
                    messages.append({"role": "tool", "tool_call_id": tc.id, "content": out})
                continue
            # no native tool call: parse JSON action from content
            content = msg.content or ""
            action = LLM.extract_json(content)
            if action and action.get("action"):
                name, args = action["action"], action.get("args") or {}
                messages.append({"role": "assistant", "content": content})
                if name == "submit_verdict":
                    TRACE.log("agent_submission", raw=args)
                    return self._parse_submission(args, project)
                out = await self.run_tool(name, args)
                self.tool_log.append({"tool": name, "args": args, "chars": len(out)})
                messages.append({"role": "user", "content": f"Tool result for {name}:\n{out}\n\n{self.budget.summary()}\nNext action (JSON only):"})
                continue
            # the model answered in prose: nudge it once, then force submission
            messages.append({"role": "assistant", "content": content})
            messages.append({"role": "user", "content": "Reply with a tool call / JSON action only. If you are done, call submit_verdict with the schema."})
        # iterations exhausted: ask for a submission directly
        messages.append({"role": "user", "content": "Iteration limit reached. Call submit_verdict NOW with what you have. " + schema_text})
        msg = await self.llm.chat(messages, tools=TOOLS if native else None, json_mode=not native)
        if native and msg.tool_calls:
            for tc in msg.tool_calls:
                if tc.function.name == "submit_verdict":
                    try:
                        return self._parse_submission(json.loads(tc.function.arguments or "{}"), project)
                    except json.JSONDecodeError:
                        pass
        data = LLM.extract_json(msg.content or "") or {}
        if "args" in data and isinstance(data["args"], dict):
            data = data["args"]
        return self._parse_submission(data, project)

    @staticmethod
    def _parse_submission(data: dict[str, Any], project: str) -> LLMSubmission:
        ident = data.get("identity") or {}
        if not isinstance(ident, dict):
            ident = {}
        ident.setdefault("product", project)
        # normalise domains
        doms = []
        for d in ident.get("official_domains") or []:
            d = str(d).strip().lower()
            d = re.sub(r"^https?://", "", d).split("/")[0]
            if d:
                doms.append(d)
        ident["official_domains"] = doms
        ident["official_orgs"] = {k.lower(): [str(x) for x in v] for k, v in (ident.get("official_orgs") or {}).items() if isinstance(v, list)}
        ident["official_repos"] = [str(x) for x in ident.get("official_repos") or []]
        ident["aliases"] = [str(x) for x in ident.get("aliases") or []]
        evs = []
        for e in data.get("evidence") or []:
            if not isinstance(e, dict) or not e.get("source"):
                continue
            evs.append({"kind": str(e.get("kind") or "page"), "source": str(e["source"]), "tier": int(e.get("tier") or 3),
                        "claim": str(e.get("claim") or ""), "quote": str(e.get("quote") or ""), "summary": str(e.get("summary") or ""),
                        "supports": bool(e.get("supports", True))})
        pv = str(data.get("proposed_verdict") or "UNVERIFIABLE").upper()
        if pv not in ("VERIFIED_TRUE", "VERIFIED_FALSE", "UNVERIFIABLE"):
            pv = "UNVERIFIABLE"
        return LLMSubmission.model_validate({"identity": ident, "evidence": evs, "proposed_verdict": pv,
                                             "proposed_reason": str(data.get("proposed_reason") or ""),
                                             "risk_notes": [str(x) for x in data.get("risk_notes") or []]})


def _short(args: dict[str, Any]) -> str:
    v = next((str(x) for x in args.values() if x), "")
    return (v[:60] + "…") if len(v) > 60 else v


def _tool_list_text() -> str:
    return "\n".join(f"- {t['function']['name']}({', '.join(t['function']['parameters'].get('properties', {}).keys())}): {t['function']['description'][:140]}" for t in TOOLS)


def _etld1(url: str) -> str:
    from ..checks.urltools import etld1_of, host_of
    return etld1_of(host_of(url))
