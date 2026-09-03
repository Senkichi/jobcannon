# PORTED from tests/test_profile.py @ c82b924bf60c9059bb3b4ed29db48d8fc9ddcba8 (private job-cannon). Ledger L-0519.
"""Tests for profile schema, validation, and I/O (jobcannon/engine/profile_schema.py).

# PORT-SEAM: private's docstring also covered Profile Editor routes and
# path-unification -- both dropped from this file, see "dropped, and why"
# below.

Covers:
- validate_profile: warnings for missing achievements, unquantified impacts,
  unmatched skill tags, and valid profiles (no warnings).
- load_profile: returns empty structure when file not found.
- save_profile: writes valid JSON, empty-overwrite/suspicious-reduction guards,
  force bypass, education round-trip.
- _normalize_profile: ID minting/collisions, email->contact nesting,
  description->achievements bridging.
# PORT-SEAM: private's GET /profile / POST /profile/save bullets removed
# from this list -- see "dropped, and why" below.

Note: extract_profile_from_markdown is NOT tested here (requires live Anthropic API).

# PORT-SEAM: dropped, and why (7 of 31 private tests):
# - TestProfileEditorRoutes (3: test_get_profile_returns_200,
#   test_post_profile_save_redirects_on_success,
#   test_post_profile_save_persists_data) and
#   TestLoadSaveProfile::test_save_profile_rejects_stale_mtime /
#   test_save_profile_accepts_fresh_mtime hit GET /profile / POST
#   /profile/save via the private `client` fixture and directly monkeypatch
#   job_finder.web.blueprints.profile._profile_path -- that blueprint layer
#   is DIES (single-user-desktop; see this row's own ledger evidence).
# - TestProfilePathUnification (2: test_editor_scorer_onboarding_agree_on_path,
#   test_scorer_still_honors_explicit_config_override) asserts that THREE
#   separate path-resolvers (onboarding's job_finder.web.user_data_dirs,
#   the editor blueprint, and job_finder.web.scoring_orchestrator) agree on
#   one file path -- none of those three modules exist publicly, and the
#   premise itself doesn't hold: jobcannon/web/onboarding.py's own comment
#   states "the clerk profile domain has exactly one writer
#   (jobcannon/web/profile.py)" -- a genuine architectural divergence
#   (single multi-tenant writer vs. private's three-way desktop-app path
#   agreement), not a translation gap.
"""

import copy
import json
import os
import tempfile

import pytest

from jobcannon.engine.profile_schema import (
    _normalize_profile,
    load_profile,
    save_profile,
    validate_profile,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def valid_profile():
    """A minimal profile dict that should produce no validation warnings."""
    return {
        "positions": [
            {
                "id": "acme_corp_ds",
                "title": "Senior Data Scientist",
                "company": "Acme Corp",
                "start_date": "Jan 2022",
                "end_date": None,
                "achievements": [
                    "Increased model accuracy by 15% reducing false positive rate",
                    "Reduced pipeline latency by 40% saving $200k annually",
                ],
                "skills": ["Python", "SQL"],
            }
        ],
        "skills": ["Python", "SQL"],
        "resume_preferences": {"summary_style": "concise", "emphasis": ["causal inference"]},
    }


@pytest.fixture
def tmp_profile_path():
    """Temp file path for profile JSON (cleaned up after test)."""
    fd, path = tempfile.mkstemp(suffix=".json")
    os.close(fd)
    os.unlink(path)  # start fresh — load_profile expects non-existent or valid JSON
    yield path
    if os.path.exists(path):
        os.remove(path)


# ---------------------------------------------------------------------------
# validate_profile — warning detection tests
# ---------------------------------------------------------------------------


class TestValidateProfile:
    def test_position_with_no_achievements_raises_warning(self):
        """Position with empty achievements list should produce a warning."""
        profile = {
            "positions": [
                {
                    "title": "Analyst",
                    "company": "TestCo",
                    "start_date": "Jan 2020",
                    "end_date": None,
                    "achievements": [],
                    "skills": ["SQL"],
                }
            ],
            "skills": ["SQL"],
            "resume_preferences": {"summary_style": "", "emphasis": []},
        }
        warnings = validate_profile(profile)
        messages = [w["message"] for w in warnings]
        assert any("TestCo" in m and "no achievements" in m for m in messages)

    def test_achievement_without_quantified_impact(self):
        """Achievement with no numbers or % should produce an advisory warning."""
        profile = {
            "positions": [
                {
                    "title": "Analyst",
                    "company": "TestCo",
                    "start_date": "Jan 2020",
                    "end_date": None,
                    "achievements": ["Improved the reporting process significantly"],
                    "skills": ["SQL"],
                }
            ],
            "skills": ["SQL"],
            "resume_preferences": {"summary_style": "", "emphasis": []},
        }
        warnings = validate_profile(profile)
        messages = [w["message"] for w in warnings]
        assert any("quantified impact" in m for m in messages)

    def test_skill_in_position_not_in_top_level_skills(self):
        """Skill tag in a position that is absent from top-level skills list."""
        profile = {
            "positions": [
                {
                    "title": "Analyst",
                    "company": "TestCo",
                    "start_date": "Jan 2020",
                    "end_date": None,
                    "achievements": ["Increased revenue by 20%"],
                    "skills": ["Tableau"],  # not in top-level skills
                }
            ],
            "skills": ["Python"],  # Tableau NOT here
            "resume_preferences": {"summary_style": "", "emphasis": []},
        }
        warnings = validate_profile(profile)
        messages = [w["message"] for w in warnings]
        assert any("Tableau" in m and "not in main skills list" in m for m in messages)

    def test_valid_profile_produces_no_warnings(self, valid_profile):
        """A well-formed profile with quantified achievements should have no warnings."""
        warnings = validate_profile(valid_profile)
        assert warnings == []

    def test_position_with_no_skills_tagged(self):
        """Position with empty skills list should produce a warning."""
        profile = {
            "positions": [
                {
                    "title": "Analyst",
                    "company": "TestCo",
                    "start_date": "Jan 2020",
                    "end_date": None,
                    "achievements": ["Grew revenue by 30% YoY"],
                    "skills": [],  # empty
                }
            ],
            "skills": [],
            "resume_preferences": {"summary_style": "", "emphasis": []},
        }
        warnings = validate_profile(profile)
        messages = [w["message"] for w in warnings]
        assert any("TestCo" in m and "no skills tagged" in m for m in messages)

    def test_position_with_missing_id_produces_warning(self):
        """Position with no id field should produce a warning (id required for role_ref linkage)."""
        profile = {
            "positions": [
                {
                    "title": "Analyst",
                    "company": "TestCo",
                    "start_date": "Jan 2020",
                    "end_date": None,
                    "achievements": ["Grew revenue by 30% YoY"],
                    "skills": ["SQL"],
                }
            ],
            "skills": ["SQL"],
            "resume_preferences": {"summary_style": "", "emphasis": []},
        }
        warnings = validate_profile(profile)
        messages = [w["message"] for w in warnings]
        assert any("TestCo" in m and "no id" in m for m in messages)

    def test_position_with_empty_id_produces_warning(self):
        """Position with an empty-string id should be treated the same as missing."""
        profile = {
            "positions": [
                {
                    "id": "",
                    "title": "Analyst",
                    "company": "TestCo",
                    "start_date": "Jan 2020",
                    "end_date": None,
                    "achievements": ["Grew revenue by 30% YoY"],
                    "skills": ["SQL"],
                }
            ],
            "skills": ["SQL"],
            "resume_preferences": {"summary_style": "", "emphasis": []},
        }
        warnings = validate_profile(profile)
        messages = [w["message"] for w in warnings]
        assert any("TestCo" in m and "no id" in m for m in messages)

    def test_duplicate_position_ids_produce_warning(self):
        """Two positions sharing the same id should produce a warning naming both companies."""
        profile = {
            "positions": [
                {
                    "id": "dup_id",
                    "title": "Analyst",
                    "company": "FirstCo",
                    "start_date": "Jan 2018",
                    "end_date": "Dec 2019",
                    "achievements": ["Grew revenue by 10% YoY"],
                    "skills": ["SQL"],
                },
                {
                    "id": "dup_id",
                    "title": "Senior Analyst",
                    "company": "SecondCo",
                    "start_date": "Jan 2020",
                    "end_date": None,
                    "achievements": ["Grew revenue by 20% YoY"],
                    "skills": ["SQL"],
                },
            ],
            "skills": ["SQL"],
            "resume_preferences": {"summary_style": "", "emphasis": []},
        }
        warnings = validate_profile(profile)
        messages = [w["message"] for w in warnings]
        assert any("dup_id" in m and "FirstCo" in m and "SecondCo" in m for m in messages)


# ---------------------------------------------------------------------------
# load_profile / save_profile
# ---------------------------------------------------------------------------


class TestLoadSaveProfile:
    def test_load_profile_returns_empty_structure_when_file_missing(self, tmp_profile_path):
        """load_profile on a non-existent path returns a dict with empty positions/skills."""
        result = load_profile(tmp_profile_path)
        assert isinstance(result, dict)
        assert "positions" in result
        assert "skills" in result
        assert result["positions"] == []
        assert result["skills"] == []

    def test_save_profile_writes_valid_json(self, valid_profile, tmp_profile_path):
        """save_profile writes a JSON file that can be loaded back correctly."""
        save_profile(valid_profile, tmp_profile_path)
        assert os.path.exists(tmp_profile_path)

        with open(tmp_profile_path, encoding="utf-8") as f:
            loaded = json.load(f)

        assert loaded["positions"][0]["company"] == "Acme Corp"
        assert loaded["skills"] == ["Python", "SQL"]

    def test_save_profile_refuses_empty_overwrite(self, tmp_profile_path):
        """save_profile must NOT overwrite a populated profile with empty data.

        Steps:
        1. Write a profile with 3 positions and 5 skills.
        2. Attempt save_profile() with EMPTY_PROFILE.
        3. Assert the original file is unchanged.
        4. Assert a warning was logged (empty-overwrite guard triggered).
        """
        from jobcannon.engine.profile_schema import EMPTY_PROFILE

        # Populate the temp file with real data
        populated = {
            "positions": [
                {
                    "title": "Staff Data Scientist",
                    "company": "TechCo",
                    "start_date": "Jan 2021",
                    "end_date": None,
                    "achievements": ["Improved model accuracy by 20%"],
                    "skills": ["Python", "PyTorch"],
                },
                {
                    "title": "Senior Data Scientist",
                    "company": "DataCo",
                    "start_date": "Mar 2019",
                    "end_date": "Dec 2020",
                    "achievements": ["Reduced churn by 15%"],
                    "skills": ["SQL", "R"],
                },
                {
                    "title": "Data Scientist",
                    "company": "StartupCo",
                    "start_date": "Jun 2017",
                    "end_date": "Feb 2019",
                    "achievements": ["Built recommendation engine"],
                    "skills": ["Python"],
                },
            ],
            "skills": ["Python", "SQL", "PyTorch", "R", "Spark"],
            "resume_preferences": {"summary_style": "concise", "emphasis": ["product analytics"]},
        }
        save_profile(populated, tmp_profile_path)

        # Confirm the file was written correctly before the guard test
        assert os.path.exists(tmp_profile_path)
        with open(tmp_profile_path, encoding="utf-8") as f:
            before = json.load(f)
        assert len(before["positions"]) == 3
        assert len(before["skills"]) == 5

        # Now attempt to overwrite with empty profile — the guard must block this
        with self._capture_warning("jobcannon.engine.profile_schema") as captured_warnings:
            save_profile(EMPTY_PROFILE, tmp_profile_path)

        # File must be UNCHANGED
        with open(tmp_profile_path, encoding="utf-8") as f:
            after = json.load(f)
        assert after["positions"] == before["positions"], (
            "save_profile silently overwrote populated profile with empty data"
        )
        assert after["skills"] == before["skills"], (
            "save_profile silently wiped skills with empty data"
        )

        # A warning must have been logged
        assert len(captured_warnings) > 0, (
            "save_profile did not log a warning when blocking empty overwrite"
        )

    @staticmethod
    def _capture_warning(logger_name: str):
        """Context manager that captures log records at WARNING level from a named logger."""
        import logging
        from contextlib import contextmanager

        @contextmanager
        def _ctx():
            records = []

            class _Handler(logging.Handler):
                def emit(self, record):
                    if record.levelno >= logging.WARNING:
                        records.append(record)

            handler = _Handler()
            log = logging.getLogger(logger_name)
            log.addHandler(handler)
            original_level = log.level
            log.setLevel(logging.WARNING)
            try:
                yield records
            finally:
                log.removeHandler(handler)
                log.setLevel(original_level)

        return _ctx()

    def test_save_profile_allows_empty_to_new_file(self, tmp_profile_path):
        """save_profile must allow writing EMPTY_PROFILE to a brand-new (non-existent) file.

        The guard only blocks overwriting a POPULATED profile with empty data.
        Initial writes to a missing path must always succeed.
        """
        from jobcannon.engine.profile_schema import EMPTY_PROFILE

        # tmp_profile_path fixture already deletes the file — path doesn't exist
        assert not os.path.exists(tmp_profile_path)

        save_profile(EMPTY_PROFILE, tmp_profile_path)
        assert os.path.exists(tmp_profile_path)

        with open(tmp_profile_path, encoding="utf-8") as f:
            saved = json.load(f)
        assert saved["positions"] == []
        assert saved["skills"] == []

    def test_save_profile_allows_populated_over_populated(self, valid_profile, tmp_profile_path):
        """save_profile must allow overwriting a populated profile with another populated profile."""
        # Write initial populated profile
        save_profile(valid_profile, tmp_profile_path)

        updated = dict(valid_profile)
        updated["skills"] = ["Python", "SQL", "Spark"]

        save_profile(updated, tmp_profile_path)

        with open(tmp_profile_path, encoding="utf-8") as f:
            saved = json.load(f)
        assert saved["skills"] == ["Python", "SQL", "Spark"]

    def test_save_profile_refuses_suspicious_reduction(self, tmp_profile_path):
        """save_profile blocks saves where both positions AND skills shrink (wipe signal)."""

        populated = {
            "positions": [
                {
                    "title": "A",
                    "company": "Co1",
                    "start_date": "",
                    "end_date": None,
                    "achievements": ["x1"],
                    "skills": ["P"],
                },
                {
                    "title": "B",
                    "company": "Co2",
                    "start_date": "",
                    "end_date": None,
                    "achievements": ["x2"],
                    "skills": ["Q"],
                },
                {
                    "title": "C",
                    "company": "Co3",
                    "start_date": "",
                    "end_date": None,
                    "achievements": ["x3"],
                    "skills": ["R"],
                },
            ],
            "skills": ["P", "Q", "R", "S", "T"],
            "resume_preferences": {"summary_style": "", "emphasis": []},
        }
        save_profile(populated, tmp_profile_path)

        reduced = {
            "positions": [
                {
                    "title": "A",
                    "company": "Co1",
                    "start_date": "",
                    "end_date": None,
                    "achievements": ["x1"],
                    "skills": ["P"],
                },
            ],
            "skills": ["P", "Q"],
            "resume_preferences": {"summary_style": "", "emphasis": []},
        }

        with self._capture_warning("jobcannon.engine.profile_schema") as warnings:
            save_profile(reduced, tmp_profile_path)

        # File must be unchanged
        with open(tmp_profile_path, encoding="utf-8") as f:
            after = json.load(f)
        assert len(after["positions"]) == 3, "Suspicious reduction was not blocked"
        assert len(after["skills"]) == 5
        assert len(warnings) > 0

    def test_save_profile_allows_reduction_with_force(self, tmp_profile_path):
        """save_profile with force=True allows intentional reduction."""
        populated = {
            "positions": [
                {
                    "title": "A",
                    "company": "Co1",
                    "start_date": "",
                    "end_date": None,
                    "achievements": ["x1"],
                    "skills": ["P"],
                },
                {
                    "title": "B",
                    "company": "Co2",
                    "start_date": "",
                    "end_date": None,
                    "achievements": ["x2"],
                    "skills": ["Q"],
                },
                {
                    "title": "C",
                    "company": "Co3",
                    "start_date": "",
                    "end_date": None,
                    "achievements": ["x3"],
                    "skills": ["R"],
                },
            ],
            "skills": ["P", "Q", "R", "S", "T"],
            "resume_preferences": {"summary_style": "", "emphasis": []},
        }
        save_profile(populated, tmp_profile_path)

        reduced = {
            "positions": [
                {
                    "title": "A",
                    "company": "Co1",
                    "start_date": "",
                    "end_date": None,
                    "achievements": ["x1"],
                    "skills": ["P"],
                },
            ],
            "skills": ["P", "Q"],
            "resume_preferences": {"summary_style": "", "emphasis": []},
        }
        save_profile(reduced, tmp_profile_path, force=True)

        with open(tmp_profile_path, encoding="utf-8") as f:
            after = json.load(f)
        assert len(after["positions"]) == 1
        assert len(after["skills"]) == 2

    def test_save_profile_allows_one_dimension_reduction(self, tmp_profile_path):
        """Reducing positions but increasing skills is allowed (not suspicious)."""
        populated = {
            "positions": [
                {
                    "title": "A",
                    "company": "Co1",
                    "start_date": "",
                    "end_date": None,
                    "achievements": ["x1"],
                    "skills": ["P"],
                },
                {
                    "title": "B",
                    "company": "Co2",
                    "start_date": "",
                    "end_date": None,
                    "achievements": ["x2"],
                    "skills": ["Q"],
                },
            ],
            "skills": ["P", "Q"],
            "resume_preferences": {"summary_style": "", "emphasis": []},
        }
        save_profile(populated, tmp_profile_path)

        # Fewer positions but more skills — legitimate edit
        updated = {
            "positions": [
                {
                    "title": "A",
                    "company": "Co1",
                    "start_date": "",
                    "end_date": None,
                    "achievements": ["x1"],
                    "skills": ["P"],
                },
            ],
            "skills": ["P", "Q", "R", "S"],
            "resume_preferences": {"summary_style": "", "emphasis": []},
        }
        save_profile(updated, tmp_profile_path)

        with open(tmp_profile_path, encoding="utf-8") as f:
            after = json.load(f)
        assert len(after["positions"]) == 1
        assert len(after["skills"]) == 4

    # PORT-SEAM: test_save_profile_rejects_stale_mtime /
    # test_save_profile_accepts_fresh_mtime dropped here -- DIES blueprint
    # layer (private's client fixture + /profile/save route), see module
    # docstring.
    def test_save_profile_preserves_education(self, tmp_profile_path):
        """save_profile round-trips education data — it must not be dropped."""
        profile_with_edu = {
            "positions": [
                {
                    "title": "Data Scientist",
                    "company": "TestCo",
                    "start_date": "Jan 2020",
                    "end_date": None,
                    "achievements": ["Improved accuracy by 10%"],
                    "skills": ["Python"],
                }
            ],
            "skills": ["Python"],
            "education": [
                {"degree": "M.S. Statistics", "institution": "Stanford", "year": "2018"},
                {"degree": "B.S. Math", "institution": "MIT", "year": "2016"},
            ],
            "resume_preferences": {"summary_style": "", "emphasis": []},
        }
        save_profile(profile_with_edu, tmp_profile_path)

        loaded = load_profile(tmp_profile_path)
        assert "education" in loaded
        assert len(loaded["education"]) == 2
        assert loaded["education"][0]["degree"] == "M.S. Statistics"
        assert loaded["education"][1]["institution"] == "MIT"


# ---------------------------------------------------------------------------
# Profile Editor routes
# ---------------------------------------------------------------------------
#
# PORT-SEAM: TestProfileEditorRoutes (3 tests) and TestProfilePathUnification
# (2 tests) dropped here -- DIES blueprint layer + genuine architectural
# divergence (private's three-way path-resolver agreement vs. this host's
# single writer), see module docstring for the full explanation.

# ---------------------------------------------------------------------------
# _normalize_profile tests (I-1 SPE)
# ---------------------------------------------------------------------------


class TestNormalizeProfile:
    def test_mints_missing_position_ids(self):
        """_normalize_profile mints deterministic IDs for positions without them."""
        profile = {
            "positions": [
                {
                    "title": "Software Engineer",
                    "company": "Acme Corp",
                    "start_date": "2020-01",
                    "end_date": None,
                    "achievements": ["Built systems"],
                    "skills": ["Python"],
                }
            ],
            "skills": ["Python"],
        }

        normalized = _normalize_profile(profile)

        assert "id" in normalized["positions"][0]
        assert normalized["positions"][0]["id"] == "acme-corp-software-engineer-2020-01"

    def test_handles_id_collisions_with_suffix(self):
        """_normalize_profile adds -2, -3 suffixes for colliding IDs."""
        profile = {
            "positions": [
                {
                    "id": "acme-corp-engineer-2020",  # Pre-existing ID
                    "title": "Engineer",
                    "company": "Acme Corp",
                    "start_date": "2020",
                    "end_date": None,
                    "achievements": [],
                    "skills": [],
                },
                {
                    "title": "Engineer",
                    "company": "Acme Corp",
                    "start_date": "2020",  # Would collide
                    "end_date": None,
                    "achievements": [],
                    "skills": [],
                },
            ],
            "skills": [],
        }

        normalized = _normalize_profile(profile)

        ids = [p["id"] for p in normalized["positions"]]
        assert "acme-corp-engineer-2020" in ids
        assert "acme-corp-engineer-2020-2" in ids
        assert len(set(ids)) == 2  # All unique

    def test_nests_stray_top_level_email_into_contact(self):
        """_normalize_profile moves top-level email into contact object."""
        profile = {
            "email": "user@example.com",
            "positions": [],
            "skills": [],
        }

        normalized = _normalize_profile(profile)

        assert "email" not in normalized
        assert normalized["contact"]["email"] == "user@example.com"

    def test_bridges_description_to_achievements(self):
        """_normalize_profile splits description into achievements array."""
        profile = {
            "positions": [
                {
                    "title": "Engineer",
                    "company": "Acme",
                    "start_date": "2020",
                    "end_date": None,
                    "description": "Built system A\nImproved performance by 40%\nLed team of 5",
                    "skills": [],
                }
            ],
            "skills": [],
        }

        normalized = _normalize_profile(profile)

        assert "description" not in normalized["positions"][0]
        assert normalized["positions"][0]["achievements"] == [
            "Built system A",
            "Improved performance by 40%",
            "Led team of 5",
        ]

    def test_preserves_existing_contact_object(self):
        """_normalize_profile doesn't clobber existing contact data."""
        profile = {
            "contact": {"full_name": "Jane Doe", "email": "jane@example.com"},
            "positions": [],
            "skills": [],
        }

        normalized = _normalize_profile(profile)

        assert normalized["contact"]["full_name"] == "Jane Doe"
        assert normalized["contact"]["email"] == "jane@example.com"

    def test_ensures_contact_object_exists(self):
        """_normalize_profile always creates a contact object if missing."""
        profile = {
            "positions": [],
            "skills": [],
        }

        normalized = _normalize_profile(profile)

        assert "contact" in normalized
        assert isinstance(normalized["contact"], dict)

    def test_normalization_roundtrip_validates_with_zero_warnings(self):
        """Property test: after normalization, validate_profile produces no id/contact warnings."""
        # Parser-shaped input (no ids, no contact, description instead of achievements)
        parser_shaped = {
            "positions": [
                {
                    "title": "Senior Engineer",
                    "company": "Tech Corp",
                    "start_date": "2019-06",
                    "end_date": "2022-12",
                    "description": "Led migration to cloud\nReduced costs by 30%",
                    "skills": ["Python", "AWS"],
                }
            ],
            "skills": ["Python", "AWS", "SQL"],
            "email": "user@example.com",
        }

        normalized = _normalize_profile(parser_shaped)
        warnings = validate_profile(normalized)

        # Should have no id warnings (they were minted)
        id_warnings = [w for w in warnings if "id" in w.get("field", "").lower()]
        assert len(id_warnings) == 0, f"Expected no id warnings, got: {id_warnings}"

        # Should have no contact warnings (contact object exists)
        contact_warnings = [w for w in warnings if "contact" in w.get("field", "").lower()]
        assert len(contact_warnings) == 0, f"Expected no contact warnings, got: {contact_warnings}"

    def test_does_not_mutate_input(self):
        """_normalize_profile must not mutate the input dict (deep copy, not shallow)."""
        profile = {
            "positions": [
                {
                    "company": "Acme",
                    "title": "Eng",
                    "start_date": "2020",
                    "description": "a\nb",
                }
            ],
            "skills": [],
        }
        snapshot = copy.deepcopy(profile)
        _normalize_profile(profile)
        assert profile == snapshot, "Input profile was mutated by _normalize_profile"
