# ADR 0001 — Deterministic pseudonymous 4-character child ID

- Status: Accepted — **amended by ADR 0006** (the `klasse` component of the normalization key
  is removed there, so that a child re-tested in a later school year keeps one ID; everything
  else below still stands)
- Date: 2026-09-20
- Context: DEMAT assessment extraction pipeline (`demat` Streamlit app)

## Context

The app extracts test results from scanned DEMAT score sheets. Each sheet carries a
handwritten child's name. This is sensitive data about minors, so the name must not be
the identifier that is stored in the browsable results table or exported.

Requirements given:

- 4 characters, uppercase alphanumeric only (`A-Z`, `0-9`).
- Deterministic: the same name always yields the same ID, across runs, so that
  re-uploading a sheet or uploading a later test for the same child lands on the same
  person record without any lookup table.

These two requirements are in direct tension with uniqueness. A 4-character base-36 ID
has 36^4 = 1,679,616 values; by the birthday bound, ~100 children carry roughly a 0.3%
chance of a hash collision and ~500 children roughly 7%. Separately — and far more
likely in practice — two genuinely different children can share the same written name
("Leon" appears twice in a school), which by design maps to the same ID.

## Decision

**Algorithm.** `person_id = encode(int.from_bytes(blake2b(normalized_key, key=SALT, digest_size=8)) % 34**4)`,
padded to 4 chars, over the alphabet `123456789ABCDEFGHIJKLMNPQRSTUVWXYZ` — base36
minus `0` and `O` (added by a later addendum below): this ID is meant to be copied
onto paper by hand, and that pair is the one most often misread or miswritten.
Existing IDs allocated before this addendum may still contain `0`/`O`; they are
never re-derived (`person_id` is permanent once allocated — see the addendum),
so both alphabets can be valid simultaneously in stored data.

**Normalization** (order is load-bearing, because `str.casefold()` already maps `ß→ss`
and NFKD+combining-strip would map `ä→a` rather than `ae`):

1. Unicode NFKC.
2. Explicit German folding on the raw string: `ß→ss`, `Ä→Ae`, `Ö→Oe`, `Ü→Ue`,
   `ä→ae`, `ö→oe`, `ü→ue`.
3. Lowercase (`str.lower()`, not `casefold()` — folding is already done explicitly).
4. Replace every non-`[a-z0-9]` character with a single space.
5. Collapse runs of whitespace, strip.

Modulo bias over a 64-bit digest reduced to 36^4 is on the order of 1e-14 and is ignored.

**Salt.** The hash is keyed with a per-install secret. Unsalted, a 4-char hash of a common
German first name is reversible by dictionary attack in seconds, which would defeat the
point of pseudonymizing. Resolution order, first hit wins:

1. `st.secrets["demat"]["id_salt"]` — wrapped in `try/except`, because accessing
   `st.secrets` with no `.streamlit/secrets.toml` present **raises** rather than returning
   a miss, and there is no such file in a fresh checkout.
2. `DEMAT_ID_SALT` environment variable.
3. **Auto-generated on first run**: 32 bytes from `secrets.token_urlsafe`, persisted to
   `data/.salt` with mode `0600` (gitignored with the rest of `data/`).

The auto-generated default is deliberate: it gives install-scoped determinism with zero
operator setup, so the first extraction never fails on missing configuration. An operator
who wants to control or back up the salt sets it explicitly via (1) or (2). IDs are not
portable between installs — that is intended.

**Name-collision policy.** The normalization key includes the class
(`"<normalized name>|<normalized klasse>"`), which cheaply separates same-named children
in different classes. This is the only disambiguator applied automatically.
**(Reversed by ADR 0006: the key is now the normalized name alone, because cross-school-year
history per child is now an explicit requirement and the class component fractured it. The
same-name risk this clause guarded against is handled there by a `possible_person_merge`
flag rather than by the key.)**

**Hash-collision policy.** `persons` stores `normalization_key` with a UNIQUE index
alongside `person_id` as primary key. On insert, if a row exists for the computed
`person_id` but carries a *different* `normalization_key`:

- the assessment is **stored unlinked** (`assessments.person_id = NULL`) — it cannot be
  attached to the existing person, and the FK forbids inventing one;
- `flags` gains `id_collision` and `review_status` becomes `needs_review`;
- `assessments.review_notes` records the computed ID, the colliding
  `normalization_key`, and the raw name, since `person_names` has an FK to `persons` and
  therefore has nowhere to put the name of an unlinked sheet.

Records are never silently merged, and never dropped.

## Alternatives rejected

- **Random UUID / sequence + lookup table.** Guarantees uniqueness, but breaks the
  explicit determinism requirement and would require persisting a name→ID map, i.e.
  storing exactly the plaintext we are trying to avoid keeping around.
- **Longer ID (6–8 chars).** Removes the hash-collision concern almost entirely, but the
  4-char length is a stated requirement and the collision is now detected rather than
  silent.
- **Unsalted hash.** Portable across installs, but trivially reversible for short German
  first names; the privacy benefit would be cosmetic.
- **Mandatory operator-supplied salt (no auto-generation).** More explicit, but makes the
  very first extraction on a fresh install fail on configuration rather than work.
- **Name only, no class, in the key.** Simpler and stable if a child changes class, but
  guarantees same-name merges, the likeliest real-world corruption. Accepted tradeoff: a
  child re-tested in a new class gets a new ID; this is visible in the UI and recoverable,
  whereas a silent merge is not. **(Adopted after all in ADR 0006 — the requirement changed,
  and the "visible in the UI and recoverable" claim did not survive contact with it: a
  fractured person looks exactly like two children.)**
- **Base32 (Crockford).** Fewer confusable characters, but only 32^4 = 1,048,576 values
  and the requirement says alphanumeric; base36 (minus `0`/`O` per the addendum above)
  uses nearly the full permitted alphabet while keeping a larger ID space.
