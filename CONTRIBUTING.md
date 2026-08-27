# Contributing

## License

Job Cannon is licensed under [AGPL-3.0-or-later](LICENSE). By contributing,
you agree your contribution will be distributed under the same license,
subject to the CLA below.

## Contributor License Agreement

Before a pull request can be merged, first-time contributors must agree to
the [Contributor License Agreement](CLA.md). This is what lets the Maintainer
keep the option to relicense the project later (e.g. fixing a licensing
mistake, or offering a separate commercial license) without having to track
down every past contributor individually. It does not take anything away
from you — you keep full rights to your own contribution for any other use.

**To sign:** open your pull request as usual — no separate signing step
first. If you haven't signed before, the `CLA Assistant Lite` check comments
on the PR with a link to [CLA.md](CLA.md) and asks you to reply to that
comment with:

```
I have read the CLA Document and I hereby sign the CLA
```

The bot records your signature (in a signatures file on the dedicated
`cla-signatures` branch) and the `CLA Assistant Lite` check turns green.
Comment `recheck` on the PR to re-run the check without pushing a new
commit.

Separately, add a line to `CONTRIBUTORS.md` in the form below, in the same
PR as (or before) your first code contribution:

```
- Your Name (@github-handle) — agrees to CLA.md as of YYYY-MM-DD
```

`CONTRIBUTORS.md` is the project's human-readable record of everyone who
has signed; it is not what gates merge. The `CLA Assistant Lite` check does
that, independent of whether your `CONTRIBUTORS.md` line has landed yet.

## Pull requests

- One logical change per PR. Conventional commit style (`feat:`, `fix:`,
  `test:`, `chore:`) for commit messages.
- CI (lint + tests) must pass before merge.
