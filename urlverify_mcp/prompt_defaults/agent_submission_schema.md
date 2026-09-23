submit_verdict args schema:
{
  "identity": {
    "product": "...",
    "developer": "company / org name or null",
    "aliases": ["other names, brand names, former names"],
    "official_domains": ["the developer's OWN sites, e.g. lmstudio.ai — never a hosting platform (github.com, huggingface.co ...); platform owners go in official_orgs"],
    "official_repos": ["github.com/org/repo", "huggingface.co/org"],
    "official_orgs": {"github": ["org"], "huggingface": ["org"], "pypi": ["package"], "npm": ["package"]},
    "narrative": "how the chain was resolved"
  },
  "evidence": [
    {"kind": "github",                       <- structured tool result: cite fact ids, no source / quote needed
     "facts": ["F12", "F14"],
     "claim": "what the facts support, e.g. the GitHub org lmstudio-ai links to lmstudio.ai",
     "supports": true|false},
    {"kind": "media|page|distro",            <- fetched web page: exact URL + verbatim quote
     "source": "<exact URL you fetched>",
     "tier": 2|3,
     "claim": "what it supports, e.g. official domain is lmstudio.ai",
     "quote": "verbatim excerpt from the page",
     "supports": true|false}
  ],
  "proposed_verdict": "VERIFIED_TRUE|VERIFIED_FALSE|UNVERIFIABLE",
  "proposed_reason": "concise reasoning in English",
  "risk_notes": ["..."]
}
