import time
from datetime import datetime, timezone

from urlverify_mcp.config import Config
from urlverify_mcp.identity.aging import DEFAULT_AGING_SOURCES, method_for, snowflake_ts
from urlverify_mcp.models import Evidence, IdentityGraph, LLMSubmission, Verdict
from urlverify_mcp.rules import decide
from tests.test_rules import STORE, IDENT, _ev, _l0


def test_snowflake_decode():
    # tweet id 1234567890123456789 -> 2020-03-02
    d = datetime.fromtimestamp(snowflake_ts(1234567890123456789), tz=timezone.utc)
    assert (d.year, d.month, d.day) == (2020, 3, 2)


def test_method_mapping():
    assert method_for("https://www.reddit.com/r/LocalLLaMA/comments/abc/x/", DEFAULT_AGING_SOURCES) == "reddit_api"
    assert method_for("https://stackoverflow.com/questions/123/x", DEFAULT_AGING_SOURCES) == "stackexchange_api"
    assert method_for("https://x.com/u/status/1234567890123456789", DEFAULT_AGING_SOURCES) == "snowflake"
    assert method_for("https://www.facebook.com/x/posts/1", DEFAULT_AGING_SOURCES) == "none"
    assert method_for("https://forum.example.org/t/some-topic/4711", DEFAULT_AGING_SOURCES) == "discourse"
    assert method_for("https://random-blog.example/post", DEFAULT_AGING_SOURCES) == "wayback"
    assert method_for("https://random-blog.example/post", {"random-blog.example": "jsonld"}) == "jsonld"


def _sub():
    return LLMSubmission(identity=IDENT, evidence=[
        _ev("https://techcrunch.com/x", "official domain is lmstudio.ai", "available at lmstudio.ai for Mac"),
        _ev("https://someforum.example/thread/1", "official domain is lmstudio.ai", "lmstudio.ai is legit trust me", tier=3)],
        proposed_verdict="VERIFIED_TRUE")


def _age(days, method="reddit_api", strength="strong", edited_days=None, ok=True):
    now = time.time()
    return {"ok": ok, "method": method, "strength": strength, "created_ts": now - days * 86400,
            "age_days": days, "edited_ts": (now - edited_days * 86400) if edited_days else None}


def test_old_forum_post_is_promoted_and_counts():
    ages = {"https://someforum.example/thread/1": _age(800)}
    d = decide(Config(), _l0(), _sub(), STORE, "LM Studio", ages=ages)
    assert d.verdict == Verdict.TRUE, d.notes
    assert any("promoted to tier 2" in n for e in d.evidence for n in e.notes)


def test_young_or_weak_or_edited_post_not_promoted():
    for age in (_age(100), _age(800, method="jsonld", strength="weak"), _age(800, edited_days=30), None):
        ages = {"https://someforum.example/thread/1": age} if age else {}
        assert decide(Config(), _l0(), _sub(), STORE, "LM Studio", ages=ages).verdict == Verdict.UNVERIFIABLE


def test_login_walled_platform_never_promoted():
    ages = {"https://someforum.example/thread/1": {"ok": False, "method": "none", "strength": "none", "error": "login-walled"}}
    assert decide(Config(), _l0(), _sub(), STORE, "LM Studio", ages=ages).verdict == Verdict.UNVERIFIABLE


def test_post_predating_young_domain_is_a_contradiction():
    ages = {"https://someforum.example/thread/1": _age(800)}
    dom = {"ok": True, "source": "ct", "age_days": 200}      # domain only 200 days old, post is 800 days old
    l0 = _l0(host="lmstudio-download.com")
    d = decide(Config(), l0, _sub(), STORE, "LM Studio", ages=ages, target_domain_age=dom)
    assert d.verdict != Verdict.TRUE
    assert "post_predates_domain" in l0.risk_signals
    # an old domain is exempt from the contradiction rule
    dom_old = {"ok": True, "source": "wayback", "age_days": 5000}
    d2 = decide(Config(), _l0(), _sub(), STORE, "LM Studio", ages=ages, target_domain_age=dom_old)
    assert d2.verdict == Verdict.TRUE


def test_max_count_caps_promotion():
    sub = LLMSubmission(identity=IDENT, evidence=[
        _ev("https://someforum.example/thread/1", "official domain is lmstudio.ai", "lmstudio.ai is legit trust me", tier=3),
        _ev("https://other-forum.example/thread/2", "official domain is lmstudio.ai", "lmstudio.ai is legit trust me", tier=3)],
        proposed_verdict="VERIFIED_TRUE")
    store = dict(STORE); store["https://other-forum.example/thread/2"] = "lmstudio.ai is legit trust me"
    ages = {"https://someforum.example/thread/1": _age(800), "https://other-forum.example/thread/2": _age(900)}
    assert decide(Config(), _l0(), sub, store, "LM Studio", ages=ages).verdict == Verdict.UNVERIFIABLE
    cfg = Config(); cfg.identity.tier3_aged_max_count = 2
    sub = LLMSubmission(identity=IDENT, evidence=list(sub.evidence), proposed_verdict="VERIFIED_TRUE")
    for e in sub.evidence: e.verified_quote = None; e.notes = []; e.tier = 3
    assert decide(cfg, _l0(), sub, store, "LM Studio", ages=ages).verdict == Verdict.TRUE
