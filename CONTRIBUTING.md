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
`cla-signatures` branch) and re-runs the `CLA Assistant Lite` check, which
should then turn green with no further action from you. If it's still red
after a minute, comment `recheck` on the PR; if that doesn't flip it either,
push any new commit (an empty one is fine — `git commit --allow-empty -m
"chore: retrigger CLA check"`) to force a fresh run, which always
re-evaluates against your now-recorded signature.

Note for external (forked) contributions: just open your pull request as
usual — `ci.yml` runs on GitHub-hosted runners now and no longer refuses
fork PRs. The repo requires a maintainer to approve a first-time outside
contributor's workflow runs (GitHub Actions' "require approval for all
outside collaborators" setting); until that happens, the `Tests passed`
check sits pending rather than failing, which still blocks merge but needs
no action from you — a maintainer approves the run from the PR's Checks
tab.

Separately, add a line to `CONTRIBUTORS.md` in the form below, in the same
PR as (or before) your first code contribution:

```
- Your Name (@github-handle) — agrees to CLA.md as of YYYY-MM-DD
```

`CONTRIBUTORS.md` is the project's human-readable record of everyone who
has signed, independent of whether the `CLA Assistant Lite` check has run
yet. `CLA Assistant Lite` is listed in `.aviator/config.yml`'s
`required_checks`, so the Aviator merge queue gates on it.

## Pull requests

- One logical change per PR. Conventional commit style (`feat:`, `fix:`,
  `test:`, `chore:`) for commit messages.
- CI (lint + tests) must pass before merge.
- Adding a schema migration? Always run `python scripts/new_migration.py
  "<slug>"` to create the file — never hand-pick a `version=`. It mints a
  version free against origin/main and every open PR; a CI check
  (`Migration collision guard`) re-verifies at PR time as a backstop. See
  `docs/deploy-runbook.md` §3, "Creating a new migration".
