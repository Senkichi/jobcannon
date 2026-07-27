# Job Cannon

The honest job feed: aggregates postings from everywhere, dedups and kills
fake-fresh reposts, and ranks per-user by your actual priorities with a
visible "why." Nothing is promoted for money.

**Status:** pre-launch. This repo currently contains the published-analysis
pipeline (`analyses/`) and the engine-extraction inventory (`docs/`) that the
platform build executes against.

## Analyses

Methodology and code for every published Job Cannon analysis live in
`analyses/`. Raw corpus data is not published; committed outputs are
aggregates only. See each analysis directory's README.

## License

Code is licensed under [AGPL-3.0-or-later](LICENSE): if you run a modified
version of this software as a network service, you must make your modified
source available to its users. Forking and monetizing an open fork is fine;
taking it closed-source is not.

Published analysis outputs under `analyses/` (prose, figures, aggregate
data) are licensed separately — see `analyses/README.md`.

Contributions require a signed Contributor License Agreement — see
[CONTRIBUTING.md](CONTRIBUTING.md) and [CLA.md](CLA.md).
