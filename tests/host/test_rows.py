def test_hybrid_row_supports_both_access_styles():
    from jobcannon.db.rows import HybridRow

    row = HybridRow(("dedup_key", "jd_full"), ("k|t", None))
    assert row["dedup_key"] == "k|t"
    assert row[0] == "k|t"
    assert row[1] is None
    assert row["jd_full"] is None
    assert list(row.keys()) == ["dedup_key", "jd_full"]
    assert len(row) == 2
