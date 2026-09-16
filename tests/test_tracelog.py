import json

from urlverify_mcp.tracelog import TraceLog, reset_trace_id, set_trace_id


def test_disabled_writes_nothing(tmp_path):
    t = TraceLog(enabled=False, directory=str(tmp_path))
    t.log("x", a=1)
    assert not list(tmp_path.glob("*.log"))


def test_records_are_chronological_jsonl_with_trace_id(tmp_path):
    t = TraceLog(enabled=True, directory=str(tmp_path))
    tok = set_trace_id("abc123")
    try:
        t.log("mcp_request", tool="verify_source", args={"url": "https://x"})
        t.log("llm_response", thinking="I think...", content="hi")
    finally:
        reset_trace_id(tok)
    t.log("after")
    files = t.files()
    assert len(files) == 1 and files[0]["name"].startswith("full-") and files[0]["name"].endswith(".log")
    lines = [json.loads(l) for l in t.read(files[0]["name"]).splitlines()]
    assert [l["kind"] for l in lines] == ["mcp_request", "llm_response", "after"]
    assert lines[0]["trace_id"] == "abc123" and lines[1]["thinking"] == "I think..." and lines[2]["trace_id"] == "-"
    assert lines[0]["ts"] <= lines[1]["ts"] <= lines[2]["ts"]


def test_rotation_at_max_bytes(tmp_path):
    t = TraceLog(enabled=True, directory=str(tmp_path), max_bytes=10_000)
    for i in range(60):
        t.log("blob", i=i, payload="x" * 500)
    files = sorted(p.name for p in tmp_path.glob("full-*.log"))
    assert len(files) >= 3, files
    for p in tmp_path.glob("full-*.log"):
        assert p.stat().st_size < 10_000 + 700     # one record past the threshold at most
    total = sum(len(p.read_text().splitlines()) for p in tmp_path.glob("full-*.log"))
    assert total == 60


def test_toggle_at_runtime(tmp_path):
    t = TraceLog(enabled=False, directory=str(tmp_path))
    t.log("no")
    t.configure(True)
    t.log("yes")
    t.configure(False)
    t.log("no2")
    text = "".join(p.read_text() for p in tmp_path.glob("full-*.log"))
    assert '"yes"' in text and '"no"' not in text and '"no2"' not in text


def test_unserializable_payload_does_not_raise(tmp_path):
    t = TraceLog(enabled=True, directory=str(tmp_path))
    t.log("weird", obj=object(), s={1, 2})
    rec = json.loads(t.read(t.files()[0]["name"]).splitlines()[0])
    assert rec["kind"] == "weird" and rec["s"] == [1, 2]
