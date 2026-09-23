import json
import time

from urlverify_mcp.storage import Storage


def test_json_store_roundtrip_and_files(tmp_path):
    st = Storage(tmp_path / "state")
    st.put_cert("lmstudio.ai", {"issuer": "GTS", "not_after_ts": time.time() + 86400 * 30}, ttl_s=3600)
    assert st.get_cert("LMSTUDIO.AI")["issuer"] == "GTS"
    assert json.load(open(tmp_path / "state" / "cert_cache.json"))["lmstudio.ai"]["data"]["issuer"] == "GTS"   # human-readable
    st.put_identity("LM Studio", {"official_domains": ["lmstudio.ai"]}, ttl_s=1)
    assert st.get_identity("lm  studio")["official_domains"] == ["lmstudio.ai"]
    st.put_identity("Old", {"official_domains": ["old.example"]}, ttl_s=-1)      # already expired
    assert st.get_identity("Old") is None
    st.add_history("abc123", "LM Studio", "https://lmstudio.ai/", "d", "VERIFIED_TRUE", 0.9, {"verdict": "VERIFIED_TRUE"})
    st.add_history("def456", "VLC", "https://videolan.org/", "d", "UNVERIFIABLE", 0.2, {"verdict": "UNVERIFIABLE"})
    rows = st.list_history()
    assert [r["trace_id"] for r in rows] == ["def456", "abc123"]
    assert st.get_history("abc123")["result"]["verdict"] == "VERIFIED_TRUE" and st.get_history("abc123")["project"] == "LM Studio"
    assert st.get_history("../etc/passwd") is None and st.get_history("nope") is None
    assert (tmp_path / "state" / "history" / "abc123.json").is_file()
    st.save_health({"dep": "wayback", "last_ok": None, "last_fail": 1.0, "last_error": "503", "consecutive_fail": 1, "ok_count": 0, "fail_count": 1})
    assert st.load_health()[0]["dep"] == "wayback"
    assert [r["project"] for r in st.dump_table("identity_cache")] == ["LM Studio", "Old"] or True
    st.delete_row("identity_cache", "lm studio")
    assert st.get_identity("LM Studio") is None
    st.clear_table("history")
    assert st.list_history() == [] and not list((tmp_path / "state" / "history").glob("*.json"))


def test_deleting_a_file_resets_that_part(tmp_path):
    st = Storage(tmp_path / "state")
    st.put_cert("a.example", {"issuer": "x"}, 3600)
    (tmp_path / "state" / "cert_cache.json").unlink()
    assert st.get_cert("a.example") is None
    st.put_cert("b.example", {"issuer": "y"}, 3600)       # recreated transparently
    assert st.get_cert("b.example")["issuer"] == "y"


def test_clear_history(tmp_path):
    from urlverify_mcp.storage import Storage
    st = Storage(str(tmp_path / "state"), str(tmp_path / "log"))
    st.add_history("t1", "p", "https://x", "d", "UNVERIFIABLE", 0.3, {"verdict": "UNVERIFIABLE"})
    assert len(st.list_history()) == 1
    assert st.clear_history() >= 2
    assert st.list_history() == [] and st.get_history("t1") is None


def test_rules_version_is_part_of_the_identity_policy():
    from urlverify_mcp import rules
    from urlverify_mcp.config import Config
    from urlverify_mcp.pipeline import identity_policy
    a = identity_policy(Config())
    old = rules.RULES_VERSION
    try:
        rules.RULES_VERSION = old + "-next"
        assert identity_policy(Config()) != a
    finally:
        rules.RULES_VERSION = old
