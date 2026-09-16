Verify that `url` is an official / legitimate source for `project`.

Args:
    project: project / product name, e.g. "LM Studio".
    url: the download, installer, repository, model or data-source URL to check.
    description: what the URL is supposed to be, e.g. "Linux x64 AppImage installer".
    options: optional overrides: {"min_sources": 2, "allow_tier3": false, "history_days": 90}.
Returns a dict with verdict (VERIFIED_TRUE | VERIFIED_FALSE | UNVERIFIABLE), confidence, reason (in the caller's language),
evidence[], checks{}, identity{}, risk_signals[], cache_hits[], engine_notes[], trace_id.
