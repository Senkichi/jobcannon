# Corpus-construction honesty: aggregator-listed vs. ATS-confirmed postings (working title)

> FOUNDER-AUTHORSHIP RULE: every `[OWNER WRITES]` block below is written by the
> owner personally. Agents may fix typos and formatting in owner-written text,
> never draft it. Numbers come from NUMBERS.md verbatim - if a number here
> disagrees with NUMBERS.md, NUMBERS.md wins.

## Hook

[OWNER WRITES - the one sharp number and why a job seeker (or anyone
evaluating a job board's data quality) should care. NUMBERS.md > Headline has
the exact split and the stale-rate ratio to draw from.]

## What we measured

Every posting in the corpus classified into exactly one provenance class -
`ats_confirmed` (a direct ATS-scanner sighting), `direct_crawl` (the
project's own first-party careers-page crawler), `aggregator_only` (a
third-party portal/search-engine listing), or `email_alert_only` (an inbox
job-alert digest) - by precedence, so a posting seen through more than one
channel is credited to the highest-trust one it reached. Full precedence
rule, label-set provenance, and exclusion accounting are in NUMBERS.md >
Method and Exclusion accounting. Copy the exclusion-accounting list here
verbatim from NUMBERS.md.

## Results

Embed `figures/stale_rate_by_provenance.png` and the Results table from
NUMBERS.md.

[OWNER WRITES - interpretation: what the stale-rate contrast between
ats_confirmed and aggregator_only actually means for a job seeker deciding
which listings to trust, what surprised you, and how this connects to two
things this project already learned the hard way -
1. the role.com aggregator-pollution incident (a sitemap-tier
   crawler trusted a job-board domain as a single employer, misattributing
   437 postings to a phantom company) - what this corpus-wide number says
   about how much weight that class of source deserves generally, not just
   in that one incident.
2. the opaque-redirect finding (portal aggregators republishing stale
   listings as "fresh," badge-suppression-by-freshness being the wrong fix)
   - does this piece's aggregator_only stale-rate number put a corpus-wide
   scale on that finding?
Both are prompts for you to write from, not conclusions to restate - pull
the exact numbers from NUMBERS.md > Results/Robustness as needed.]

## The query behind the number

Copy the SQL appendix from NUMBERS.md. One paragraph, owner-written, on why
the classification precedence was ordered this way (ATS-scan beats
first-party crawl beats aggregator beats email alert) and what "is_stale"
does and doesn't tell you.

## Honest caveats

Copy the Caveats section from NUMBERS.md verbatim (do not soften them). In
particular: the provenance taxonomy is a versioned snapshot of the private
pipeline's source vocabulary, not a live import (see Method) - flag if that
framing needs more or less emphasis for this audience.

## What's next

[OWNER WRITES - one line on the next piece in the series.]
