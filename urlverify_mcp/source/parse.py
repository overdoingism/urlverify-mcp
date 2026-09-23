"""Deterministic parsing of the `source` argument: a URL or one install / download command.

Principles (AGENTS.md §13):
- never guess: a bare name, an unknown command, an unknown flag or an ambiguous registry is reported with a code and
  the subject is not verified; nothing is polled across registries;
- flags are white-listed per tool: a recognised flag is interpreted, an unrecognised one yields UNSUPPORTED_FLAG:<flag>
  (a flag we do not understand may change what is installed, e.g. `-i https://evil/simple`);
- one source may name several subjects (`pip install a b c`); each is verified separately;
- the command is never executed.
"""
from __future__ import annotations

import re
import shlex
from typing import Callable, Iterable

from .model import ParsedSource, Subject

URL_RE = re.compile(r"""https?://[^\s'"()<>|;&`,]+""", re.I)
SCP_GIT_RE = re.compile(r"^(?:ssh://)?(?:[\w.-]+@)?(github\.com|gitlab\.com|codeberg\.org|bitbucket\.org)[:/]([\w.-]+)/([\w.-]+?)(?:\.git)?/?$", re.I)

PUBLIC_REGISTRY_HOSTS = {
    "pypi": {"pypi.org", "www.pypi.org", "files.pythonhosted.org"},
    "npm": {"registry.npmjs.org", "registry.npmjs.com", "registry.yarnpkg.com"},
    "nuget": {"api.nuget.org", "www.nuget.org", "nuget.org"},
}
DEFAULT_REGISTRIES = {"pypi": "https://pypi.org/simple", "npm": "https://registry.npmjs.org",
                      "nuget": "https://api.nuget.org/v3/index.json"}

# environment variables that change where packages come from
ENV_PRIMARY = {"PIP_INDEX_URL": "pypi", "UV_INDEX_URL": "pypi", "UV_DEFAULT_INDEX": "pypi",
               "NPM_CONFIG_REGISTRY": "npm", "YARN_REGISTRY": "npm"}
ENV_EXTRA = {"PIP_EXTRA_INDEX_URL", "UV_EXTRA_INDEX_URL", "UV_INDEX", "PIP_FIND_LINKS", "UV_FIND_LINKS"}

CONTROL_TOKENS = {";", "&&", "||", "&", "(", ")", "<", ">", ">>", "<<", "|&", ";;", "<(", ">("}


# --------------------------------------------------------------------------------------------------------- helpers
class Flags:
    """Per-tool flag table. `value` flags consume the next token (or `--flag=value`)."""

    def __init__(self, boolean: Iterable[str] = (), value: Iterable[str] = (), ci: bool = False):
        self.ci = ci
        self.boolean = {self._k(f) for f in boolean}
        self.value = {self._k(f) for f in value}

    def _k(self, f: str) -> str:
        return f.lower() if self.ci else f

    def kind(self, flag: str) -> str | None:
        k = self._k(flag)
        if k in self.boolean:
            return "bool"
        if k in self.value:
            return "value"
        return None


def walk(args: list[str], flags: Flags, stop_at_first_positional: bool = False):
    """Split args into (positionals, seen_flags {canonical: [values]}, codes). Combined short boolean flags (-Uq) are
    accepted when every letter is a known boolean flag."""
    pos: list[str] = []
    seen: dict[str, list[str]] = {}
    codes: list[str] = []
    rest: list[str] = []
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--":
            rest = args[i + 1:]
            break
        if a.startswith("-") and len(a) > 1 and not re.fullmatch(r"-\d.*", a):
            name, eq, val = a.partition("=")
            kind = flags.kind(name)
            if kind == "bool" and not eq:
                seen.setdefault(flags._k(name), []).append("")
            elif kind == "value":
                if not eq:
                    if i + 1 >= len(args):
                        codes.append(f"FLAG_MISSING_VALUE:{name}")
                        break
                    val = args[i + 1]
                    i += 1
                seen.setdefault(flags._k(name), []).append(val)
            elif not a.startswith("--") and len(a) > 2 and not eq and all(flags.kind("-" + c) == "bool" for c in a[1:]):
                for c in a[1:]:
                    seen.setdefault(flags._k("-" + c), []).append("")
            elif not a.startswith("--") and len(a) > 2 and not eq and flags.kind(a[:2]) == "value":
                seen.setdefault(flags._k(a[:2]), []).append(a[2:])      # -iURL / -vX
            else:
                codes.append(f"UNSUPPORTED_FLAG:{name}")
        else:
            pos.append(a)
            if stop_at_first_positional:
                rest = args[i + 1:]
                break
        i += 1
    return pos, seen, codes, rest


def _has(seen: dict, *names: str) -> bool:
    return any(n in seen for n in names)


def _val(seen: dict, *names: str) -> str | None:
    for n in names:
        if seen.get(n):
            return seen[n][-1]
    return None


def _host(url: str) -> str:
    m = re.match(r"^[a-z][a-z0-9+.-]*://([^/:?#]+)", url.strip(), re.I)
    return m.group(1).lower() if m else ""


def _is_local_path(tok: str) -> bool:
    t = tok.strip()
    return (t.startswith((".", "/", "~", "file:", "link:", "workspace:", "portal:")) or "\\" in t
            or re.match(r"^[a-zA-Z]:[\\/]", t) is not None
            or (re.search(r"\.(whl|tar\.gz|tgz|zip|nupkg)$", t, re.I) is not None and "://" not in t))


def git_https(ref: str) -> str | None:
    """Deterministic HTTPS form of a git remote on a known host (git@github.com:o/r.git, ssh://..., https://...)."""
    r = ref.strip()
    r = r[4:] if r.lower().startswith("git+") else r
    r = r.split("#", 1)[0]
    m = SCP_GIT_RE.match(r)
    if m:
        return f"https://{m.group(1).lower()}/{m.group(2)}/{m.group(3)}"
    if re.match(r"^https?://", r, re.I):
        u = re.sub(r"\.git/?$", "", r.rstrip("/"))
        return u
    return None


def _subject(inp: str, eco: str, **kw) -> Subject:
    return Subject(input=inp, ecosystem=eco, **kw)


def _git_subject(inp: str, ref: str, note: str | None = None) -> Subject:
    raw = ref[4:] if ref.lower().startswith("git+") else ref
    frag = raw.split("#", 1)[1] if "#" in raw else None
    url = git_https(ref)
    s = _subject(inp, "git", name=ref)
    if note:
        s.notes.append(note)
    if url is None:
        s.codes.append("GIT_REMOTE_UNSUPPORTED" if not raw.lower().startswith("git://") else "GIT_PROTOCOL_INSECURE")
        return s
    at = re.match(r"^(https?://[^@]+?)@([\w./-]+)$", url)            # pip style git+https://host/o/r@ref
    if at and at.group(1).count("/") >= 4:
        url, frag = at.group(1), frag or at.group(2)
    s.url = re.sub(r"\.git$", "", url)
    if frag:
        s.version_spec = frag
        s.notes.append("GIT_REF_NOT_VERIFIED")
    return s


# ---------------------------------------------------------------------------------------------------- entry point
def parse_source(source: str, registries: dict[str, str] | None = None, max_subjects: int = 8) -> ParsedSource:
    regs = {**DEFAULT_REGISTRIES, **(registries or {})}
    s = (source or "").strip()
    if not s:
        return ParsedSource(codes=["INPUT_SOURCE_MISSING"], message="source is empty")
    if re.fullmatch(r"https?://\S+", s, re.I):
        return ParsedSource(tool="url", subjects=[_subject(s, "url", url=s)])
    if re.search(r"(?i)(?:^|[|;&(`\"']\s*|\$\(\s*|<\(\s*|sudo\s+)(curl|wget|iwr|irm|invoke-webrequest|invoke-restmethod|start-bitstransfer)(?![\w.-])"
                 r"|\.(downloadstring|downloadfile)\s*\(", s):
        return _parse_script(s)
    try:
        lex = shlex.shlex(s, posix=True, punctuation_chars=";&|()<>")
        lex.whitespace_split = True
        lex.commenters = ""                       # '#' is a git ref / npm fragment, not a comment
        tokens = list(lex)
    except ValueError as e:
        return ParsedSource(codes=["SOURCE_UNPARSABLE"], message=f"could not tokenise the command: {e}")
    if not tokens:
        return ParsedSource(codes=["SOURCE_UNPARSABLE"], message="no tokens")
    if "|" in tokens:
        return ParsedSource(codes=["SOURCE_PIPELINE_UNSUPPORTED"], message="pipelines are only understood for download-and-run scripts")
    if any(t in CONTROL_TOKENS for t in tokens):
        return ParsedSource(codes=["SOURCE_CHAINED_COMMANDS"], message="send one command per call (&&, ;, redirects are not interpreted)")

    env: dict[str, str] = {}
    while tokens and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", tokens[0]):
        k, _, v = tokens.pop(0).partition("=")
        env[k] = v
    while tokens and tokens[0].lower() in ("sudo", "doas"):
        tokens.pop(0)
        while tokens and tokens[0] in ("-H", "-E", "-n", "--preserve-env"):
            tokens.pop(0)
    if not tokens:
        return ParsedSource(codes=["SOURCE_UNPARSABLE"], message="no command")
    for k in env:
        if k.upper() not in ENV_PRIMARY and k.upper() not in ENV_EXTRA:
            return ParsedSource(codes=[f"UNSUPPORTED_ENV:{k}"], message=f"environment variable {k} is not interpreted")

    cmd = re.sub(r"\.exe$", "", tokens[0].split("/")[-1].split("\\")[-1].lower())
    args = tokens[1:]
    if len(tokens) == 1 and not re.search(r"[/:]", tokens[0]) and cmd not in KNOWN_COMMANDS:
        return ParsedSource(codes=["SOURCE_BARE_NAME_AMBIGUOUS"],
                            message="a bare name does not say which registry; pass the install command (e.g. `pip install x`, `npm i x`) or a URL")
    handler = _dispatch(cmd, args)
    if handler is None:
        if len(tokens) == 1:
            return ParsedSource(codes=["SOURCE_BARE_NAME_AMBIGUOUS"], message="a bare name does not say which registry")
        return ParsedSource(codes=["SOURCE_COMMAND_UNSUPPORTED"], message=f"`{tokens[0]}` is not a supported install command")
    parsed = handler()
    _apply_env(parsed, env, regs)
    _apply_registry_defaults(parsed, regs)
    if len(parsed.subjects) > max_subjects:
        return ParsedSource(tool=parsed.tool, codes=["TOO_MANY_SUBJECTS"],
                            message=f"{len(parsed.subjects)} packages in one call; the limit is {max_subjects}")
    if not parsed.subjects and not parsed.codes:
        parsed.codes.append("SOURCE_NO_PACKAGE")
        parsed.message = parsed.message or "the command names no package"
    return parsed


def _apply_env(parsed: ParsedSource, env: dict[str, str], regs: dict[str, str]) -> None:
    for k, v in env.items():
        ku = k.upper()
        for s in parsed.subjects:
            if ku in ENV_EXTRA and s.ecosystem in ("pypi",):
                s.codes.append("REGISTRY_AMBIGUOUS")
            elif ENV_PRIMARY.get(ku) == s.ecosystem and not s.registry:
                s.registry, s.registry_basis = v, "environment variable"


def _apply_registry_defaults(parsed: ParsedSource, regs: dict[str, str]) -> None:
    for s in parsed.subjects:
        if s.ecosystem not in PUBLIC_REGISTRY_HOSTS:
            continue
        if not s.registry:
            s.registry = regs[s.ecosystem]
            s.registry_basis = "config" if regs[s.ecosystem] != DEFAULT_REGISTRIES[s.ecosystem] else "public default"
        if _host(s.registry) not in PUBLIC_REGISTRY_HOSTS[s.ecosystem]:
            s.codes.append(f"REGISTRY_UNSUPPORTED:{_host(s.registry) or s.registry}")


# ------------------------------------------------------------------------------------------------------ dispatch
PARSE_ONLY = {"cargo": "crates", "go": "go", "gem": "rubygems", "composer": "packagist", "brew": "homebrew",
              "scoop": "scoop", "choco": "chocolatey", "conda": "conda", "mamba": "conda", "micromamba": "conda",
              "apt": "apt", "apt-get": "apt", "dnf": "rpm", "yum": "rpm", "pacman": "pacman", "zypper": "rpm",
              "apk": "apk", "snap": "snap", "flatpak": "flatpak", "ollama": "ollama", "install-module": "psgallery"}
KNOWN_COMMANDS = {"pip", "pip3", "python", "python3", "py", "uv", "uvx", "pipx", "poetry", "pdm", "rye", "pipenv",
                  "npm", "pnpm", "yarn", "bun", "npx", "pnpx", "bunx", "dotnet", "nuget", "install-package", "winget",
                  "git", "gh", "hf", "huggingface-cli", "docker", "podman", "nerdctl"} | set(PARSE_ONLY)


def _dispatch(cmd: str, args: list[str]) -> Callable[[], ParsedSource] | None:
    if re.fullmatch(r"pip(\d(\.\d+)?)?", cmd):
        return lambda: _pip_like("pip", args, expect_sub=("install",))
    if re.fullmatch(r"python(\d(\.\d+)?)?|py", cmd):
        return lambda: _python_m(cmd, args)
    if cmd == "uv":
        return lambda: _uv(args)
    if cmd == "uvx":
        return lambda: _exec_pypi("uvx", args)
    if cmd == "pipx":
        return lambda: _pipx(args)
    if cmd in ("poetry", "pdm", "rye"):
        return lambda: _pip_like(cmd, args, expect_sub=("add",))
    if cmd == "pipenv":
        return lambda: _pip_like("pipenv", args, expect_sub=("install",))
    if cmd in ("npm", "pnpm", "yarn", "bun"):
        return lambda: _npm_family(cmd, args)
    if cmd in ("npx", "pnpx", "bunx"):
        return lambda: _npm_exec(cmd, args)
    if cmd == "dotnet":
        return lambda: _dotnet(args)
    if cmd == "nuget":
        return lambda: _nuget_exe(args)
    if cmd == "install-package":
        return lambda: _install_package(args)
    if cmd == "winget":
        return lambda: _winget(args)
    if cmd == "git":
        return lambda: _git(args)
    if cmd == "gh":
        return lambda: _gh(args)
    if cmd in ("hf", "huggingface-cli"):
        return lambda: _hf(cmd, args)
    if cmd in ("docker", "podman", "nerdctl"):
        return lambda: _docker(cmd, args)
    if cmd in PARSE_ONLY:
        return lambda: _parse_only(cmd, args)
    return None


# ------------------------------------------------------------------------------------------------------------ PyPI
PIP_FLAGS = Flags(
    boolean=["-U", "--upgrade", "--user", "-q", "--quiet", "-v", "--verbose", "--no-cache-dir", "--pre", "--force-reinstall",
             "--no-deps", "--break-system-packages", "--no-warn-script-location", "--disable-pip-version-check",
             "--prefer-binary", "--no-compile", "--compile", "--ignore-installed", "-I", "--no-build-isolation", "--isolated",
             "--no-input", "--system", "--dev", "-D", "--no-color", "--require-virtualenv", "--no-sync", "--frozen", "--locked",
             "--raw", "--optional-deps", "--allow-prereleases", "--prerelease-allow", "--dry-run", "--lock", "--skip-lock",
             "-d", "--save-compatible", "--save-exact", "--save-wildcard", "--save-minimum", "-u", "--unconstrained",
             "--no-self", "--force", "--include-deps", "--global", "--exact", "--no-progress", "--compile-bytecode",
             "--reinstall", "--no-editable"],
    value=["--upgrade-strategy", "--only-binary", "--no-binary", "--python", "-p", "-t", "--target", "--prefix", "--root",
           "--progress-bar", "--timeout", "--retries", "--cache-dir", "--platform", "--python-version", "--implementation",
           "--abi", "--log", "--proxy", "--cert", "--client-cert", "--src", "--exists-action", "--config-settings", "-C",
           "--python-platform", "--group", "-G", "--optional", "--extra", "-E", "--extras", "--package", "--bounds",
           "--suffix", "--prerelease", "--index-strategy", "--keyring-provider", "--link-mode", "--upgrade-package", "-P",
           "--trusted-host", "--script",
           # registry flags (interpreted below)
           "-i", "--index-url", "--default-index", "--extra-index-url", "--index", "-f", "--find-links", "--source",
           # blocking flags (reported below)
           "-r", "--requirement", "-c", "--constraint", "--override", "-e", "--editable", "--pip-args", "--spec",
           "--with", "--with-requirements", "--from"])
PIP_PRIMARY = ("-i", "--index-url", "--default-index")
PIP_EXTRA = ("--extra-index-url", "--index", "-f", "--find-links", "--no-index")
PIP_BLOCK = {"-r": "REQUIREMENTS_FILE_UNSUPPORTED", "--requirement": "REQUIREMENTS_FILE_UNSUPPORTED",
             "-c": "REQUIREMENTS_FILE_UNSUPPORTED", "--constraint": "REQUIREMENTS_FILE_UNSUPPORTED",
             "--override": "REQUIREMENTS_FILE_UNSUPPORTED", "--with-requirements": "REQUIREMENTS_FILE_UNSUPPORTED",
             "-e": "LOCAL_PATH_UNSUPPORTED", "--editable": "LOCAL_PATH_UNSUPPORTED",
             "--pip-args": "UNSUPPORTED_FLAG:--pip-args", "--source": "REGISTRY_UNSUPPORTED:named-source"}


def _python_m(cmd: str, args: list[str]) -> ParsedSource:
    a = list(args)
    while a and re.fullmatch(r"-\d(\.\d+)?(-\d+)?|-[IEsSBuq]+", a[0]):     # py -3.12, python -I
        a.pop(0)
    if len(a) >= 2 and a[0] == "-m" and a[1] == "pip":
        return _pip_like("pip", a[2:], expect_sub=("install",))
    if len(a) >= 2 and a[0] == "-m" and a[1] == "pipx":
        return _pipx(a[2:])
    if len(a) >= 2 and a[0] == "-m" and a[1] == "uv":
        return _uv(a[2:])
    return ParsedSource(codes=["SOURCE_COMMAND_UNSUPPORTED"], message=f"`{cmd}` is only understood as `{cmd} -m pip install ...`")


def _uv(args: list[str]) -> ParsedSource:
    if args[:2] == ["pip", "install"]:
        return _pip_like("uv", args[2:], expect_sub=())
    if args[:1] == ["add"]:
        return _pip_like("uv", args[1:], expect_sub=())
    if args[:2] == ["tool", "install"]:
        return _pip_like("uv", args[2:], expect_sub=())
    if args[:2] == ["tool", "run"]:
        return _exec_pypi("uvx", args[2:])
    return ParsedSource(codes=["SOURCE_COMMAND_UNSUPPORTED"], message="uv is understood as `uv pip install`, `uv add`, `uv tool install|run`")


def _pipx(args: list[str]) -> ParsedSource:
    if args[:1] == ["install"]:
        return _pip_like("pipx", args[1:], expect_sub=())
    if args[:1] == ["run"]:
        return _exec_pypi("pipx", args[1:])
    return ParsedSource(codes=["SOURCE_COMMAND_UNSUPPORTED"], message="pipx is understood as `pipx install` / `pipx run`")


def _exec_pypi(tool: str, args: list[str]) -> ParsedSource:
    """uvx / pipx run: the first positional (or --from / --spec) is the package; everything after it is the program's."""
    pos, seen, codes, _ = walk(args, PIP_FLAGS, stop_at_first_positional=True)
    spec = _val(seen, "--from", "--spec") or (pos[0] if pos else None)
    out = ParsedSource(tool=tool)
    if codes:
        out.codes += codes
        return out
    if not spec:
        out.codes.append("SOURCE_NO_PACKAGE")
        return out
    subj = _pypi_spec(spec, tool)
    subj.executes_code = True
    subj.notes.append("EXECUTES_ON_INSTALL")
    _pip_registry(subj, seen)
    if "@" in spec and "://" not in spec and not spec.startswith("git+") and " @ " not in spec:
        name, _, ver = spec.partition("@")                              # uvx pkg@1.2 / pkg@latest
        subj = _pypi_spec(name if ver == "latest" else f"{name}=={ver}", tool)
        subj.executes_code = True
        subj.notes.append("EXECUTES_ON_INSTALL")
        _pip_registry(subj, seen)
    out.subjects.append(subj)
    return out


def _pip_like(tool: str, args: list[str], expect_sub: tuple[str, ...]) -> ParsedSource:
    out = ParsedSource(tool=tool)
    a = list(args)
    if expect_sub:
        if not a or a[0] not in expect_sub:
            out.codes.append("SOURCE_COMMAND_UNSUPPORTED")
            out.message = f"`{tool}` is understood as `{tool} {expect_sub[0]} <package>`"
            return out
        a = a[1:]
    pos, seen, codes, rest = walk(a, PIP_FLAGS)
    pos += rest
    blocking = [code for f, code in PIP_BLOCK.items() if f in seen]
    if "--no-index" in a:
        blocking.append("REGISTRY_AMBIGUOUS")
    out.codes += codes + sorted(set(blocking))
    if out.codes:
        return out
    for p in pos:
        subj = _pypi_spec(p, tool)
        if subj.ecosystem == "pypi":
            _pip_registry(subj, seen)
            subj.prerelease_ok = _has(seen, "--pre", "--allow-prereleases", "--prerelease=allow") or _val(seen, "--prerelease") == "allow"
            if _has(seen, "--no-deps"):
                subj.notes.append("NO_DEPS")
        out.subjects.append(subj)
    return out


def _pip_registry(subj: Subject, seen: dict) -> None:
    if any(f in seen for f in PIP_EXTRA):
        subj.codes.append("REGISTRY_AMBIGUOUS")
    primary = _val(seen, *PIP_PRIMARY)
    if primary:
        subj.registry, subj.registry_basis = primary, "command flag"
    if _val(seen, "--trusted-host"):
        subj.notes.append("TLS_VERIFICATION_DISABLED_FOR_HOST")


def _poetry_constraint(spec: str) -> str | None:
    """Poetry constraint syntax -> PEP 440 (only the forms with an exact translation)."""
    s = spec.strip()
    m = re.fullmatch(r"\^(\d+)(?:\.(\d+))?(?:\.(\d+))?", s)
    if m:
        parts = [int(x) if x is not None else None for x in m.groups()]
        major, minor, patch = parts[0], parts[1] or 0, parts[2] or 0
        if major > 0:
            upper = f"{major + 1}.0.0"
        elif minor > 0 or parts[1] is not None and parts[2] is None and minor > 0:
            upper = f"0.{minor + 1}.0"
        elif parts[2] is not None:
            upper = f"0.0.{patch + 1}"
        else:
            upper = f"0.{(parts[1] or 0) + 1}.0" if parts[1] is not None else "1.0.0"
        return f">={major}.{minor}.{patch},<{upper}"
    m = re.fullmatch(r"~(\d+)(?:\.(\d+))?(?:\.(\d+))?", s)
    if m:
        major, minor, patch = int(m[1]), int(m[2] or 0), int(m[3] or 0)
        upper = f"{major}.{minor + 1}.0" if m[2] is not None else f"{major + 1}.0.0"
        return f">={major}.{minor}.{patch},<{upper}"
    if re.fullmatch(r"\d+(\.\d+)*([a-z]+\d*)?", s):
        return f"=={s}"
    if re.fullmatch(r"(==|>=|<=|!=|~=|>|<|===)\s*\S+(\s*,\s*(==|>=|<=|!=|~=|>|<)\s*\S+)*", s):
        return s.replace(" ", "")
    return None


def _pypi_spec(tok: str, tool: str) -> Subject:
    t = tok.strip()
    if t.lower().startswith("git+"):
        return _git_subject(t, t, "INSTALLS_FROM_GIT")
    if re.match(r"^https?://", t, re.I):
        s = _subject(t, "url", url=t)
        s.notes.append("INSTALLS_FROM_URL")
        return s
    if " @ " in t or re.match(r"^[\w.\[\],-]+\s*@\s*(git\+|https?://)", t):
        ref = t.split("@", 1)[1].strip()
        return _pypi_spec(ref, tool)
    if _is_local_path(t):
        return _subject(t, "pypi", codes=["LOCAL_PATH_UNSUPPORTED"])
    if tool == "poetry" and "@" in t:
        name, _, spec = t.partition("@")
        pep = None if spec in ("latest", "*", "") else _poetry_constraint(spec)
        if spec not in ("latest", "*", "") and pep is None:
            return _subject(t, "pypi", name=name, version_spec=spec, codes=["VERSION_RANGE_UNSUPPORTED"])
        t = name + (pep or "")
    from packaging.requirements import InvalidRequirement, Requirement
    try:
        req = Requirement(t)
    except InvalidRequirement:
        return _subject(t, "pypi", codes=["INVALID_PACKAGE_SPEC"])
    if req.url:
        if re.match(r"^(git\+)?https?://", req.url, re.I):
            return _pypi_spec(req.url, tool)
        return _subject(t, "pypi", codes=["INVALID_PACKAGE_SPEC"])
    s = _subject(t, "pypi", name=req.name, version_spec=str(req.specifier) or None)
    if req.extras:
        s.notes.append("EXTRAS_IGNORED:" + ",".join(sorted(req.extras)))
    if req.marker:
        s.notes.append("MARKER_IGNORED")
    return s


# ------------------------------------------------------------------------------------------------------------- npm
NPM_FLAGS = Flags(
    boolean=["-g", "--global", "-D", "--save-dev", "-S", "--save", "-E", "--save-exact", "-O", "--save-optional", "-P",
             "--save-prod", "--save-peer", "--save-bundle", "-B", "--no-save", "--force", "-f", "--legacy-peer-deps",
             "--strict-peer-deps", "--ignore-scripts", "--no-audit", "--no-fund", "--no-package-lock", "--prefer-offline",
             "--prefer-online", "--silent", "-s", "--dev", "--exact", "--tilde", "-T", "--peer", "-W",
             "--ignore-workspace-root-check", "--no-optional", "--production", "--prod", "--verbose", "-q", "--quiet",
             "--yes", "-y", "--no", "--optional", "--trust", "--global-dir", "--no-lockfile", "--pure-lockfile",
             "--install-links", "--foreground-scripts", "--no-progress", "--frozen", "--recursive", "-r"],
    value=["--omit", "--include", "--loglevel", "--prefix", "--location", "--cache", "--tag", "--workspace", "--filter",
           "--cwd", "--modules-folder", "--install-strategy", "--registry", "-p", "--package", "-c", "--call",
           "--userconfig", "--globalconfig", "--save-catalog-name", "--network-timeout", "--mutex", "--backend"])
NPM_BLOCK = {"--userconfig": "UNSUPPORTED_FLAG:--userconfig", "--globalconfig": "UNSUPPORTED_FLAG:--globalconfig",
             "-c": "UNSUPPORTED_FLAG:-c", "--call": "UNSUPPORTED_FLAG:--call"}
NPM_INSTALL_SUBS = {"install", "i", "add", "in", "ins", "inst", "insta", "instal", "isnt", "isnta", "isntal", "isntall", "a"}
NPM_NAME_RE = re.compile(r"^(?:@[a-z0-9~][a-z0-9._~-]*/)?[a-z0-9~][a-z0-9._~-]*$", re.I)


def _npm_family(cmd: str, args: list[str]) -> ParsedSource:
    a = list(args)
    if cmd == "yarn" and a[:1] == ["global"]:
        a = a[1:]
    if not a:
        return ParsedSource(tool=cmd, codes=["LOCKFILE_INSTALL_UNSUPPORTED"], message="installing from the lockfile names no package")
    sub = a[0]
    if (cmd, sub) in (("npm", "exec"), ("npm", "x"), ("pnpm", "dlx"), ("yarn", "dlx"), ("bun", "x")):
        return _npm_exec(cmd, a[1:])
    if sub == "ci" or (sub not in NPM_INSTALL_SUBS):
        return ParsedSource(tool=cmd, codes=["SOURCE_COMMAND_UNSUPPORTED" if sub != "ci" else "LOCKFILE_INSTALL_UNSUPPORTED"],
                            message=f"`{cmd} {sub}` is not an install-with-package command")
    pos, seen, codes, rest = walk(_split_scope_registry(a[1:]), NPM_FLAGS)
    pos += rest
    out = ParsedSource(tool=cmd)
    out.codes += codes + [c for f, c in NPM_BLOCK.items() if f in seen]
    if cmd == "npm" and "-w" in a[1:]:
        out.codes.append("UNSUPPORTED_FLAG:-w")
    if out.codes:
        return out
    if not pos:
        out.codes.append("LOCKFILE_INSTALL_UNSUPPORTED")
        return out
    for p in pos:
        out.subjects.append(_npm_spec(p, seen))
    return out


def _split_scope_registry(args: list[str]) -> list[str]:
    """--@scope:registry=URL -> --registry@scope URL (kept apart from the global --registry)."""
    out = []
    for a in args:
        m = re.fullmatch(r"--(@[\w.-]+):registry=(.+)", a)
        out += ["--registry", f"{m.group(1)}={m.group(2)}"] if m else [a]
    return out


def _npm_registry(subj: Subject, seen: dict) -> None:
    for v in seen.get("--registry", []):
        scope, eq, url = v.partition("=")
        if eq and scope.startswith("@"):
            if (subj.name or "").startswith(scope + "/"):
                subj.registry, subj.registry_basis = url, "command flag"
        elif not eq:
            subj.registry, subj.registry_basis = v, "command flag"


def _npm_spec(tok: str, seen: dict) -> Subject:
    t = tok.strip()
    low = t.lower()
    if low.startswith(("git+", "git://", "github:", "gitlab:", "bitbucket:", "gist:")):
        if low.startswith(("github:", "gitlab:", "bitbucket:")):
            host = {"github": "github.com", "gitlab": "gitlab.com", "bitbucket": "bitbucket.org"}[low.split(":")[0]]
            t2 = f"https://{host}/{t.split(':', 1)[1]}"
            return _git_subject(t, t2, "INSTALLS_FROM_GIT")
        if low.startswith("gist:"):
            return _subject(t, "git", codes=["GIT_REMOTE_UNSUPPORTED"])
        return _git_subject(t, t, "INSTALLS_FROM_GIT")
    if re.match(r"^https?://", t, re.I):
        s = _subject(t, "url", url=t)
        s.notes.append("INSTALLS_FROM_URL")
        return s
    if _is_local_path(t):
        return _subject(t, "npm", codes=["LOCAL_PATH_UNSUPPORTED"])
    m = re.fullmatch(r"([^@/]+)@npm:(.+)", t) or re.fullmatch(r"(@[^/]+/[^@]+)@npm:(.+)", t)
    alias = None
    if m:
        alias, t = m.group(1), m.group(2)
    if re.fullmatch(r"[\w.-]+/[\w.-]+(#.+)?", t) and not t.startswith("@"):      # user/repo -> GitHub
        return _git_subject(tok, f"https://github.com/{t}", "GITHUB_SHORTHAND")
    at = t.rfind("@")
    name, spec = (t[:at], t[at + 1:]) if at > 0 else (t, "")
    if not NPM_NAME_RE.match(name):
        return _subject(tok, "npm", codes=["INVALID_PACKAGE_SPEC"])
    s = _subject(tok, "npm", name=name, version_spec=spec or _val(seen, "--tag") or None)
    if alias:
        s.notes.append(f"NPM_ALIAS:{alias}")
    _npm_registry(s, seen)
    return s


def _npm_exec(cmd: str, args: list[str]) -> ParsedSource:
    pos, seen, codes, _ = walk(_split_scope_registry(args), NPM_FLAGS, stop_at_first_positional=True)
    out = ParsedSource(tool=cmd)
    out.codes += codes + [c for f, c in NPM_BLOCK.items() if f in seen]
    if out.codes:
        return out
    specs = seen.get("-p", []) + seen.get("--package", []) or pos[:1]
    if not specs:
        out.codes.append("SOURCE_NO_PACKAGE")
        return out
    for sp in specs:
        s = _npm_spec(sp, seen)
        s.executes_code = True
        s.notes.append("EXECUTES_ON_INSTALL")
        out.subjects.append(s)
    return out


# ----------------------------------------------------------------------------------------------------------- NuGet
DOTNET_FLAGS = Flags(boolean=["--prerelease", "-n", "--no-restore", "--interactive", "-g", "--global", "--local",
                              "--allow-downgrade", "--create-manifest-if-needed", "--disable-parallel", "--ignore-failed-sources",
                              "--no-cache", "--allow-roll-forward"],
                     value=["-v", "--version", "-s", "--source", "-f", "--framework", "--package-directory", "--project",
                            "--tool-path", "--add-source", "--configfile", "--verbosity", "-a", "--arch"])
NUGET_FLAGS = Flags(boolean=["-prerelease", "-excludeversion", "-x", "-noninteractive", "-nocache", "-directdownload",
                             "-disableparallelprocessing", "-force", "-allowprereleaseversions", "-includeprerelease",
                             "-skipdependencies", "-acceptlicense", "-whatif", "-noclobber", "-allowclobber"],
                    value=["-version", "-requiredversion", "-minimumversion", "-maximumversion", "-source", "-outputdirectory",
                           "-verbosity", "-dependencyversion", "-framework", "-configfile", "-providername", "-scope",
                           "-projectname", "-name", "-destination"], ci=True)


def _nuget_subject(inp: str, pkg: str, version: str | None, source: str | None, prerelease: bool) -> Subject:
    if "@" in pkg:
        pkg, _, v2 = pkg.partition("@")
        version = version or v2
    s = _subject(inp, "nuget", name=pkg, version_spec=version, prerelease_ok=prerelease)
    if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]*", pkg):
        s.codes.append("INVALID_PACKAGE_SPEC")
    if source:
        s.registry, s.registry_basis = source, "command flag"
    return s


def _dotnet(args: list[str]) -> ParsedSource:
    a = list(args)
    out = ParsedSource(tool="dotnet")
    if a[:2] == ["package", "add"]:
        a = a[2:]
    elif a[:1] == ["add"] and "package" in a[1:3]:
        a = a[a.index("package") + 1:]
    elif a[:2] == ["tool", "install"]:
        a = a[2:]
    else:
        out.codes.append("SOURCE_COMMAND_UNSUPPORTED")
        out.message = "dotnet is understood as `dotnet add package`, `dotnet package add`, `dotnet tool install`"
        return out
    pos, seen, codes, rest = walk(a, DOTNET_FLAGS)
    out.codes += codes
    if "--add-source" in seen:
        out.codes.append("REGISTRY_AMBIGUOUS")
    if "--configfile" in seen:
        out.codes.append("UNSUPPORTED_FLAG:--configfile")
    if out.codes:
        return out
    if len(pos) != 1:
        out.codes.append("SOURCE_NO_PACKAGE" if not pos else "SOURCE_MULTIPLE_PACKAGES_UNSUPPORTED")
        return out
    out.subjects.append(_nuget_subject(" ".join(["dotnet"] + args), pos[0], _val(seen, "-v", "--version"),
                                       _val(seen, "-s", "--source"), _has(seen, "--prerelease")))
    return out


def _nuget_exe(args: list[str]) -> ParsedSource:
    out = ParsedSource(tool="nuget")
    if not args or args[0].lower() != "install":
        out.codes.append("SOURCE_COMMAND_UNSUPPORTED")
        return out
    return _nuget_common("nuget", args[1:], args)


def _install_package(args: list[str]) -> ParsedSource:
    return _nuget_common("install-package", args, args)


def _nuget_common(tool: str, a: list[str], raw: list[str]) -> ParsedSource:
    out = ParsedSource(tool=tool)
    pos, seen, codes, rest = walk(a, NUGET_FLAGS)
    out.codes += codes
    prov = _val(seen, "-providername")
    if prov and prov.lower() != "nuget":
        out.codes.append(f"ECOSYSTEM_NOT_YET_VERIFIED:{prov.lower()}")
    if _has(seen, "-minimumversion", "-maximumversion"):
        out.codes.append("VERSION_RANGE_UNSUPPORTED")
    if "-configfile" in seen:
        out.codes.append("UNSUPPORTED_FLAG:-ConfigFile")
    if out.codes:
        return out
    names = ([_val(seen, "-name")] if _val(seen, "-name") else []) + pos
    if len(names) != 1:
        out.codes.append("SOURCE_NO_PACKAGE" if not names else "SOURCE_MULTIPLE_PACKAGES_UNSUPPORTED")
        return out
    out.subjects.append(_nuget_subject(" ".join([tool] + raw), names[0], _val(seen, "-version", "-requiredversion"),
                                       _val(seen, "-source"),
                                       _has(seen, "-prerelease", "-allowprereleaseversions", "-includeprerelease")))
    return out


# ---------------------------------------------------------------------------------------------------------- WinGet
WINGET_FLAGS = Flags(
    boolean=["-e", "--exact", "-h", "--silent", "-i", "--interactive", "--accept-package-agreements",
             "--accept-source-agreements", "--force", "--disable-interactivity", "--skip-dependencies",
             "--ignore-security-hash", "--ignore-local-archive-malware-scan", "--no-upgrade", "--uninstall-previous",
             "--wait", "--verbose", "--verbose-logs", "--allow-reboot", "--open-logs"],
    value=["--id", "-q", "--query", "--name", "--moniker", "-v", "--version", "-s", "--source", "--scope", "-a",
           "--architecture", "--installer-type", "-l", "--location", "--override", "--custom", "--locale", "--log", "-o",
           "-r", "--rename", "--header", "--authentication-mode", "--authentication-account", "--dependency-source"],
    ci=True)


def _winget(args: list[str]) -> ParsedSource:
    out = ParsedSource(tool="winget")
    if not args or args[0].lower() not in ("install", "add", "in"):
        out.codes.append("SOURCE_COMMAND_UNSUPPORTED")
        out.message = "winget is understood as `winget install --id <Id>` or `winget install -e <Id>`"
        return out
    pos, seen, codes, rest = walk(args[1:], WINGET_FLAGS)
    out.codes += codes
    if out.codes:
        return out
    src = (_val(seen, "-s", "--source") or "winget").lower()
    ident = _val(seen, "--id")
    exact = _has(seen, "-e", "--exact")
    treated_as_id = False
    if not ident and len(pos) == 1 and not _has(seen, "--name", "--moniker", "-q", "--query"):
        # `winget install <query>`: when the query is an existing PackageIdentifier winget either installs exactly that
        # package or stops with "multiple packages found"; resolution confirms the manifest exists (else not exact).
        ident, treated_as_id = pos[0], not exact
    s = _subject(" ".join(["winget"] + args), "winget", name=ident, version_spec=_val(seen, "-v", "--version"))
    if treated_as_id:
        s.notes.append("WINGET_QUERY_TREATED_AS_ID")
    if src != "winget":
        s.codes.append(f"ECOSYSTEM_NOT_YET_VERIFIED:winget-{src}")
    elif not ident or (pos and _val(seen, "--id")):
        s.codes.append("WINGET_QUERY_NOT_EXACT")
    elif not re.fullmatch(r"[^\s\\/:*?\"<>|]+\.[^\s\\/:*?\"<>|]+", ident):
        s.codes.append("INVALID_PACKAGE_SPEC")
    s.options = {k: _val(seen, *v) for k, v in {"architecture": ("-a", "--architecture"), "scope": ("--scope",),
                                                "installer_type": ("--installer-type",), "locale": ("--locale",)}.items()
                 if _val(seen, *v)}
    if _has(seen, "--ignore-security-hash"):
        s.notes.append("HASH_CHECK_DISABLED")
    if _has(seen, "--ignore-local-archive-malware-scan"):
        s.notes.append("MALWARE_SCAN_DISABLED")
    if _has(seen, "--override", "--custom"):
        s.notes.append("INSTALLER_ARGUMENTS_OVERRIDDEN")
    out.subjects.append(s)
    return out


# ------------------------------------------------------------------------------------------------------------- git
GIT_CLONE_FLAGS = Flags(boolean=["--single-branch", "--no-single-branch", "--recursive", "--recurse-submodules",
                                 "--shallow-submodules", "--no-shallow-submodules", "-q", "--quiet", "--sparse", "-n",
                                 "--no-checkout", "--bare", "--mirror", "-v", "--verbose", "--progress", "--no-tags",
                                 "--remote-submodules", "-l", "--local", "--no-hardlinks", "--also-filter-submodules"],
                        value=["--depth", "-b", "--branch", "--filter", "-o", "--origin", "-j", "--jobs", "--shallow-since",
                               "--shallow-exclude", "--revision", "--bundle-uri", "-u", "--upload-pack", "--template",
                               "-c", "--config", "--reference", "--separate-git-dir", "--server-option"])
GIT_BLOCK = {"-c": "UNSUPPORTED_FLAG:-c", "--config": "UNSUPPORTED_FLAG:--config", "-u": "UNSUPPORTED_FLAG:-u",
             "--upload-pack": "UNSUPPORTED_FLAG:--upload-pack", "--template": "UNSUPPORTED_FLAG:--template",
             "--reference": "UNSUPPORTED_FLAG:--reference", "--bundle-uri": "UNSUPPORTED_FLAG:--bundle-uri"}


def _git(args: list[str]) -> ParsedSource:
    out = ParsedSource(tool="git")
    a = list(args)
    while a and a[0].startswith("-"):
        if a[0] in ("-c", "--config-env") or a[0].startswith(("-c", "--config-env=")):
            out.codes.append("UNSUPPORTED_FLAG:-c")        # -c url.<x>.insteadOf=... rewrites where clone goes
            return out
        if a[0] == "-C" and len(a) > 1:
            a = a[2:]
            continue
        out.codes.append(f"UNSUPPORTED_FLAG:{a[0]}")
        return out
    if not a or a[0] != "clone":
        out.codes.append("SOURCE_COMMAND_UNSUPPORTED")
        out.message = "git is understood as `git clone <remote>`"
        return out
    pos, seen, codes, rest = walk(a[1:], GIT_CLONE_FLAGS)
    pos += rest
    out.codes += codes + [c for f, c in GIT_BLOCK.items() if f in seen]
    if out.codes:
        return out
    if not pos:
        out.codes.append("SOURCE_NO_PACKAGE")
        return out
    s = _git_subject(" ".join(["git"] + args), pos[0])
    if _val(seen, "-b", "--branch", "--revision"):
        s.version_spec = _val(seen, "-b", "--branch", "--revision")
        if "GIT_REF_NOT_VERIFIED" not in s.notes:
            s.notes.append("GIT_REF_NOT_VERIFIED")
    if _has(seen, "--recursive", "--recurse-submodules"):
        s.notes.append("SUBMODULES_NOT_VERIFIED")
    out.subjects.append(s)
    return out


def _gh(args: list[str]) -> ParsedSource:
    out = ParsedSource(tool="gh")
    if args[:2] != ["repo", "clone"] or len(args) < 3:
        out.codes.append("SOURCE_COMMAND_UNSUPPORTED")
        out.message = "gh is understood as `gh repo clone <owner/repo>`"
        return out
    repo = args[2]
    ref = repo if re.match(r"^https?://|^git@", repo) else f"https://github.com/{repo}"
    if not re.match(r"^https?://|^git@", repo) and not re.fullmatch(r"[\w.-]+/[\w.-]+", repo):
        out.codes.append("INVALID_PACKAGE_SPEC")
        return out
    out.subjects.append(_git_subject(" ".join(["gh"] + args), ref))
    if "--" in args:
        sub = _git(["clone"] + args[args.index("--") + 1:] + [ref])
        if sub.codes:
            out.codes += sub.codes
    return out


# ------------------------------------------------------------------------------------------------------ Hugging Face
HF_FLAGS = Flags(boolean=["--quiet", "--force-download", "--resume-download", "--dry-run"],
                 value=["--repo-type", "--type", "--revision", "--include", "--exclude", "--local-dir", "--cache-dir",
                        "--token", "--local-dir-use-symlinks", "--max-workers", "--format"])


def _hf(cmd: str, args: list[str]) -> ParsedSource:
    out = ParsedSource(tool=cmd)
    if not args or args[0] != "download":
        out.codes.append("SOURCE_COMMAND_UNSUPPORTED")
        out.message = f"{cmd} is understood as `{cmd} download <repo_id>`"
        return out
    pos, seen, codes, rest = walk(args[1:], HF_FLAGS)
    out.codes += codes
    if out.codes:
        return out
    if not pos or not re.fullmatch(r"[\w.-]+/[\w.-]+", pos[0]):
        out.codes.append("SOURCE_NO_PACKAGE" if not pos else "INVALID_PACKAGE_SPEC")
        return out
    rtype = (_val(seen, "--repo-type", "--type") or "model").lower()
    prefix = {"model": "", "dataset": "datasets/", "space": "spaces/"}.get(rtype)
    if prefix is None:
        out.codes.append(f"UNSUPPORTED_FLAG:--repo-type={rtype}")
        return out
    s = _subject(" ".join([cmd] + args), "huggingface", name=pos[0], version_spec=_val(seen, "--revision"),
                 url=f"https://huggingface.co/{prefix}{pos[0]}")
    if s.version_spec:
        s.notes.append("GIT_REF_NOT_VERIFIED")
    out.subjects.append(s)
    return out


# ---------------------------------------------------------------------------------------------------------- Docker
DOCKER_FLAGS = Flags(
    boolean=["-a", "--all-tags", "-q", "--quiet", "--disable-content-trust", "-d", "--detach", "-i", "--interactive", "-t",
             "--tty", "--rm", "--privileged", "--init", "--read-only", "-P", "--publish-all", "--sig-proxy", "--oom-kill-disable"],
    value=["--platform", "-p", "--publish", "-v", "--volume", "-e", "--env", "--env-file", "--name", "-w", "--workdir",
           "--network", "--net", "-u", "--user", "--entrypoint", "--gpus", "--device", "--mount", "--restart", "-m", "--memory",
           "--cpus", "--shm-size", "--pull", "-h", "--hostname", "--add-host", "--label", "-l", "--cap-add", "--cap-drop",
           "--security-opt", "--ipc", "--pid", "--runtime", "--ulimit", "--log-driver", "--log-opt", "--group-add", "--tmpfs",
           "--expose", "--dns", "--stop-signal", "--health-cmd", "--cidfile", "--volumes-from", "--userns"])


def _docker(cmd: str, args: list[str]) -> ParsedSource:
    out = ParsedSource(tool=cmd)
    a = list(args)
    if a[:2] == ["image", "pull"] or a[:2] == ["container", "run"]:
        a = a[1:]
    if not a or a[0] not in ("pull", "run", "create"):
        out.codes.append("SOURCE_COMMAND_UNSUPPORTED")
        out.message = f"{cmd} is understood as `{cmd} pull|run <image>`"
        return out
    pos, seen, codes, _ = walk(a[1:], DOCKER_FLAGS, stop_at_first_positional=True)
    out.codes += codes
    if out.codes:
        return out
    if not pos:
        out.codes.append("SOURCE_NO_PACKAGE")
        return out
    s = docker_subject(" ".join([cmd] + args), pos[0])
    if a[0] in ("run", "create"):
        s.executes_code = True
        s.notes.append("EXECUTES_ON_INSTALL")
    if _has(seen, "--privileged"):
        s.notes.append("PRIVILEGED_CONTAINER")
    if _has(seen, "--disable-content-trust"):
        s.notes.append("CONTENT_TRUST_DISABLED")
    out.subjects.append(s)
    return out


def docker_subject(inp: str, ref: str) -> Subject:
    r = ref.strip()
    digest = None
    if "@" in r:
        r, _, digest = r.partition("@")
    tag = None
    last = r.rsplit("/", 1)[-1]
    if ":" in last:
        r, _, tag = r.rpartition(":")
    parts = r.split("/")
    registry = None
    if len(parts) > 1 and ("." in parts[0] or ":" in parts[0] or parts[0] == "localhost"):
        registry, parts = parts[0].lower(), parts[1:]
    s = _subject(inp, "docker", name=ref, version_spec=digest or tag)
    if not parts or not all(re.fullmatch(r"[a-z0-9]+(?:[._-][a-z0-9]+)*", p) for p in parts):
        s.codes.append("INVALID_PACKAGE_SPEC")
        return s
    if registry in (None, "docker.io", "index.docker.io", "registry-1.docker.io"):
        if len(parts) == 1 or parts[0] == "library":
            s.url = f"https://hub.docker.com/_/{parts[-1]}"
        else:
            s.url = f"https://hub.docker.com/r/{parts[0]}/{'/'.join(parts[1:])}"
        s.registry, s.registry_basis = "docker.io", "public default" if registry is None else "command flag"
    elif registry == "ghcr.io":
        s.url = f"https://github.com/{parts[0]}"
        s.registry, s.registry_basis = "ghcr.io", "command flag"
        s.notes.append("GHCR_OWNER_LEVEL")
    else:
        s.registry = registry
        s.codes.append(f"ECOSYSTEM_NOT_YET_VERIFIED:container-registry:{registry}")
    if digest:
        s.version = digest
    return s


# ------------------------------------------------------------------------------------------------------ parse-only
_PO_VALUE = {"cargo": {"--version", "--vers", "--git", "--branch", "--tag", "--rev", "--path", "--registry", "--index",
                       "--features", "-F", "--bin", "--root", "--target", "--profile", "-j", "--jobs", "--example"},
             "gem": {"-v", "--version", "-s", "--source", "-i", "--install-dir", "-n", "--bindir", "--platform"},
             "choco": {"--version", "-s", "--source", "--params", "--package-parameters", "--ia", "--install-arguments"},
             "conda": {"-c", "--channel", "-n", "--name", "-p", "--prefix"},
             "brew": set(), "scoop": set(), "composer": set(), "go": set(), "apt": {"-t", "--target-release", "-o"},
             "flatpak": {"--arch", "--branch"}, "snap": {"--channel", "--revision"}}


def _parse_only(cmd: str, args: list[str]) -> ParsedSource:
    eco = PARSE_ONLY[cmd]
    out = ParsedSource(tool=cmd)
    a = list(args)
    subs = {"crates": ("install", "add"), "go": ("install", "get"), "rubygems": ("install",), "packagist": ("require",),
            "homebrew": ("install",), "scoop": ("install",), "chocolatey": ("install",), "conda": ("install", "create"),
            "apt": ("install",), "rpm": ("install", "in"), "pacman": ("-S", "-Sy", "-Syu"), "apk": ("add",),
            "snap": ("install",), "flatpak": ("install",), "ollama": ("pull", "run"), "psgallery": ()}[eco]
    if subs:
        if not a or a[0] not in subs:
            out.codes.append("SOURCE_COMMAND_UNSUPPORTED")
            return out
        a = a[1:]
    vflags = _PO_VALUE.get(cmd if cmd in _PO_VALUE else {"mamba": "conda", "micromamba": "conda", "apt-get": "apt"}.get(cmd, ""), set())
    names, i, flagvals = [], 0, {}
    while i < len(a):
        t = a[i]
        if t.startswith("-"):
            k = t.split("=", 1)[0]
            if k in vflags:
                flagvals[k] = t.split("=", 1)[1] if "=" in t else (a[i + 1] if i + 1 < len(a) else "")
                i += 0 if "=" in t else 1
        else:
            names.append(t)
        i += 1
    if cmd == "cargo" and flagvals.get("--git"):
        s = _git_subject(" ".join([cmd] + args), flagvals["--git"], "INSTALLS_FROM_GIT")
        out.subjects.append(s)
        return out
    if cmd == "flatpak" and len(names) >= 2:
        names = names[1:]                                   # flatpak install <remote> <app-id>
    for n in names:
        s = _subject(n, eco, name=n, codes=[f"ECOSYSTEM_NOT_YET_VERIFIED:{eco}"])
        out.subjects.append(s)
    if not names:
        out.codes.append("SOURCE_NO_PACKAGE")
    return out


# ------------------------------------------------------------------------------------------------ scripts / downloads
_SCRIPT_RUNNERS = r"(?i)(?<![\w.-])(sh|bash|zsh|dash|ksh|fish|iex|invoke-expression|python3?|pwsh|powershell|node|perl|ruby|cmd|source|scriptblock)(?![\w.-])"
_PM_WORDS = r"(?i)(?<![\w.-])(pip3?|npm|pnpm|yarn|npx|uvx?|pipx|poetry|winget|choco|scoop|brew|apt(-get)?|dnf|yum|dotnet|nuget|cargo|gem|docker|podman|git|gh)(?![\w.-])"


def _parse_script(s: str) -> ParsedSource:
    urls = list(dict.fromkeys(u.rstrip(".,)\"'") for u in URL_RE.findall(s)))
    remote_other = re.findall(r"(?<![\w/])(?:git@[\w.-]+:|ssh://|ftp://)", s)
    out = ParsedSource(tool="script")
    if len(urls) != 1 or remote_other:
        out.codes.append("SOURCE_MULTIPLE_REMOTE_REFERENCES" if urls else "SOURCE_UNPARSABLE")
        out.message = ("a download command must name exactly one remote location; pass each URL in its own call"
                       if urls else "no http(s) URL found in the download command")
        return out
    rest = URL_RE.sub(" ", s)
    if re.search(_PM_WORDS, rest):
        out.codes.append("SOURCE_MIXED_COMMANDS")
        out.message = "the command mixes a download with a package manager; send them as separate calls"
        return out
    url = urls[0]
    executes = re.search(_SCRIPT_RUNNERS, rest) is not None
    subj = _subject(s, "script" if executes else "url", url=url, executes_code=executes)
    if executes:
        subj.notes += ["EXECUTES_ON_INSTALL", "SCRIPT_MAY_DOWNLOAD_MORE"]
    if re.search(r"(?i)(?<![\w-])(-k|--insecure|--no-check-certificate|-skipcertificatecheck)(?![\w-])", rest):
        subj.notes.append("TLS_VERIFICATION_DISABLED")
    out.subjects.append(subj)
    return out
