# ADR 0011 — Filter state lives in the URL via native widget binding; the manual mirror owns a fixed key set

- Status: Accepted
- Date: 2026-09-23

## Context

`app.py` already mirrors a small amount of navigation state into the URL so a reload or a shared
link lands on the same spot:

- **Read side** (top of script): `page`, `inbox_selected_assessment` and `class_profile_selected`
  are seeded from `st.query_params` only `if "<key>" not in st.session_state`, so the URL wins on
  first load and session state wins on every later rerun.
- **Write side** (end of script): a hand-written `_url_params` dict is built per page
  (`page`, plus `assessment` on Assessment Review, or `klasse`/`schuljahr`/`test_version` on Class
  Profile). If `dict(st.query_params) != _url_params`, the code calls `st.query_params.clear()`
  and then `update()`.

The filter widgets on Assessment Review and Class Profile are not mirrored. Every new filter would
need another `if page == ...: _url_params[...] = ...` branch and another read-side seeding block
that parses a string back into a typed value. That is the part the request wants gone.

Streamlit 1.64 (the pinned version, `uv.lock`) supports `bind="query-params"` on input widgets
(multiselect, selectbox, checkbox, text_input, date_input, ...). A bound widget:

- uses its `key=` as the query-parameter name;
- on initial load, takes its value from the URL; after that, session state or user interaction
  wins (`SessionState._handle_query_param_binding`). This is the same rule as the existing
  "seed only if not already in session state" guard;
- drops the parameter from the URL when the value equals the widget default;
- ignores invalid URL values (for example a multiselect option that doesn't exist) and removes
  them instead of raising;
- when it unmounts, has its parameter removed from the URL (`QueryParams.remove_stale_bindings`).
  With `persist_state`, the value comes back on remount and is written back to the URL.

Bound parameters **can't be written, deleted or cleared through `st.query_params`**. `clear()`,
`update()`, `__setitem__` and `__delitem__` all raise `StreamlitAPIException` for a bound key.
The current end-of-script mirror runs `clear()` whenever the URL differs from its hand-built
dict.

While every bound widget is at its default, its parameter is absent from the URL, so nothing
breaks. As soon as a user picks a non-default filter, the URL holds a key the mirror doesn't
know about. From then on the comparison fails on every rerun, and `clear()` raises. The failure
is latent, and a smoke test with default filters won't catch it.

## Decision

1. **Filter widgets persist to the URL through `bind="query-params"`, not hand-written code.**
   Adding a URL-persisted filter means adding one widget with `key=<param name>`,
   `bind="query-params"` and `persist_state="session"`. No read-side seeding block and no
   write-side branch. Bound widgets must use a stable `key=` that is unique app-wide.
2. **The end-of-script mirror only handles non-widget navigation state, and only touches keys
   it owns.** A fixed `OWNED_URL_KEYS = {"page", "assessment", "klasse", "schuljahr",
   "test_version"}` is declared once. On each run the mirror:
   - computes the desired values for the owned keys;
   - deletes owned keys that are present but not wanted;
   - sets owned keys whose value changed.

   It never iterates, compares or clears the whole `st.query_params`. This logic lives in a
   small pure function over a `MutableMapping[str, str]` (`demat/url_state.py`) so it can be
   unit-tested without a Streamlit runtime. The first-load read side for these three
   navigation values stays as it is.
3. **Widget keys are URL parameter names.** They are short, page-prefixed (`rv_*` for Assessment
   Review, `cp_*` for Class Profile), and must never equal an owned key or the reserved `view`
   parameter used by the print route. The existing `inbox_*` / `cp_*_filter` filter keys are
   renamed. Only their definition sites reference them.
4. **`persist_state="session"` on every bound filter.** In this single-script app, the filters of
   the page you aren't on are unmounted. Without persistence, their state is garbage-collected
   and their URL parameters are removed, so a round trip through another page resets them. With
   it, the values survive and are written back to the URL when the page is shown again. This
   changes today's behaviour, where switching page resets filters.
5. **No PII in the URL.** Bound filters may carry pseudonymous or organisational values
   (status, test version, Klasse, Schuljahr, `person_id`). They must never carry a child's name
   (ADR 0001 / 0006). A free-text "name contains" filter must not be bound.

## Consequences

- One kwarg per filter replaces two hand-written blocks. The rerun model is unchanged: session
  state is the source of truth, and Streamlit syncs the URL.
- The mirror can no longer blow away parameters it doesn't own. That fixes the latent crash and
  also stops it from stripping any future parameter added by someone else.
- Multiselect widget identity includes `options` and `default`. If those are data-derived
  (for example the existing Schuljahr default "most recent year"), the widget is re-identified
  when the data changes, and that resets its state. New filters use options that are as stable
  as possible (registry versions, fixed enums, free text) and an empty default meaning "no
  constraint".
- Browser back/forward restoration depends on how the Streamlit frontend handles `popstate`.
  This ADR doesn't guarantee it. Reload and shared links are the guaranteed paths.
- A reset callback that writes session state for a widget that also declares `default=` logs
  Streamlit's one-time "created with a default value but also had its value set via the
  Session State API" warning. This is harmless and accepted.

## Alternatives rejected

- **A generic hand-rolled registry** (`URL_FILTER_KEYS = {...}` plus encode/decode per type,
  seeded on first load and written at the end of the script). It re-implements what `bind`
  does: typed serialization, invalid-value handling, default-collapsing, multi-value repeated
  params. It would also need its own `clear()`-free diffing. More code, more ways to get it wrong.
- **Keep `clear()` + `update()` and skip it when bound keys are present.** Fragile. The mirror
  would have to know every bound key, which puts back the per-filter list this ADR removes.
- **Move navigation state (`page`, selected assessment/class) onto bound widgets too.** These
  are set by buttons and row clicks, not input widgets, and `bind` doesn't support buttons.
  Out of scope.
- **`persist_state=None` (today's reset-on-page-switch behaviour).** Rejected because the
  unmount would also strip the parameters from the URL. That makes "remember filters in the
  URL" hold only while you stay on the page.
