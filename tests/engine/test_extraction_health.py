from jobcannon.engine import extraction_health


def test_record_is_noop_without_recorder():
    extraction_health.set_recorder(None)
    extraction_health.record(conn=None, source="ats:greenhouse", payload="[]")  # must not raise


def test_record_forwards_to_registered_recorder():
    calls = []
    extraction_health.set_recorder(lambda **kw: calls.append(kw), min_meaningful_len=0)
    extraction_health.record(conn="CONN", source="ats:lever", payload="[1]")
    assert calls and calls[0]["source"] == "ats:lever"
    extraction_health.set_recorder(None)
