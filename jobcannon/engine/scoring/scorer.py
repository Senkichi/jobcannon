# PORTED from job_finder/scoring/scorer.py @ ebb9f865b13b6304615b0ba11e5e39750151de1b (private job-cannon). Ledger L-0473.
"""Job scoring engine.

Scores each job 0-100 based on weighted match criteria against the user's profile.
"""

from datetime import UTC, datetime, timedelta

from thefuzz import fuzz

DEFAULT_MIN_SCORE_THRESHOLD = 40  # PORT-SEAM: job_finder.config is out of scope; inline the default
from jobcannon.engine.models import Job  # noqa: E402


class JobScorer:
    """Score jobs against a user profile."""

    def __init__(self, config: dict):
        """Initialize scorer with profile and scoring config.

        Args:
            config: Full config dict (profile + scoring sections).
        """
        self.profile = config.get("profile", {})
        self.weights = config.get("scoring", {}).get("weights", {})
        self.threshold = config.get("scoring", {}).get(
            "min_score_threshold", DEFAULT_MIN_SCORE_THRESHOLD
        )

        # Pre-compute lowercased exclusion sets for O(1) lookup on every scored job
        exclusions = self.profile.get("exclusions", {})
        self._title_exclusions: frozenset[str] = frozenset(
            kw.lower() for kw in exclusions.get("title_keywords", [])
        )
        self._excluded_companies: frozenset[str] = frozenset(
            c.lower() for c in exclusions.get("companies", [])
        )

    def score_jobs(self, jobs: list[Job]) -> list[Job]:
        """Score and sort a list of jobs. Returns jobs above threshold, sorted desc."""
        for job in jobs:
            job.score, job.score_breakdown = self._score_job(job)

        # Filter and sort
        scored = [j for j in jobs if j.score >= self.threshold]
        scored.sort(key=lambda j: j.score, reverse=True)
        return scored

    def _score_job(self, job: Job) -> tuple[float, dict]:
        """Score a single job. Returns (total_score, breakdown_dict)."""
        breakdown = {}

        breakdown["title_match"] = self._score_title(job.title)
        breakdown["seniority_alignment"] = self._score_seniority(job.title)
        breakdown["location_fit"] = self._score_location(job.location)
        breakdown["salary_range"] = self._score_salary(job.salary_min, job.salary_max)
        breakdown["industry_relevance"] = self._score_industry(job)
        breakdown["company_signals"] = self._score_company(job.company)
        breakdown["recency"] = self._score_recency(job.posted_date)

        # Weighted sum
        total = sum(breakdown[factor] * self.weights.get(factor, 0) for factor in breakdown)

        return round(total, 1), breakdown

    def _score_title(self, title: str) -> float:
        """Score 0-100 based on fuzzy match against target titles."""
        target_titles = self.profile.get("target_titles", [])
        if not target_titles:
            return 50  # neutral if no preference

        # Check exclusions first (pre-computed in __init__)
        title_lower = title.lower()
        if any(kw in title_lower for kw in self._title_exclusions):
            return 0  # hard reject

        # Best fuzzy match across all target titles
        best = max(fuzz.token_sort_ratio(title.lower(), t.lower()) for t in target_titles)
        return best

    def _score_seniority(self, title: str) -> float:
        """Score based on seniority level alignment."""
        title_lower = title.lower()

        # Desired seniority keywords and their scores
        seniority_map = {
            "staff": 100,
            "principal": 100,
            "lead": 90,
            "senior": 80,
            "sr.": 80,
            "manager": 85,
            "head of": 90,
            "director": 70,  # might be too senior
        }

        # Penalty keywords
        penalty_keywords = ["junior", "jr.", "associate", "entry", "intern"]
        for kw in penalty_keywords:
            if kw in title_lower:
                return 0

        # Check for seniority matches
        for kw, score in seniority_map.items():
            if kw in title_lower:
                return score

        # No seniority indicator - could be mid-level
        return 40

    def _score_location(self, location: str) -> float:
        """Score based on location preference match."""
        target_locations = self.profile.get("target_locations", [])
        if not target_locations:
            return 50

        location_lower = location.lower()

        # Remote is always good
        if "remote" in location_lower:
            return 100

        # "United States" usually means remote-eligible
        if location_lower in ("united states", "us", "usa"):
            return 90

        # Check fuzzy match against targets
        best = max(fuzz.partial_ratio(location_lower, t.lower()) for t in target_locations)
        return best

    def _score_salary(self, salary_min: int | None, salary_max: int | None) -> float:
        """Score based on salary range overlap with target."""
        target_min = self.profile.get("min_salary", 0)

        if salary_min is None and salary_max is None:
            return 50  # neutral if unknown

        # Use midpoint if we have a range
        if salary_min and salary_max:
            midpoint = (salary_min + salary_max) / 2
        elif salary_max:
            midpoint = salary_max
        else:
            midpoint = salary_min

        if midpoint >= target_min * 1.2:
            return 100  # well above target
        elif midpoint >= target_min:
            return 85  # meets target
        elif midpoint >= target_min * 0.8:
            return 50  # slightly below but negotiable
        else:
            return 10  # significantly below

    def _score_industry(self, job: Job) -> float:
        """Score based on industry keyword signals."""
        industries = self.profile.get("industries", [])
        if not industries:
            return 50

        # Check company name and description for industry signals
        search_text = f"{job.company} {job.title} {job.description or ''}".lower()

        matches = sum(1 for ind in industries if ind.lower() in search_text)
        if matches >= 2:
            return 100
        elif matches == 1:
            return 75
        else:
            return 30

    def _score_company(self, company: str) -> float:
        """Score based on company signals."""
        if company.lower() in self._excluded_companies:
            return 0

        # Could expand with a company database, Glassdoor ratings, etc.
        return 50  # neutral default

    def _score_recency(self, posted_date: datetime | None) -> float:
        """Score higher for more recent postings."""
        if not posted_date:
            return 50

        now = datetime.now(UTC)
        # Make posted_date aware if it isn't
        if posted_date.tzinfo is None:
            posted_date = posted_date.replace(tzinfo=UTC)

        age = now - posted_date
        if age < timedelta(days=1):
            return 100
        elif age < timedelta(days=3):
            return 85
        elif age < timedelta(days=7):
            return 65
        elif age < timedelta(days=14):
            return 40
        else:
            return 20
