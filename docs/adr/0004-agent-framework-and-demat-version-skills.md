# ADR 0004 — Claude Agent SDK as the extraction runtime, with one Agent Skill per DEMAT version

- Status: Accepted — **amended by ADR 0005 §9.5 and §9.7**: `demat-4plus-extraction` is
  deleted (no DEMAT 4 sheets exist in the corpus) and `demat-5plus-extraction` is added; and
  the skill-discovery mechanism changes from a single `.claude/skills` → `demat/skills`
  symlink to a generated `.claude/skills/` directory of per-skill symlinks, so that externally
  maintained skills can also be exposed. Everything else below stands.
- Date: 2026-09-20
- Extends: ADR 0002

## Context

DEMAT 1+, 2+, 3+ and 4+ are four different instruments. They share a sheet layout but
differ in subtest list, abbreviations, maxima and the composition of the Gesamtwert block.
A single monolithic extraction prompt would have to carry all four tables at once, which
(a) dilutes the instructions actually relevant to the sheet in front of the model,
(b) makes adding DEMAT 4+ an edit to a prompt shared with three working versions, and
(c) gives no place to put version-specific transcription quirks.

The requirement is therefore that the extraction step run through an agent framework where
**skills are a first-class, invocable-by-name concept**, so version-specific extraction
logic lives in separate, independently editable skill packages selected at runtime from the
detected test version.

## Decision

**Framework: the Claude Agent SDK** (`claude-agent-sdk`, Python) is the default and the
production path. It is Claude Code's own harness packaged as a library, it discovers and
invokes Agent Skills natively from a skills directory, and it ships the filesystem tools
(Read/Write) the extraction flow needs to read a page PNG and emit a result file. The agent
loop and context management come with it; we host it (harness-only — deployment stays local,
which is what this app wants).

**Skill layout.** Skills live in `demat/skills/`, one directory per skill with a `SKILL.md`
carrying `name`/`description` frontmatter:

| Skill | Purpose |
|---|---|
| `demat-sheet-triage` | Look at one page image; decide whether it is a DEMAT score sheet and, if so, which version (`DEMAT 1+`…`4+`) and form (A/B). Cheap, narrow. |
| `demat-1plus-extraction` | Full transcription using the DEMAT 1+ subtest table. |
| `demat-2plus-extraction` | Same, DEMAT 2+ (ZE, LÄ, AD, SU, DO, DI, HA, GEL, SA, GEO). |
| `demat-3plus-extraction` | Same, DEMAT 3+. |
| `demat-4plus-extraction` | Same, DEMAT 4+. |
| `demat-generic-extraction` | Fallback when triage cannot identify the version: transcribe whatever subtest rows are printed, in printed order. |

> **Amended by ADR 0005 §9.5.** `demat-4plus-extraction` is removed — the label was assumed
> symmetrically here and no DEMAT 4 sheet exists in the corpus. `demat-5plus-extraction` is
> added, DEMAT 5+ being a real instrument with a full manual transcription available. The
> triage skill's version list is henceforth generated from the test-definition registry rather
> than hand-maintained in its `SKILL.md`.

**Shared rules are authored once and copied in, not cross-referenced.** The shared
transcription rules and output contract are authored in `demat/skills/_source/reference.md`
and **generated into each skill directory** as `reference.md` by a small dev script, with a
test asserting the copies are current. A skill can reliably read files bundled inside its
own directory; a sibling-directory path (`../demat-shared/reference.md`) depends on how the
SDK exposes the skills root and fails *silently* — the agent simply never reads the rules,
and unflagged guessing is exactly the failure this design exists to prevent.

**Two-pass flow per sheet:** triage → version-specific extraction. The Python orchestrator
does the routing (detected version → skill name); the skill is not chosen by the model.
Routing in code keeps the mapping inspectable, testable and loggable, and means an unknown
version deterministically lands on the generic skill plus an `unknown_test_version` flag.

> **Amended by ADR 0005 §4.** The routing table `extraction.SKILL_FOR_VERSION` is replaced by
> a lookup in the test-definition registry (`extraction_skill` is declared in each test's TOML
> definition). Routing still happens in code, not in the model; only the source of the mapping
> moves from a Python dict to a data file.

**Structured output without `output_config`.** The Agent SDK does not expose the Messages
API's `output_config.format` schema guarantee. The output contract is therefore: the skill
instructs the agent to write its result as JSON to an exact path supplied in the prompt
(`data/runs/<page_key>.json`), and Python validates that file against the `ExtractedSheet`
Pydantic model. Validation failure triggers exactly one retry with the validation error fed
back; a second failure is recorded as `status="error"`, never as a partial row.

**A runner seam, not a hard dependency.** The pipeline depends on a small `SkillRunner`
protocol (`run(skill_name, image_path, output_path) -> dict`), with two implementations:
`ClaudeAgentSdkRunner` (**the default**) and `DirectMessagesRunner` (a fallback/test path:
plain `client.messages.parse()` with the same `SKILL.md` body inlined as the system prompt).
The skill markdown is the single source of truth for extraction instructions under both.
This keeps the app runnable where the Claude Code CLI is unavailable and keeps unit tests
free of both the CLI and the network. It is not an alternative to the skills design — both
runners execute the same skill packages.

> **Narrowed by ADR 0005 §4.** "The skill markdown is the single source of truth for
> extraction instructions" now means: the skill owns the *reading technique*, the test
> definition owns the *data shape*. The expected metric/subtest keys are injected into the
> prompt at call time through a new `context` argument on the protocol.

**New dependency:** `claude-agent-sdk` (in addition to `pymupdf` and `anthropic`). Runtime
constraint: the Python Agent SDK drives the Claude Code CLI, so the `claude` binary (and its
Node runtime) must be installed on the machine — a real deployment cost, and the reason the
`DirectMessagesRunner` fallback exists.

## Alternatives rejected

- **A single raw Messages API call with a hardcoded prompt** (the original ADR 0002 plan).
  Simplest and gives a hard schema guarantee via `output_config.format`, but has no skill
  concept at all and forces all four DEMAT tables into one prompt. Rejected per the
  requirement; retained only as the `DirectMessagesRunner` fallback.
- **Agent Skills over the Messages API** (`container={"skills": [...]}` + the
  `code_execution` tool). Skills are first-class and server-hosted, and it keeps
  `output_config` — but the skills must be uploaded and versioned through the Skills API,
  which adds a control-plane step and network state to what is otherwise a local, offline-
  editable `SKILL.md` directory. Worth revisiting if skill distribution across machines
  becomes a requirement.
- **Managed Agents (beta).** Supports Skills and MCP and runs the loop server-side, but
  provisions a hosted per-session container. Sending scans of children's score sheets into
  a hosted sandbox is a privacy step this local-first app should not take, and the
  persisted agent/session control plane is overhead for a single-user desktop tool.
- **SDK tool-runner with custom tools instead of skills.** Automates the loop but has no
  skill concept — version logic would go back into code-side prompt strings.
- **Model picks the skill itself** (all skills exposed, no triage pass). Fewer moving parts,
  but the selection becomes unobservable and untestable, and a misroute produces a silently
  wrong subtest table rather than a flag.
