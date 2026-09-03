# PORTED from job_finder/web/profile_schema.py @ c82b924bf60c9059bb3b4ed29db48d8fc9ddcba8 (private job-cannon). Ledger L-0231.
"""Profile schema definition, validation, and I/O utilities.

Provides:
    PROFILE_SCHEMA  -- Reference dict documenting expected experience_profile.json structure
    validate_profile(profile) -> list[dict]   -- Returns list of warning dicts
    load_profile(path) -> dict                -- Load JSON file (returns empty structure if missing)
    save_profile(profile, path) -> None       -- Write JSON file with indent=2 (with empty-overwrite guard)
    _normalize_profile(profile) -> dict     -- Normalize profile to match PROFILE_SCHEMA (internal)
"""

import copy
import json
import logging
import os
import re
import unicodedata
from pathlib import Path

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Schema reference
# ---------------------------------------------------------------------------

PROFILE_SCHEMA = {
    "contact": {
        "full_name": "str",
        "email": "str",
        "phone": "str",
        "linkedin": "str",
        "github": "str",
        "portfolio": "str",
        "location": "str",
    },
    "positions": [
        {
            "id": "str (unique identifier, referenced by experience_bank.json role_ref)",
            "title": "str",
            "company": "str",
            "start_date": "str",
            "end_date": "str or null",
            "achievements": ["str"],
            "skills": ["str"],
            "title_variants": ["str (optional alternate truthful titles)"],
        }
    ],
    "skills": ["str (ordered by priority)"],
    "education": ["dict (opaque passthrough — no form UI, preserved on save)"],
}

# ---------------------------------------------------------------------------
# Empty / default profile structure
# ---------------------------------------------------------------------------

EMPTY_PROFILE = {
    "contact": {},
    "positions": [],
    "skills": [],
    "education": [],
}

# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def validate_profile(profile: dict) -> list:
    """Validate a profile dict and return advisory warnings.

    Args:
        profile: Profile dict matching PROFILE_SCHEMA.

    Returns:
        List of warning dicts, each with keys: {field, message, severity}.
        Empty list means the profile is valid.
    """
    warnings = []
    positions = profile.get("positions", [])
    top_level_skills = set(profile.get("skills", []))
    seen_ids: dict[str, str] = {}

    for position in positions:
        company = position.get("company", "unknown")
        achievements = position.get("achievements", [])
        skills = position.get("skills", [])
        position_id = position.get("id")

        # Check: missing or empty id (required for experience_bank.json role_ref linkage)
        if not position_id:
            warnings.append(
                {
                    "field": f"positions[{company}].id",
                    "message": (
                        f"Position at {company} has no id; id is required for "
                        "experience_bank.json role_ref linkage"
                    ),
                    "severity": "warning",
                }
            )
        else:
            # Check: duplicate id across positions
            if position_id in seen_ids:
                warnings.append(
                    {
                        "field": f"positions[{company}].id",
                        "message": (
                            f"Duplicate position id '{position_id}' used by both "
                            f"{seen_ids[position_id]} and {company}"
                        ),
                        "severity": "warning",
                    }
                )
            else:
                seen_ids[position_id] = company

        # Check: no achievements
        if not achievements:
            warnings.append(
                {
                    "field": f"positions[{company}].achievements",
                    "message": f"Position at {company} has no achievements",
                    "severity": "warning",
                }
            )

        # Check: achievement without quantified impact (no numbers or %)
        for achievement in achievements:
            has_number = bool(re.search(r"\d+(?:[,\.]\d+)?[%x]?|\d+x", achievement))
            if not has_number:
                short = achievement[:50] + "..." if len(achievement) > 50 else achievement
                warnings.append(
                    {
                        "field": f"positions[{company}].achievements",
                        "message": f"Achievement lacks quantified impact: '{short}'",
                        "severity": "info",
                    }
                )

        # Check: skills in position not present in top-level skills list
        for skill in skills:
            if skill and skill not in top_level_skills:
                warnings.append(
                    {
                        "field": f"positions[{company}].skills",
                        "message": f"Skill '{skill}' in {company} position not in main skills list",
                        "severity": "info",
                    }
                )

        # Check: no skills tagged on position
        if not skills:
            warnings.append(
                {
                    "field": f"positions[{company}].skills",
                    "message": f"Position at {company} has no skills tagged",
                    "severity": "warning",
                }
            )

    return warnings


# ---------------------------------------------------------------------------
# Normalization helpers
# ---------------------------------------------------------------------------


def _slugify(text: str) -> str:
    """Convert text to a URL-friendly slug.

    Args:
        text: Input string to slugify.

    Returns:
        Lowercase, alphanumeric slug with hyphens replacing spaces/special chars.
    """
    # Normalize unicode to NFKD form (decompose accented chars)
    normalized = unicodedata.normalize("NFKD", text)
    # Remove non-ASCII characters
    ascii_only = normalized.encode("ascii", "ignore").decode("ascii")
    # Convert to lowercase and replace non-alphanumeric with hyphens
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_only.lower()).strip("-")
    return slug


def _mint_position_id(company: str, title: str, start_date: str, existing_ids: set[str]) -> str:
    """Mint a deterministic position ID from company, title, and start_date.

    Args:
        company: Company name.
        title: Job title.
        start_date: Start date string.
        existing_ids: Set of already-used IDs for collision detection.

    Returns:
        A unique ID string (slug of company+title+start, with -2, -3 suffix if collision).
    """
    base_slug = _slugify(f"{company}-{title}-{start_date}")
    if not base_slug:
        base_slug = "position"

    # If no collision, return base slug
    if base_slug not in existing_ids:
        return base_slug

    # Handle collisions with -2, -3, ... suffix
    counter = 2
    while f"{base_slug}-{counter}" in existing_ids:
        counter += 1
    return f"{base_slug}-{counter}"


def _normalize_profile(profile: dict) -> dict:
    """Normalize a profile dict to match PROFILE_SCHEMA before write.

    This is the single-point-of-enforcement (SPE) for profile shape invariants.
    All writers (onboarding, Profile Editor, future importers) must go through
    save_profile(), which calls this normalization.

    Normalizations applied:
    1. Mint missing position IDs (deterministic slug of company+title+start; collision → -2 suffix)
    2. Nest stray top-level email into contact object
    3. Bridge description → achievements (newline-split)
    4. Accept legacy contact.location on read (I-5: rename to home_location in schema, but accept both)

    Args:
        profile: Incoming profile dict (may be parser-shaped or editor-shaped).

    Returns:
        Normalized profile dict matching PROFILE_SCHEMA.
    """
    normalized = copy.deepcopy(profile)  # Deep copy to avoid mutating input (nested dicts/lists)
    seen_ids: set[str] = set()

    # 1. Mint missing position IDs
    positions = normalized.get("positions", [])
    for position in positions:
        if not position.get("id"):
            company = position.get("company", "unknown")
            title = position.get("title", "unknown")
            start_date = position.get("start_date", "unknown")
            new_id = _mint_position_id(company, title, start_date, seen_ids)
            position["id"] = new_id
            seen_ids.add(new_id)
        else:
            seen_ids.add(position["id"])

        # 3. Bridge description → achievements (newline-split)
        if "description" in position and "achievements" not in position:
            description = position["description"]
            if description:
                # Split on newlines and filter empty strings
                achievements = [line.strip() for line in description.split("\n") if line.strip()]
                position["achievements"] = achievements
            # Remove the description field after bridging
            del position["description"]

    # 2. Nest stray top-level email into contact object
    if "email" in normalized and "contact" not in normalized:
        normalized["contact"] = {"email": normalized["email"]}
        del normalized["email"]
    elif "email" in normalized and isinstance(normalized.get("contact"), dict):
        # If both exist, email takes precedence in contact
        normalized["contact"]["email"] = normalized["email"]
        del normalized["email"]

    # Ensure contact object exists
    if "contact" not in normalized:
        normalized["contact"] = {}

    # 4. Accept legacy contact.location on read (I-5 bridge)
    # The schema will eventually rename this to home_location, but we accept both for now
    # This is a no-op for now - the actual rename is a separate I-5 task

    return normalized


# ---------------------------------------------------------------------------
# File I/O
# ---------------------------------------------------------------------------


def load_profile(profile_path: str = "experience_profile.json") -> dict:
    """Load the experience profile from a JSON file.

    Args:
        profile_path: Path to the profile JSON file.

    Returns:
        Profile dict, or empty structure if file doesn't exist.
    """
    path = Path(profile_path)
    if not path.exists():
        return dict(EMPTY_PROFILE)

    with open(path, encoding="utf-8") as f:
        try:
            return json.load(f)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid JSON in profile file {path}: {exc}") from exc


def save_profile(
    profile: dict, profile_path: str = "experience_profile.json", *, force: bool = False
) -> None:
    """Save the experience profile to a JSON file.

    Safety guards (skipped when *force=True*):
    1. Refuses to overwrite a populated profile with an empty one (0 positions AND 0 skills).
    2. Refuses "suspicious reduction" — incoming has strictly fewer positions AND strictly
       fewer skills than existing — which signals an accidental wipe rather than intentional edit.

    Normalization (always applied, even with force=True):
    - Mint missing position IDs (deterministic slug of company+title+start; collision → -2 suffix)
    - Nest stray top-level email into contact object
    - Bridge description → achievements (newline-split)
    - Accept legacy contact.location on read

    Args:
        profile: Profile dict to save.
        profile_path: Path to write the profile JSON file.
        force: When True, bypass safety guards. Use for explicit user-initiated saves.
    """
    path = Path(profile_path)

    # Normalize profile to match PROFILE_SCHEMA before any validation or write
    normalized = _normalize_profile(profile)

    incoming_positions = normalized.get("positions", [])
    incoming_skills = normalized.get("skills", [])

    if not force and path.exists():
        try:
            with open(path, encoding="utf-8") as f:
                existing = json.load(f)
            existing_positions = existing.get("positions", [])
            existing_skills = existing.get("skills", [])
        except (json.JSONDecodeError, ValueError):
            existing_positions = []
            existing_skills = []

        existing_has_data = len(existing_positions) > 0 or len(existing_skills) > 0

        # Guard 1: completely empty incoming over populated existing
        if existing_has_data and len(incoming_positions) == 0 and len(incoming_skills) == 0:
            logger.warning(
                "save_profile: refusing to overwrite populated profile (%d positions, %d skills) "
                "with empty data at %s. Save aborted.",
                len(existing_positions),
                len(existing_skills),
                profile_path,
            )
            return

        # Guard 2: suspicious reduction — both dimensions shrink
        if (
            existing_has_data
            and len(incoming_positions) < len(existing_positions)
            and len(incoming_skills) < len(existing_skills)
        ):
            logger.warning(
                "save_profile: suspicious reduction detected (%d->%d positions, %d->%d skills) "
                "at %s. Save aborted. Use force=True for intentional changes.",
                len(existing_positions),
                len(incoming_positions),
                len(existing_skills),
                len(incoming_skills),
                profile_path,
            )
            return

    # Atomic temp+rename write so a crash mid-write can never leave a truncated/
    # corrupt experience_profile.json — this file is the single point of enforcement
    # for every writer (onboarding, Profile Editor), so it carries the same atomicity
    # guarantee state._write_config already gives config.yaml.
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(normalized, f, indent=2, ensure_ascii=False)
    os.replace(tmp_path, path)
