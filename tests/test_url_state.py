"""ADR 0011: `sync_owned_params` only ever touches `OWNED_URL_KEYS`, never
the whole params mapping (so it can coexist with `bind="query-params"`
filter keys that would raise on `clear()`/`update()`)."""

from __future__ import annotations

from demat.url_state import sync_owned_params


def test_noop_when_nothing_changed():
    params = {"page": "review", "assessment": "42"}
    sync_owned_params(params, {"page": "review", "assessment": "42"})
    assert params == {"page": "review", "assessment": "42"}


def test_deletes_stale_owned_key():
    params = {"page": "review", "assessment": "42"}
    sync_owned_params(params, {"page": "review"})
    assert params == {"page": "review"}


def test_sets_changed_owned_key():
    params = {"page": "review"}
    sync_owned_params(params, {"page": "profile", "klasse": "3a"})
    assert params == {"page": "profile", "klasse": "3a"}


def test_leaves_non_owned_key_untouched():
    params = {"page": "review", "rv_status": "validated_ok"}
    sync_owned_params(params, {})
    assert params == {"rv_status": "validated_ok"}
