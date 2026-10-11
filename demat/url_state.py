"""ADR 0011: the end-of-script URL mirror owns a fixed set of navigation
keys and must never touch keys owned by bound filter widgets
(`bind="query-params"`), since `st.query_params.clear()`/`update()` raise
`StreamlitAPIException` once any bound key is present.

`sync_owned_params` is a small pure function over a `MutableMapping[str,
str]` so it can be unit-tested without a Streamlit runtime.
"""

from __future__ import annotations

from collections.abc import MutableMapping

OWNED_URL_KEYS = {"page", "assessment", "klasse", "schuljahr", "test_version"}


def sync_owned_params(
    params: MutableMapping[str, str], desired: dict[str, str]
) -> None:
    """Mutate `params` in place so its `OWNED_URL_KEYS` entries match `desired`.

    Only keys in `OWNED_URL_KEYS` are ever read, set or deleted; every other
    key in `params` (for example a filter's `bind="query-params"` parameter)
    is left completely untouched. `desired` must only contain owned keys.
    """
    assert set(desired).issubset(OWNED_URL_KEYS), (
        f"desired contains non-owned keys: {set(desired) - OWNED_URL_KEYS}"
    )
    for key in OWNED_URL_KEYS:
        if key in params and key not in desired:
            del params[key]
        elif key in desired and params.get(key) != desired[key]:
            params[key] = desired[key]
