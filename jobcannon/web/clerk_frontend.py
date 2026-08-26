"""Pure helper for deriving Clerk's Frontend API (FAPI) host from a
publishable key — issue #149.

A Clerk publishable key encodes its FAPI host: strip the `pk_live_`/
`pk_test_` prefix, base64-decode the remainder, and drop the trailing `$`
sentinel. This is the same derivation clerk-js performs client-side to know
which host to talk to; publishable keys are public by design (meant to be
embedded in client-side code), so decoding one server-side to build a
`<script src>` URL discloses nothing that isn't already shipped to the
browser.

Example: `pk_live_Y2xlcmsuam9iY2Fubm9uLmRldiQ=` decodes to
`clerk.jobcannon.dev$` -> FAPI host `clerk.jobcannon.dev`.
"""

from __future__ import annotations

import base64
import binascii

_PREFIXES = ("pk_live_", "pk_test_")


def frontend_api_host(publishable_key: str) -> str:
    """Decode `publishable_key` into its Clerk Frontend API host.

    Raises ValueError for anything that isn't a `pk_live_`/`pk_test_`
    prefixed, validly base64-encoded string ending in the `$` sentinel — a
    malformed key must never silently resolve to an empty/wrong host and
    have the caller load clerk-js against nothing (which would silently
    reproduce #149's "no __session ever gets set" failure instead of
    failing loudly at boot).
    """
    for prefix in _PREFIXES:
        if publishable_key.startswith(prefix):
            encoded = publishable_key[len(prefix) :]
            break
    else:
        raise ValueError(
            f"publishable key has no recognized pk_live_/pk_test_ prefix: {publishable_key!r}"
        )
    if not encoded:
        raise ValueError(f"publishable key has no content after its prefix: {publishable_key!r}")
    # clerk-js decodes the key with the browser's forgiving `atob`, which
    # tolerates missing `=` padding; re-pad so this check is never stricter
    # than the client it is paired with (a key clerk-js accepts must not
    # fail the web service's boot). `validate=True` still rejects any
    # non-alphabet character.
    padded = encoded + "=" * (-len(encoded) % 4)
    try:
        decoded = base64.b64decode(padded, validate=True).decode("ascii")
    except (binascii.Error, UnicodeDecodeError, ValueError) as exc:
        raise ValueError(f"publishable key is not valid base64: {publishable_key!r}") from exc
    if not decoded.endswith("$"):
        raise ValueError(f"decoded publishable key is missing the '$' sentinel: {decoded!r}")
    host = decoded[:-1]
    if not host:
        raise ValueError(f"decoded publishable key host is empty: {publishable_key!r}")
    return host
