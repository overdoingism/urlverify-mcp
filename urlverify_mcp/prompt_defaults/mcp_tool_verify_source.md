Verify that `url` is an official / legitimate source for `project`.

Args:
    project: the product name only, as it is commonly written, e.g. "LM Studio" or "Vulkan SDK". Do not append the vendor,
        parentheses, version numbers or notes (not "Vulkan SDK (LunarG / Khronos)"): the name is matched literally against
        evidence, and extra words prevent a match. Put the vendor and platform in `description` instead.
    url: the download, installer, repository, model or data-source URL to check.
    description: what the URL is supposed to be, e.g. "Linux x64 AppImage installer".
    options: optional overrides: {"min_sources": 2, "allow_tier3": false, "history_days": 90}.
Returns a dict with verdict (VERIFIED_TRUE | VERIFIED_FALSE | UNVERIFIABLE), confidence, reason (in the caller's language),
evidence[], checks{}, identity{}, risk_signals[], cache_hits[], engine_notes[], trace_id.
