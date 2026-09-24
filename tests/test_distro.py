"""Distribution packages: deterministic decision from the package manager's own origin report (options.origin)."""
from urlverify_mcp.config import Config
from urlverify_mcp.models import Verdict
from urlverify_mcp.source import run as run_mod
from urlverify_mcp.source.distro import decide, parse_apt_policy
from urlverify_mcp.source.parse import parse_source
from urlverify_mcp.source.run import SourceRequest, verify_source
from urlverify_mcp.storage import Storage

UBUNTU = """curl:
  Installed: (none)
  Candidate: 8.5.0-2ubuntu10.6
  Version table:
     8.5.0-2ubuntu10.6 500
        500 http://tw.archive.ubuntu.com/ubuntu noble-updates/main amd64 Packages
        500 http://security.ubuntu.com/ubuntu noble-security/main amd64 Packages
     8.5.0-2ubuntu10 500
        500 http://tw.archive.ubuntu.com/ubuntu noble/main amd64 Packages
"""
DOCKER = """docker-ce:
  Installed: (none)
  Candidate: 5:27.3.1-1~ubuntu.24.04~noble
  Version table:
     5:27.3.1-1~ubuntu.24.04~noble 500
        500 https://download.docker.com/linux/ubuntu noble/stable amd64 Packages
"""
MIRROR = UBUNTU.replace("http://tw.archive.ubuntu.com/ubuntu", "http://free.nchc.org.tw/ubuntu").replace("http://security.ubuntu.com/ubuntu", "http://free.nchc.org.tw/ubuntu")
PACMAN = "Repository      : extra\nName            : firefox\nVersion         : 131.0-1\nURL             : https://www.mozilla.org/firefox/\n"
DNF_THIRD = "Name         : code\nVersion      : 1.94.0\nRepository   : code\n"


def subj(src):
    return parse_source(src).subjects[0]


def test_apt_policy_parsing():
    rep = parse_apt_policy(UBUNTU, "curl")
    assert rep["candidate"] == "8.5.0-2ubuntu10.6" and len(rep["versions"]["8.5.0-2ubuntu10.6"]) == 2
    assert parse_apt_policy(UBUNTU, "wget") is None


def test_apt_official_third_party_mirror_and_missing():
    d = decide(subj("apt install curl"), "curl", UBUNTU, [])
    assert d["verdict"] == "VERIFIED_TRUE" and "DISTRO_PACKAGE" in d["notices"]
    assert decide(subj("apt install docker-ce"), "Docker", DOCKER, []) == {"url": "https://download.docker.com/linux/ubuntu"}
    assert decide(subj("apt install curl"), "curl", MIRROR, []) == {"url": "http://free.nchc.org.tw/ubuntu"}   # unknown host: verified as a repository, not assumed
    assert decide(subj("apt install curl"), "curl", MIRROR, ["free.nchc.org.tw/ubuntu"])["verdict"] == "VERIFIED_TRUE"
    assert decide(subj("apt install curl"), "curl", None, [])["codes"] == ["DISTRO_ORIGIN_NEEDED"]
    assert decide(subj("apt install curl=1.0"), "curl", UBUNTU, [])["codes"] == ["VERSION_NOT_FOUND"]
    assert decide(subj("apt install curl"), "wget", UBUNTU, [])["codes"] == ["PROJECT_NAME_NOT_MATCHED"]


def test_pacman_and_dnf():
    assert decide(subj("pacman -S firefox"), "Firefox", PACMAN, [])["verdict"] == "VERIFIED_TRUE"
    assert decide(subj("dnf install code"), "Visual Studio Code", DNF_THIRD, [])["codes"] == ["DISTRO_REPOSITORY_NOT_OFFICIAL:code"]
    fed = "Name : htop\nVersion : 3.3.0\nRepository : updates\n"
    assert decide(subj("dnf install htop"), "htop", fed, [])["verdict"] == "VERIFIED_TRUE"


async def test_orchestration(tmp_path, monkeypatch):
    calls = []

    async def fake_verify(req, cfg, store, record=True):
        from urlverify_mcp.models import VerifyResult
        calls.append(req.url)
        return VerifyResult(verdict=Verdict.TRUE, confidence=0.9, reason="r", trace_id="s", path="full")
    import urlverify_mcp.pipeline as pipeline
    monkeypatch.setattr(pipeline, "verify", fake_verify)
    store = Storage(str(tmp_path / "s"), str(tmp_path / "l"))
    r = await verify_source(SourceRequest(project="curl", source="sudo apt install -y curl", artifact="Ubuntu package",
                                          options={"origin": UBUNTU}), Config(), store)
    assert (r.verdict, r.next_action) == (Verdict.TRUE, "PROCEED") and not calls
    r = await verify_source(SourceRequest(project="curl", source="apt install curl", artifact="x"), Config(), store)
    assert r.next_action == "FIX_INPUT_AND_RETRY" and any(n.startswith("HOWTO:run `LC_ALL=C apt-cache policy curl`") for n in r.notices)
    r = await verify_source(SourceRequest(project="Docker", source="apt install docker-ce", artifact="x", options={"origin": DOCKER}), Config(), store)
    assert calls == ["https://download.docker.com/linux/ubuntu"] and "THIRD_PARTY_REPOSITORY" in r.notices


def test_apt_policy_in_any_locale():
    zh = """curl:
  已安裝：8.5.0-2ubuntu10.13
  候選： 8.5.0-2ubuntu10.13
  版本列表：
 *** 8.5.0-2ubuntu10.13 500
        500 http://tw.archive.ubuntu.com/ubuntu noble-updates/main amd64 Packages
        500 http://security.ubuntu.com/ubuntu noble-security/main amd64 Packages
        100 /var/lib/dpkg/status
     8.5.0-2ubuntu10 500
        500 http://tw.archive.ubuntu.com/ubuntu noble/main amd64 Packages
"""
    rep = parse_apt_policy(zh, "curl")
    assert rep["candidate"] == "8.5.0-2ubuntu10.13" and rep["installed"] == "8.5.0-2ubuntu10.13"
    assert len(rep["versions"]["8.5.0-2ubuntu10.13"]) == 2
    assert decide(subj("apt install curl"), "curl", zh, [])["verdict"] == "VERIFIED_TRUE"
    none = "foo:\n  Installed: (none)\n  Candidate: (none)\n  Version table:\n"
    assert decide(subj("apt install foo"), "foo", none, [])["codes"] == ["DISTRO_PACKAGE_NOT_AVAILABLE"]
