from urlverify_mcp.checks.private import is_public_ip, looks_local_hostname, non_public
from urlverify_mcp.identity.registry import is_security_holding, norm_pypi, pypi_latest_all_yanked


def test_ip_classes():
    assert not is_public_ip("127.0.0.1") and not is_public_ip("10.1.2.3") and not is_public_ip("192.168.2.60")
    assert not is_public_ip("169.254.169.254") and not is_public_ip("::1") and not is_public_ip("fe80::1") and not is_public_ip("::ffff:10.0.0.1")
    assert is_public_ip("104.18.32.1") and is_public_ip("2606:4700::1111")
    assert non_public(["8.8.8.8", "10.0.0.1"]) == ["10.0.0.1"]


def test_local_hostnames():
    assert looks_local_hostname("localhost") and looks_local_hostname("nas.local") and looks_local_hostname("printer")
    assert not looks_local_hostname("lmstudio.ai")


def test_registry_states_and_norm():
    assert norm_pypi("Python_DateUtil") == norm_pypi("python-dateutil") == "python-dateutil"
    assert is_security_holding({"dist-tags": {"latest": "0.0.1-security"}, "description": "security holding package"})
    assert is_security_holding({"dist-tags": {"latest": "1.2.3"}, "description": "This is a security holding package."})
    assert not is_security_holding({"dist-tags": {"latest": "4.17.21"}, "description": "Lodash modular utilities."})
    assert pypi_latest_all_yanked({"info": {"version": "2.0"}, "releases": {"2.0": [{"yanked": True}, {"yanked": True}]}})
    assert not pypi_latest_all_yanked({"info": {"version": "2.0"}, "releases": {"2.0": [{"yanked": True}, {"yanked": False}]}})
    assert not pypi_latest_all_yanked({"info": {"version": "2.0"}, "releases": {}})


def test_manifest_names_parse_properly():
    from urlverify_mcp.identity.registry import _manifest_names
    assert _manifest_names("pyproject.toml", '[tool.towncrier]\nname = "Data updates"\n[project]\nname = "python-dateutil"\n') == ["python-dateutil"]
    assert _manifest_names("pyproject.toml", '[tool.poetry]\nname = "pypdf"\n') == ["pypdf"]
    assert _manifest_names("setup.cfg", "[metadata]\nname = python-dateutil\nversion = 1\n") == ["python-dateutil"]
    assert _manifest_names("setup.py", 'setup(name="requests", version="2")') == ["requests"]
    assert _manifest_names("package.json", '{"name": "lodash", "version": "4"}') == ["lodash"]
    assert _manifest_names("pyproject.toml", "not toml at all name = 'x'") == ["x"]   # regex fallback


def test_project_name_match():
    from urlverify_mcp.identity.registry import _names_match
    assert _names_match("requests", "requests") and _names_match("LM Studio", "lmstudio") and _names_match("pypdf", "PyPDF")
    assert _names_match("dateutil", "python-dateutil")
    assert not _names_match("requests", "reqests-utils") and not _names_match("numpy", "pandas")


def test_provenance_repo_parsing():
    from urlverify_mcp.identity.provenance import _owner_repo
    assert _owner_repo("https://github.com/electron/asar") == ("electron", "asar")
    assert _owner_repo("git+https://github.com/psf/requests.git") == ("psf", "requests")
    assert _owner_repo("https://github.com/py-pdf/pypdf/.github/workflows/x.yml") == ("py-pdf", "pypdf")
    assert _owner_repo("https://gitlab.com/x/y") is None and _owner_repo(None) is None


def test_sct_detection_and_doh_assessment():
    from urlverify_mcp.checks.tls import SCT_OID_DER, has_embedded_scts
    assert has_embedded_scts(b"\x30\x82" + SCT_OID_DER + b"\x04\x00") and not has_embedded_scts(b"\x30\x82\x01\x00")
    from urlverify_mcp.checks.doh import assess
    assert assess(["1.1.1.1"], ["1.1.1.1", "2.2.2.2"], True, None)[0] == "pass"
    st, fatal, _ = assess(["9.9.9.9"], ["1.1.1.1"], False, True)
    assert st == "fail" and fatal
    assert assess(["9.9.9.9"], ["1.1.1.1"], True, True)[0] == "warn"
    assert assess(["9.9.9.9"], [], True, None)[0] == "skip"
