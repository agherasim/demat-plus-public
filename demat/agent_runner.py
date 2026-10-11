"""SkillRunner protocol + implementations. The only module importing
`claude_agent_sdk` or `anthropic`. See ADR 0004 and spec `Skill contracts`.
"""

from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel

from demat import config
from demat.schema import ExtractedSheet, TriageResult

TRIAGE_SKILL = "demat-sheet-triage"

# How often a running skill call polls `stop_event` for cancellation.
_STOP_POLL_INTERVAL_S = 0.2


class SkillRunnerError(Exception):
    """Raised when a skill run fails to produce a usable result file."""


class RunnerUnavailableError(SkillRunnerError):
    """Raised when the runner's prerequisites (CLI/Node, credentials) are missing."""


class ExtractionStoppedError(SkillRunnerError):
    """Raised when `stop_event` was set while a skill call was in flight (a
    user-requested stop, e.g. `pipeline.process_pdfs`' "Stop" button) —
    never retried, and never reported as an extraction failure.
    """


class SkillRunner(Protocol):
    def run(
        self,
        skill_name: str,
        image_path: str,
        output_path: str,
        extra_instructions: str | None = None,
        context: str | None = None,
        stop_event: threading.Event | None = None,
    ) -> dict[str, Any]:
        """Run `skill_name` against the image, and return the parsed JSON result.

        `context` is the test definition's expected key/label list (ADR 0005
        §4), rendered by `test_registry.prompt_contract` and held constant
        across retry attempts. It is distinct from `extra_instructions`
        (retry-only, "correction needed from a previous attempt").

        `stop_event`, if given and set while this call is running, aborts it
        — for `ClaudeAgentSdkRunner` that means killing the in-flight `claude`
        CLI subprocess, not just skipping future calls — and raises
        `ExtractionStoppedError`.

        Raises `SkillRunnerError` (or a subclass) if the skill does not
        produce a parseable JSON object at `output_path`.
        """
        ...

    def run_text(
        self,
        prompt: str,
        output_path: str,
        output_model: type[BaseModel],
        skill_name: str | None = None,
        stop_event: threading.Event | None = None,
    ) -> dict[str, Any]:
        """Run a text-in/schema-out call (no image) and return the parsed
        JSON result. A sibling to `run()` (ADR 0008 §6.3/spec §6.3):
        `run()` requires a positional `image_path` and hardcodes its
        output model, so a prose-only call cannot go through it as
        written. `run()` and its sole call site
        (`demat/extraction.py:51`) are unaffected.

        `stop_event` behaves as documented on `run()`.

        Raises `SkillRunnerError` (or a subclass) if the call does not
        produce a parseable JSON object matching `output_model`.
        """
        ...


def _read_output_json(output_path: str) -> dict[str, Any]:
    path = Path(output_path)
    if not path.exists():
        raise SkillRunnerError(f"skill did not write an output file at {output_path}")
    try:
        text = path.read_text(encoding="utf-8")
        return json.loads(text)
    except (OSError, json.JSONDecodeError) as exc:
        raise SkillRunnerError(
            f"could not parse output file {output_path}: {exc}"
        ) from exc


async def _run_cancellable(
    make_coro: Callable[[], Awaitable[None]], stop_event: threading.Event | None
) -> None:
    """Await `make_coro()`, polling `stop_event` every
    `_STOP_POLL_INTERVAL_S` while it runs. If the event gets set first,
    cancel the task and raise `ExtractionStoppedError` instead of letting it
    run to completion.

    Cancelling the task raises `asyncio.CancelledError` inside the
    `async for` loop over `query()` — that alone does not close the
    underlying async generator (Python doesn't call `aclose()` on a
    `break`/exception out of `async for`), so the caller's coroutine must
    close it itself in a `finally` for the CLI subprocess to actually be
    killed (see `ClaudeAgentSdkRunner._run_query`/`_run_text_query`).
    """
    if stop_event is None:
        await make_coro()
        return

    task = asyncio.ensure_future(make_coro())
    try:
        while True:
            if stop_event.is_set():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                raise ExtractionStoppedError("extraction stopped by user")
            done, _pending = await asyncio.wait({task}, timeout=_STOP_POLL_INTERVAL_S)
            if task in done:
                task.result()
                return
    finally:
        if not task.done():
            task.cancel()


def _build_prompt(
    skill_name: str,
    image_path: str,
    output_path: str,
    extra_instructions: str | None,
    context: str | None = None,
) -> str:
    prompt = (
        f"Use the {skill_name} skill.\n"
        f"Image path: {image_path}\n"
        f"Output path: {output_path}\n"
        "Read the image, follow the skill's instructions, and write exactly one "
        "JSON object to the output path."
    )
    if context:
        prompt += f"\n\nExpected output fields for this test type:\n{context}"
    if extra_instructions:
        prompt += (
            f"\n\nCorrection needed from a previous attempt:\n{extra_instructions}"
        )
    return prompt


class ClaudeAgentSdkRunner:
    """Default runner: drives the Claude Code CLI via the Claude Agent SDK.

    Requires the `claude` CLI (and its Node runtime) to be installed and
    authenticated. Tool access is scoped to Read/Write only; Bash, WebFetch
    and WebSearch are explicitly disallowed so the agent cannot run shell
    commands or reach the network. (Full path-scoping to PAGES_DIR/RUNS_DIR
    via fine-grained `Read(<path>/**)` permission patterns was attempted but
    proved unreliable in this environment; tool-class scoping is used
    instead — see agent_runner deviation notes in the implementation report.)
    """

    def __init__(self, model_id: str = config.MODEL_ID) -> None:
        self.model_id = model_id
        self._checked_cli = False

    def _check_available(self) -> None:
        if self._checked_cli:
            return
        import shutil

        if shutil.which("claude") is None:
            raise RunnerUnavailableError(
                "The Claude Code CLI ('claude') was not found on PATH. The Agent SDK "
                "runner requires it (plus Node.js) to be installed. Install it, or "
                "set DEMAT_RUNNER=direct to use the DirectMessagesRunner fallback."
            )
        self._checked_cli = True

    def run(
        self,
        skill_name: str,
        image_path: str,
        output_path: str,
        extra_instructions: str | None = None,
        context: str | None = None,
        stop_event: threading.Event | None = None,
    ) -> dict[str, Any]:
        self._check_available()
        config.ensure_skills_discoverable()

        try:
            asyncio.run(
                _run_cancellable(
                    lambda: self._run_query(
                        skill_name,
                        image_path,
                        output_path,
                        extra_instructions,
                        context,
                    ),
                    stop_event,
                )
            )
        except RunnerUnavailableError:
            raise
        except ExtractionStoppedError:
            raise
        except Exception as exc:
            raise SkillRunnerError(f"agent run failed: {exc}") from exc

        return _read_output_json(output_path)

    async def _run_query(
        self,
        skill_name: str,
        image_path: str,
        output_path: str,
        extra_instructions: str | None,
        context: str | None = None,
    ) -> None:
        from claude_agent_sdk import ClaudeAgentOptions, query

        options = ClaudeAgentOptions(
            cwd=str(config.REPO_ROOT),
            skills=[skill_name],
            allowed_tools=["Read", "Write"],
            disallowed_tools=["Bash", "WebFetch", "WebSearch"],
            add_dirs=[str(config.PAGES_DIR), str(config.RUNS_DIR)],
            permission_mode="default",
            max_buffer_size=32 * 1024 * 1024,
        )
        prompt = _build_prompt(
            skill_name, image_path, output_path, extra_instructions, context
        )
        gen = query(prompt=prompt, options=options)
        try:
            async for _ in gen:
                pass
        finally:
            # Explicit close so cancellation (see `_run_cancellable`) tears
            # down the transport — and kills the `claude` subprocess — the
            # same way normal completion does; `async for` alone only does
            # that on a clean exhaustion of the generator.
            aclose = getattr(gen, "aclose", None)
            if aclose is not None:
                try:
                    await aclose()
                except Exception:
                    pass

    def run_text(
        self,
        prompt: str,
        output_path: str,
        output_model: type[BaseModel],
        skill_name: str | None = None,
        stop_event: threading.Event | None = None,
    ) -> dict[str, Any]:
        self._check_available()
        config.ensure_skills_discoverable()

        try:
            asyncio.run(
                _run_cancellable(
                    lambda: self._run_text_query(prompt, output_path, skill_name),
                    stop_event,
                )
            )
        except RunnerUnavailableError:
            raise
        except ExtractionStoppedError:
            raise
        except Exception as exc:
            raise SkillRunnerError(f"agent run failed: {exc}") from exc

        return _read_output_json(output_path)

    async def _run_text_query(
        self, prompt: str, output_path: str, skill_name: str | None
    ) -> None:
        from claude_agent_sdk import ClaudeAgentOptions, query

        options = ClaudeAgentOptions(
            cwd=str(config.REPO_ROOT),
            skills=[skill_name] if skill_name else [],
            allowed_tools=["Read", "Write"],
            disallowed_tools=["Bash", "WebFetch", "WebSearch"],
            add_dirs=[str(config.RUNS_DIR)],
            permission_mode="default",
            max_buffer_size=32 * 1024 * 1024,
        )
        full_prompt = (
            f"{prompt}\n\nWrite exactly one JSON object to the output path: "
            f"{output_path}"
        )
        gen = query(prompt=full_prompt, options=options)
        try:
            async for _ in gen:
                pass
        finally:
            aclose = getattr(gen, "aclose", None)
            if aclose is not None:
                try:
                    await aclose()
                except Exception:
                    pass


class DirectMessagesRunner:
    """Fallback/test runner: a plain Messages API call with the skill's
    `SKILL.md` + `reference.md` body inlined as the system prompt.

    Does not require the `claude` CLI, but does require `ANTHROPIC_API_KEY`
    (or another credential the `anthropic` SDK resolves) and does not get the
    native Agent Skills / filesystem-tool behavior — the model returns the
    JSON directly and this runner writes it to `output_path` itself.
    """

    def __init__(self, model_id: str = config.MODEL_ID) -> None:
        self.model_id = model_id

    def _skill_system_prompt(self, skill_name: str) -> str:
        skill_dir = config.SKILLS_DIR / skill_name
        skill_md = skill_dir / "SKILL.md"
        reference_md = skill_dir / "reference.md"
        if not skill_md.exists():
            raise SkillRunnerError(f"unknown skill: {skill_name!r} (no {skill_md})")
        parts = [skill_md.read_text(encoding="utf-8")]
        if reference_md.exists():
            parts.append(reference_md.read_text(encoding="utf-8"))
        return "\n\n".join(parts)

    def run(
        self,
        skill_name: str,
        image_path: str,
        output_path: str,
        extra_instructions: str | None = None,
        context: str | None = None,
        stop_event: threading.Event | None = None,
    ) -> dict[str, Any]:
        import base64

        import anthropic

        if stop_event is not None and stop_event.is_set():
            raise ExtractionStoppedError("extraction stopped by user")

        try:
            client = anthropic.Anthropic()
        except Exception as exc:
            raise RunnerUnavailableError(
                f"could not construct an Anthropic client (credentials?): {exc}"
            ) from exc

        system_prompt = self._skill_system_prompt(skill_name)
        image_bytes = Path(image_path).read_bytes()
        image_b64 = base64.standard_b64encode(image_bytes).decode("ascii")

        user_text = (
            "Read the attached page image and produce the JSON object described "
            "in the system prompt's output contract. Return only that JSON object."
        )
        if context:
            user_text += f"\n\nExpected output fields for this test type:\n{context}"
        if extra_instructions:
            user_text += (
                f"\n\nCorrection needed from a previous attempt:\n{extra_instructions}"
            )

        output_model = TriageResult if skill_name == TRIAGE_SKILL else ExtractedSheet

        try:
            response = client.messages.parse(
                model=self.model_id,
                max_tokens=16000,
                output_config={"effort": "high"},
                output_format=output_model,
                system=system_prompt,
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "image",
                                "source": {
                                    "type": "base64",
                                    "media_type": "image/png",
                                    "data": image_b64,
                                },
                            },
                            {"type": "text", "text": user_text},
                        ],
                    }
                ],
            )
        except anthropic.NotFoundError as exc:
            raise SkillRunnerError(f"model not found: {exc}") from exc
        except anthropic.RateLimitError as exc:
            raise SkillRunnerError(f"rate limited: {exc}") from exc
        except anthropic.APIStatusError as exc:
            raise SkillRunnerError(f"API error: {exc}") from exc
        except anthropic.APIConnectionError as exc:
            raise RunnerUnavailableError(
                f"could not reach the Anthropic API: {exc}"
            ) from exc

        parsed = response.parsed_output
        if parsed is None:
            raise SkillRunnerError("model did not return a parseable structured output")
        result = parsed.model_dump(mode="json")

        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        Path(output_path).write_text(
            json.dumps(result, ensure_ascii=False), encoding="utf-8"
        )
        return result

    def run_text(
        self,
        prompt: str,
        output_path: str,
        output_model: type[BaseModel],
        skill_name: str | None = None,
        stop_event: threading.Event | None = None,
    ) -> dict[str, Any]:
        import anthropic

        if stop_event is not None and stop_event.is_set():
            raise ExtractionStoppedError("extraction stopped by user")

        try:
            client = anthropic.Anthropic()
        except Exception as exc:
            raise RunnerUnavailableError(
                f"could not construct an Anthropic client (credentials?): {exc}"
            ) from exc

        system_prompt = (
            self._skill_system_prompt(skill_name)
            if skill_name
            else "Respond with structured output matching the requested schema."
        )

        try:
            response = client.messages.parse(
                model=self.model_id,
                max_tokens=16000,
                output_config={"effort": "high"},
                output_format=output_model,
                system=system_prompt,
                messages=[{"role": "user", "content": prompt}],
            )
        except anthropic.NotFoundError as exc:
            raise SkillRunnerError(f"model not found: {exc}") from exc
        except anthropic.RateLimitError as exc:
            raise SkillRunnerError(f"rate limited: {exc}") from exc
        except anthropic.APIStatusError as exc:
            raise SkillRunnerError(f"API error: {exc}") from exc
        except anthropic.APIConnectionError as exc:
            raise RunnerUnavailableError(
                f"could not reach the Anthropic API: {exc}"
            ) from exc

        parsed = response.parsed_output
        if parsed is None:
            raise SkillRunnerError("model did not return a parseable structured output")
        result = parsed.model_dump(mode="json")

        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        Path(output_path).write_text(
            json.dumps(result, ensure_ascii=False), encoding="utf-8"
        )
        return result


def get_default_runner() -> SkillRunner:
    if config.RUNNER == "direct":
        return DirectMessagesRunner()
    return ClaudeAgentSdkRunner()
