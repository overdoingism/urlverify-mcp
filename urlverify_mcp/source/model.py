"""What a `source` string resolves to. One source may name several subjects (`pip install a b c`)."""
from __future__ import annotations

from pydantic import BaseModel, Field

# ecosystems whose subjects are verified end to end in this version
VERIFIED_ECOSYSTEMS = {"url", "pypi", "npm", "nuget", "winget", "git", "script", "huggingface", "docker", "homebrew", "scoop", "go", "flatpak", "distro"}


class Seed(BaseModel):
    """A deterministic fact found while resolving the source (e.g. the winget manifest line that names the installer
    URL). The pipeline adds it to the evidence store and to the evidence list; the rules engine verifies it like any
    other evidence."""
    kind: str
    source: str
    text: str
    claim: str
    quote: str


class Subject(BaseModel):
    input: str                                  # the part of `source` this subject came from
    ecosystem: str                              # see VERIFIED_ECOSYSTEMS + parse-only ones (cargo, go, gem, ...)
    name: str | None = None                     # package / image / repo id as written (npm alias resolved)
    version_spec: str | None = None             # as written: "==1.2", "^1.2", "latest", tag, digest
    version: str | None = None                  # exact version after resolution
    prerelease_ok: bool = False
    registry: str | None = None                 # registry / index actually used
    registry_basis: str | None = None           # "command flag" | "environment variable" | "config" | "public default"
    url: str | None = None                      # the URL the pipeline verifies
    executes_code: bool = False                 # npx / uvx / curl | sh: code runs immediately
    options: dict = Field(default_factory=dict) # parser facts used by resolution (winget architecture, scope, ...)
    seeds: list[Seed] = Field(default_factory=list)
    codes: list[str] = Field(default_factory=list)       # blocking codes: this subject is not verified
    notes: list[str] = Field(default_factory=list)       # informational codes / notes (always reported)

    @property
    def blocked(self) -> bool:
        return bool(self.codes)


class ParsedSource(BaseModel):
    tool: str | None = None                     # "pip", "npm", "winget", "url", "script", ...
    subjects: list[Subject] = Field(default_factory=list)
    codes: list[str] = Field(default_factory=list)       # source-level blocking codes (nothing is verified)
    message: str = ""
