from urlverify_mcp.checks.private import is_public_ip, looks_local_hostname, non_public
from urlverify_mcp.identity.registry import _variants, norm_pypi


def test_ip_classes():
    assert not is_public_ip("127.0.0.1") and not is_public_ip("10.1.2.3") and not is_public_ip("192.168.2.60")
    assert not is_public_ip("169.254.169.254") and not is_public_ip("::1") and not is_public_ip("fe80::1") and not is_public_ip("::ffff:10.0.0.1")
    assert is_public_ip("104.18.32.1") and is_public_ip("2606:4700::1111")
    assert non_public(["8.8.8.8", "10.0.0.1"]) == ["10.0.0.1"]


def test_local_hostnames():
    assert looks_local_hostname("localhost") and looks_local_hostname("nas.local") and looks_local_hostname("printer")
    assert not looks_local_hostname("lmstudio.ai")


def test_variants_and_norm():
    v = _variants("lodash")
    assert "lodahs" in v and "lodas" in v and "lodash-js" in v and "lodash" not in v
    assert norm_pypi("Python_DateUtil") == norm_pypi("python-dateutil") == "python-dateutil"


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
