# Design constraints and records

Design documents that bind future implementation work. Each records
constraints at the moment they were learned so they are discoverable when the
relevant feature is built, not after it is violated.

| Document | Binds |
|---|---|
| [provider-cascade-constraints.md](provider-cascade-constraints.md) | Any future model-provider cascade: single monotonic deadline across the whole chain; no silent timeout-parameter drops |
| [living-journal.md](living-journal.md) | Every template/stylesheet change: Living Journal identity rules (green = honesty only, paper never moves, closed jc-* vocabulary), token pipeline, vendored-asset provenance, re-sync procedure |
