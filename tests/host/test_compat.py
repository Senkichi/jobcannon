from jobcannon.db.compat import qmark_to_format


def test_translates_bare_qmarks():
    assert (
        qmark_to_format("SELECT jd_full FROM postings WHERE dedup_key = ?")
        == "SELECT jd_full FROM postings WHERE dedup_key = %s"
    )


def test_preserves_qmarks_inside_string_literals():
    sql = "SELECT * FROM postings WHERE title = 'what?' AND dedup_key = ?"
    assert qmark_to_format(sql) == "SELECT * FROM postings WHERE title = 'what?' AND dedup_key = %s"


def test_multiple_params():
    assert qmark_to_format("UPDATE t SET a = ?, b = ? WHERE c = ?") == "UPDATE t SET a = %s, b = %s WHERE c = %s"


def test_percent_literals_are_escaped():
    # psycopg treats bare % as a placeholder introducer when params are passed;
    # engine SQL with LIKE '%foo%' must survive translation.
    assert qmark_to_format("SELECT 1 FROM t WHERE x LIKE '%rem%' AND y = ?").count("%%rem%%") == 1
