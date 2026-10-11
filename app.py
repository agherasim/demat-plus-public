import hashlib
import logging
import threading
import time
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import pandas as pd
import streamlit as st
from streamlit.delta_generator import DeltaGenerator

from demat import (
    class_groups,
    config,
    norm_completion,
    norm_variants,
    store,
    url_state,
)
from demat.agent_runner import (
    RunnerUnavailableError,
    SkillRunner,
    get_default_runner,
)
from demat.pipeline import (
    PdfInput,
    ReextractResult,
    process_pdfs,
    reextract_assessments,
)
from demat.reports import generate_class_child_reports, generate_class_profile
from demat.reports.norms import KnowledgeBaseUnavailableError, resolve_norm_group
from demat.review_validation import (
    ReviewFinding,
    ReviewValidationResult,
    findings_from_json,
    findings_to_json,
    run_review_validations,
)
from demat.school_year import klasse_schuljahre, school_year
from demat.test_registry import TestDefinition
from demat.validation import flag_severity

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("demat.app")


@st.cache_resource
def _log_startup() -> None:
    # app.py's top level reruns on every interaction, so without
    # cache_resource (which runs its body once per process) this would log
    # on every rerun instead of once, alongside Streamlit's own "Local URL"
    # startup message.
    logger.info("DEMAT_HOME=%s (data dir: %s)", config.DEMAT_HOME, config.DATA_DIR)


_log_startup()

st.set_page_config(page_title="DEMAT+", layout="wide")

st.markdown(
    """
    <style>
    /* Carlito (sans, for headings/tables/UI) loads via theme.font in
       config.toml. These two are NOT the default font, so they need an
       explicit @import here; each is opted into via a specific class. */
    @import url('https://fonts.googleapis.com/css2?family=Mulish:wght@400;800&display=swap');
    @import url('https://fonts.googleapis.com/css2?family=Source+Serif+4:opsz,wght@8..60,400;8..60,600&display=swap');

    /* Brand design tokens (Coolors export, palette.pdf). The *-light/-dark/
       sidebar-bg tints are mixed toward --demat-bg rather than toward
       white, so they stay coherent with the app's background. */
    :root {
        --demat-primary: #FCB801;         /* brand gold */
        --demat-primary-dark: #D69C01;    /* hover shade of the primary */
        --demat-secondary: #0062AD;       /* secondary accent / links */
        --demat-secondary-dark: #005393;  /* hover shade of the secondary */
        --demat-secondary-light: #DEE9F3; /* selected/hover row background */
        --demat-bg: #FAF7F0;              /* main content background */
        --demat-ink: #12100C;             /* body/heading text */
        --demat-sidebar-bg: #ECE9E2;      /* sidebar shell, tinted off --demat-bg */
        --demat-divider: #DEDBD5;         /* row dividers / chart tracks */
    }

    /* Sidebar shell: light gray, with the brand block bleeding to its edges */
    [data-testid="stSidebar"] {
        background-color: var(--demat-sidebar-bg);
    }
    /* App header: match the sidebar shell so the two form one shell */
    [data-testid="stHeader"] {
        background-color: var(--demat-sidebar-bg);
    }
    /* Brand block: cover-gold band with the wordmark, in the sidebar's own
       header (the strip that holds the collapse control), via a CSS
       pseudo-element so the logo container itself carries the brand. */
    [data-testid="stSidebarHeader"] {
        background-color: var(--demat-primary);
        margin: 0 -22.5px 1.25rem -22.5px;
        padding: 1rem 22.5px;
        align-items: center;
    }
    [data-testid="stSidebarHeader"] [data-testid="stLogoSpacer"] {
        display: none;
    }
    [data-testid="stSidebarHeader"]::before {
        content: "DEMAT+";
        font-family: 'Mulish', sans-serif;
        font-weight: 400;
        text-transform: uppercase;
        letter-spacing: 0.02em;
        color: var(--demat-ink);
        font-size: 2.25rem;
        line-height: 1;
    }

    /* Opt-in serif for large blocks of prose (e.g. long assessment notes).
       Everything else (tabs, tables, buttons, inputs, chart text, and
       headings h2-h6) stays on the sans-serif Carlito set as theme.font. */
    .demat-prose {
        font-family: 'Source Serif 4', serif;
    }

    /* Keep text readable on the light gold primary color */
    [data-testid="stBaseButton-primary"] {
        color: var(--demat-ink) !important;
    }

    /* Sidebar nav: left-aligned labels, tight spacing, gold for the active page */
    [data-testid="stSidebar"] [data-testid="stVerticalBlock"] {
        gap: 6px !important;
    }
    [data-testid="stSidebar"] button[data-testid^="stBaseButton"] {
        justify-content: flex-start !important;
        text-align: left !important;
        padding-left: 13.5px !important;
        border-width: 1px !important;
        border-style: solid !important;
        border-color: transparent !important;
    }
    [data-testid="stSidebar"] button[data-testid^="stBaseButton"] > div {
        justify-content: flex-start !important;
    }
    [data-testid="stSidebar"] [data-testid="stBaseButton-primary"]:hover {
        background-color: var(--demat-primary-dark) !important;
        border-color: var(--demat-primary-dark) !important;
    }

    /* Inbox / Class Profile lists: flat rows with a divider, not stacked
       button cards. Shared selector — both lists use the same layout. */
    .st-key-inbox_list [data-testid="stVerticalBlock"],
    .st-key-class_list [data-testid="stVerticalBlock"] {
        gap: 0 !important;
    }
    .st-key-inbox_list [data-testid="stHorizontalBlock"],
    .st-key-class_list [data-testid="stHorizontalBlock"] {
        border-width: 0 0 1px 0 !important;
        border-style: solid !important;
        border-color: var(--demat-divider) !important;
        padding: 10px 6px !important;
    }
    /* Inbox and Class Profile rows both carry a selection checkbox, so
       they're denser than a plain button row — compact them further. */
    .st-key-inbox_list [data-testid="stHorizontalBlock"],
    .st-key-class_list [data-testid="stHorizontalBlock"] {
        padding: 4px 6px !important;
    }
    .st-key-inbox_list button[data-testid^="stBaseButton"],
    .st-key-class_list button[data-testid^="stBaseButton"] {
        justify-content: flex-start !important;
        text-align: left !important;
        border: none !important;
        background-color: transparent !important;
        padding: 0 !important;
    }
    .st-key-inbox_list button[data-testid^="stBaseButton"] > div,
    .st-key-class_list button[data-testid^="stBaseButton"] > div {
        justify-content: flex-start !important;
    }
    .st-key-inbox_list [data-testid="stHorizontalBlock"]:has(
        [data-testid="stBaseButton-secondary"]
    ):hover,
    .st-key-class_list [data-testid="stHorizontalBlock"]:has(
        [data-testid="stBaseButton-secondary"]
    ):hover {
        background-color: var(--demat-secondary-light) !important;
    }
    .st-key-inbox_list [data-testid="stHorizontalBlock"]:has(
        [data-testid="stBaseButton-primary"]
    ),
    .st-key-class_list [data-testid="stHorizontalBlock"]:has(
        [data-testid="stBaseButton-primary"]
    ) {
        background-color: var(--demat-secondary-light) !important;
        border-color: var(--demat-secondary) !important;
    }

    /* Validate action: the brand green, distinct from the app's gold
       primary. Darkened from --demat-green (#188D42) for AA contrast with
       the white label text; the un-darkened green is the theme's
       greenColor, used elsewhere (st.success, :green[...] text). */
    .st-key-validate_action [data-testid="stBaseButton-primary"] {
        background-color: #147838 !important;
        border-color: #147838 !important;
        color: #FFFFFF !important;
    }
    .st-key-validate_action [data-testid="stBaseButton-primary"]:hover {
        background-color: #126A32 !important;
        border-color: #126A32 !important;
    }

    /* "Fields were edited" marker next to the Name/Klasse/Schuljahr caption:
       push it flush to the row's right edge via flex auto-margin — more
       reliable than a horizontal_alignment="distribute" container, which
       only spreads elements across their own combined width, not the full
       row, when that combined width is narrower than the row. */
    .st-key-review_edited_marker {
        margin-left: auto !important;
    }

    /* Inline code (e.g. the monospaced person ID) defaults to a smaller
       font-size than its surrounding text, which misaligns it against
       adjacent plain text — match the parent's size everywhere. */
    code {
        font-size: 1em;
    }

    /* Trim the default main-content padding, which is oversized for this
       app's dense tables and review panel. */
    .stMainBlockContainer {
        padding: 45px !important;
    }

    /* st.info alerts: sidebar gray instead of Streamlit's default blue,
       so they read as a neutral aside rather than a distinct color. */
    [data-testid="stAlertContainer"]:has([data-testid="stAlertContentInfo"]) {
        background-color: var(--demat-sidebar-bg) !important;
        color: var(--demat-ink) !important;
    }

    /* Assessment Review and Class Profile: the list and the preview panel
       each scroll on their own within the remaining viewport height below
       the page's fixed chrome, instead of growing the whole page. Without
       this, stMain's own bottom padding leaves it just tall enough to
       scroll by a few px, which content-driven wheel scrolling inside the
       (possibly very long) list chains into once the list itself hits its
       end — the "main page still scrolls" bug. Pinning stMain to
       overflow:hidden whenever one of these fixed-height layouts is
       present removes that page-level scroll entirely; the list/panel are
       still independently scrollable via their own overflow-y:auto. */
    [data-testid="stMain"]:has(.st-key-inbox_review_layout),
    [data-testid="stMain"]:has(.st-key-class_profile_layout) {
        overflow: hidden !important;
    }

    /* The filters and batch-action bar above the list keep their natural
       height; only .st-key-inbox_list/.st-key-inbox_review_panel flex to
       fill what's left, so the list's scrollable area shrinks/grows with
       them. */
    .st-key-inbox_review_layout {
        height: calc(100vh - 90px) !important;
        flex: none !important;
        min-height: 0 !important;
    }
    .st-key-inbox_review_layout > [data-testid="stLayoutWrapper"] {
        height: 100% !important;
        min-height: 0 !important;
    }
    .st-key-inbox_review_layout > [data-testid="stLayoutWrapper"] > [data-testid="stHorizontalBlock"] {
        height: 100% !important;
        flex: none !important;
        min-height: 0 !important;
    }
    .st-key-inbox_review_layout > [data-testid="stLayoutWrapper"] > [data-testid="stHorizontalBlock"] > [data-testid="stColumn"] {
        height: 100% !important;
        display: flex !important;
        flex-direction: column !important;
        flex: none !important;
        min-height: 0 !important;
    }
    .st-key-inbox_list,
    .st-key-inbox_review_panel {
        flex: 1 1 auto !important;
        min-height: 0 !important;
        overflow-y: auto !important;
    }
    /* The list sits alongside sibling filter/batch-bar elements inside the
       same flex column, wrapped in Streamlit's own element-container div —
       that wrapper (not .st-key-inbox_list itself) is the actual flex
       sibling competing for space, so it also needs flex:1 or it keeps its
       default flex-grow:0 and the "filters take X, list takes 100%" bug
       comes back. */
    .st-key-inbox_review_layout div:has(> .st-key-inbox_list) {
        flex: 1 1 auto !important;
        min-height: 0 !important;
        overflow: hidden !important;
    }

    /* Class Profile: same independent-scroll treatment as Assessment
       Review above — the class list and the profile panel each scroll on
       their own within the remaining viewport height, instead of growing
       the whole page. */
    .st-key-class_profile_layout {
        height: calc(100vh - 90px) !important;
        flex: none !important;
        min-height: 0 !important;
    }
    .st-key-class_profile_layout > [data-testid="stLayoutWrapper"] {
        height: 100% !important;
        min-height: 0 !important;
    }
    .st-key-class_profile_layout > [data-testid="stLayoutWrapper"] > [data-testid="stHorizontalBlock"] {
        height: 100% !important;
        flex: none !important;
        min-height: 0 !important;
    }
    .st-key-class_profile_layout > [data-testid="stLayoutWrapper"] > [data-testid="stHorizontalBlock"] > [data-testid="stColumn"] {
        height: 100% !important;
        display: flex !important;
        flex-direction: column !important;
        flex: none !important;
        min-height: 0 !important;
    }
    .st-key-class_list,
    .st-key-class_profile_panel {
        flex: 1 1 auto !important;
        min-height: 0 !important;
        overflow-y: auto !important;
    }
    .st-key-class_profile_layout div:has(> .st-key-class_list) {
        flex: 1 1 auto !important;
        min-height: 0 !important;
        overflow: hidden !important;
    }
    </style>
    """,
    unsafe_allow_html=True,
)

store.init_db()

if st.query_params.get("view") == "print":
    st.markdown(
        """
        <style>
        [data-testid="stSidebar"], [data-testid="stHeader"] {
            display: none !important;
        }
        html, body, .stApp, [data-testid="stAppViewContainer"],
        [data-testid="stMain"], [data-testid="stMainBlockContainer"],
        .stMainBlockContainer {
            background-color: #FFFFFF !important;
        }
        .stMainBlockContainer {
            max-width: 860px !important;
            margin: 2rem auto !important;
            padding: 0 1.5rem !important;
        }
        @media print {
            .stMainBlockContainer { margin: 0 !important; padding: 0 0.5in !important; }
        }
        @page { margin: 1.5cm; }
        </style>
        """,
        unsafe_allow_html=True,
    )
    print_group = next(
        (
            g
            for g in class_groups.list_groups()
            if g.klasse == st.query_params.get("klasse")
            and g.schuljahr == st.query_params.get("schuljahr")
            and g.test_version == st.query_params.get("test_version")
        ),
        None,
    )
    print_md_path = (
        Path(print_group.latest_profile["markdown_path"])
        if print_group and print_group.latest_profile
        else None
    )
    if print_md_path is None or not print_md_path.exists():
        st.error("No generated report found for this class.")
    else:
        st.markdown(print_md_path.read_text(encoding="utf-8"), unsafe_allow_html=True)
    st.stop()

# ADR 0014 §8.3: the child report's print view is A4/12mm/10pt, distinct
# from the class profile's print CSS above (which is not single-page).
_CHILD_PRINT_CSS = """
<style>
[data-testid="stSidebar"], [data-testid="stHeader"] {
    display: none !important;
}
html, body, .stApp, [data-testid="stAppViewContainer"],
[data-testid="stMain"], [data-testid="stMainBlockContainer"],
.stMainBlockContainer {
    background-color: #FFFFFF !important;
}
.stMainBlockContainer {
    max-width: 100% !important;
    margin: 0 auto !important;
    padding: 0 1.5rem !important;
    font-size: 10pt !important;
}
/* Streamlit's default h1/h2 (2.75rem/2.25rem plus padding) blow the spec
   §8.2 budget of 32px/26px per heading and push the page onto a second sheet. */
.stMainBlockContainer h1 {
    font-size: 16pt !important;
    line-height: 1.2 !important;
    padding: 0 0 6px 0 !important;
    margin: 0 !important;
}
.stMainBlockContainer h2 {
    font-size: 12pt !important;
    line-height: 1.2 !important;
    padding: 4px 0 2px 0 !important;
    margin: 0 !important;
}
/* Streamlit sets p/li/td to its own 1rem/1.6 with 1rem gaps, so the 10pt on
   the container above never reaches the body text without these. */
.stMainBlockContainer p, .stMainBlockContainer li,
.stMainBlockContainer th, .stMainBlockContainer td {
    font-size: 10pt !important;
    line-height: 1.35 !important;
}
.stMainBlockContainer p, .stMainBlockContainer ul, .stMainBlockContainer table {
    margin-bottom: 6px !important;
}
.stMainBlockContainer li { margin: 0 0 0 1.2em !important; }
.stMainBlockContainer hr { margin: 8px 0 !important; }
svg { max-width: 100%; height: auto; }
@media print {
    .stMainBlockContainer { margin: 0 !important; padding: 0 !important; }
    .screen-only { display: none !important; }
    [data-testid="stVerticalBlock"] { display: block !important; }
    [class*="st-key-child_report_page_"] { break-after: page; }
    [class*="st-key-child_report_page_"]:last-of-type { break-after: auto; }
    * {
        print-color-adjust: exact !important;
        -webkit-print-color-adjust: exact !important;
    }
}
@page { size: A4 portrait; margin: 12mm; }
</style>
"""

if st.query_params.get("view") == "child":
    st.markdown(_CHILD_PRINT_CSS, unsafe_allow_html=True)
    assessment_param = st.query_params.get("assessment_id")
    child_assessment_id = (
        int(assessment_param)
        if assessment_param is not None and assessment_param.isdigit()
        else None
    )
    child_report_row = (
        store.latest_child_report(child_assessment_id)
        if child_assessment_id is not None
        else None
    )
    print_child_md_path = (
        Path(child_report_row["markdown_path"])
        if child_report_row is not None
        else None
    )
    if print_child_md_path is None or not print_child_md_path.exists():
        st.error("No generated report found for this child.")
    else:
        st.markdown(
            print_child_md_path.read_text(encoding="utf-8"), unsafe_allow_html=True
        )
    st.stop()

if st.query_params.get("view") == "child_batch":
    st.markdown(_CHILD_PRINT_CSS, unsafe_allow_html=True)
    batch_group = next(
        (
            g
            for g in class_groups.list_groups()
            if g.klasse == st.query_params.get("klasse")
            and g.schuljahr == st.query_params.get("schuljahr")
            and g.test_version == st.query_params.get("test_version")
        ),
        None,
    )
    if batch_group is None:
        st.error("No matching class found.")
    else:
        rendered_count = 0
        missing_count = 0
        for batch_assessment_id in batch_group.assessment_ids:
            batch_report_row = store.latest_child_report(batch_assessment_id)
            batch_md_path = (
                Path(batch_report_row["markdown_path"])
                if batch_report_row is not None
                else None
            )
            if batch_md_path is None or not batch_md_path.exists():
                missing_count += 1
                continue
            rendered_count += 1
            with st.container(key=f"child_report_page_{batch_assessment_id}"):
                st.markdown(
                    batch_md_path.read_text(encoding="utf-8"),
                    unsafe_allow_html=True,
                )
        if rendered_count == 0:
            st.error("No generated child reports found for this class.")
        if missing_count:
            st.markdown(
                f'<p class="screen-only">{missing_count} of '
                f"{missing_count + rendered_count} child reports have not "
                "been generated yet and are not included here.</p>",
                unsafe_allow_html=True,
            )
    st.stop()

NAV_PAGES = ["Load Assessments", "Assessment Review", "Class Profile"]
# Slugs keep the URL short and stable across any future page renames.
NAV_PAGE_SLUGS = {
    "Load Assessments": "load",
    "Assessment Review": "review",
    "Class Profile": "profile",
}
NAV_SLUG_TO_PAGE = {slug: name for name, slug in NAV_PAGE_SLUGS.items()}

if "page" not in st.session_state:
    page_param = st.query_params.get("page")
    st.session_state["page"] = (
        NAV_SLUG_TO_PAGE[page_param] if page_param in NAV_SLUG_TO_PAGE else NAV_PAGES[0]
    )
if "inbox_selected_assessment" not in st.session_state:
    assessment_param = st.query_params.get("assessment")
    st.session_state["inbox_selected_assessment"] = (
        int(assessment_param)
        if assessment_param is not None and assessment_param.isdigit()
        else None
    )
if "class_profile_selected" not in st.session_state:
    klasse_param = st.query_params.get("klasse")
    schuljahr_param = st.query_params.get("schuljahr")
    test_version_param = st.query_params.get("test_version")
    st.session_state["class_profile_selected"] = (
        "␟".join([klasse_param, schuljahr_param, test_version_param])
        if klasse_param and schuljahr_param and test_version_param
        else None
    )

with st.sidebar:
    for nav_page in NAV_PAGES:
        st.button(
            nav_page,
            key=f"nav_{nav_page}",
            width="stretch",
            type="primary" if st.session_state["page"] == nav_page else "tertiary",
            on_click=lambda p=nav_page: st.session_state.update(page=p),
        )
page = st.session_state["page"]

st.markdown(
    f"""
    <style>
    /* Page title lives in the app toolbar (left of Deploy/menu) instead of
       a redundant st.title() in the body — the sidebar nav button already
       names the page, so a second in-page heading just repeats it. */
    [data-testid="stToolbar"] > div > div:first-child::before {{
        content: {page!r};
        display: inline-block;
        margin-left: 1rem;
        font-family: inherit;
        font-weight: normal;
        font-size: 1.5rem;
        color: var(--demat-ink);
    }}
    </style>
    """,
    unsafe_allow_html=True,
)


def _select_inbox_row(assessment_id: int) -> None:
    st.session_state["inbox_selected_assessment"] = assessment_id
    store.mark_opened(assessment_id)


# --- Re-extract batch (ADR 0016) -------------------------------------------
# Runs `pipeline.reextract_assessments` on one background thread; the batch
# dict below is shared with it (plain object, progress under `lock`). A
# fragment polls it once a second instead of rerunning the whole app, and
# triggers one full-app rerun when the thread is done so the list/panel
# refresh; `_finish_reextract_batch` then remaps the selection.

_REEXTRACT_REASONS = {
    "kept_not_a_sheet": (
        "page no longer recognised as a DEMAT sheet — row left unchanged"
    ),
    "skipped_source_unavailable": (
        "source PDF/page image not found — row left unchanged"
    ),
    "skipped_missing": "row no longer exists",
    "stopped": "stopped before processing",
}


def _runner_unavailable_message(error: str) -> str:
    return (
        "The default extraction runner (Claude Agent SDK, via the `claude` "
        "CLI) is not available in this environment.\n\n"
        f"{error}\n\n"
        "Fix: install the `claude` CLI and Node.js, or set the environment "
        "variable `DEMAT_RUNNER=direct` to use the DirectMessagesRunner "
        "fallback (requires `ANTHROPIC_API_KEY`) and restart the app."
    )


def _reextract_batch_active() -> bool:
    return st.session_state.get("inbox_reextract_batch") is not None


def _start_reextract_batch(assessment_ids: list[int]) -> None:
    runner = get_default_runner()
    batch: dict[str, Any] = {
        "ids": list(assessment_ids),
        "lock": threading.Lock(),
        "progress": {"done": 0, "total": len(assessment_ids), "current": None},
        "stop_event": threading.Event(),
        "results": [],
        "error": None,
        "unexpected_error": None,
        "done": False,
    }

    def _progress_cb(done: int, total: int, current: int) -> None:
        with batch["lock"]:
            batch["progress"] = {"done": done, "total": total, "current": current}

    def _run() -> None:
        try:
            reextract_assessments(
                batch["ids"],
                runner,
                progress_cb=_progress_cb,
                stop_event=batch["stop_event"],
                results_sink=batch["results"],
            )
        except RunnerUnavailableError as exc:
            batch["error"] = str(exc)
        except Exception as exc:
            logger.exception("re-extract batch failed")
            batch["unexpected_error"] = str(exc)
        finally:
            batch["done"] = True

    st.session_state["inbox_reextract_batch"] = batch
    threading.Thread(target=_run, daemon=True).start()


def _finish_reextract_batch() -> None:
    """Once the background batch is done: remap the selection from old to
    new ids, drop the old rows' checkbox keys, and keep a summary for the
    next render. Runs before the list and the review panel read state."""
    batch = st.session_state.get("inbox_reextract_batch")
    if batch is None or not batch["done"]:
        return
    results: list[ReextractResult] = list(batch["results"])
    remap = {
        r.old_assessment_id: r.new_assessment_id
        for r in results
        if r.status == "replaced" and r.new_assessment_id is not None
    }
    for old in batch["ids"]:
        st.session_state.pop(f"inbox_select_{old}", None)
    selected = st.session_state.get("inbox_selected_assessment")
    if selected in remap:
        st.session_state["inbox_selected_assessment"] = remap[selected]
        store.mark_opened(remap[selected])
    st.session_state["inbox_reextract_summary"] = {
        "ids": list(batch["ids"]),
        "results": results,
        "error": batch["error"],
        "unexpected_error": batch["unexpected_error"],
    }
    st.session_state["inbox_reextract_batch"] = None


def _render_reextract_summary() -> None:
    summary = st.session_state.get("inbox_reextract_summary")
    if summary is None:
        return
    by_id = {r.old_assessment_id: r for r in summary["results"]}
    replaced = [r for r in summary["results"] if r.status == "replaced"]
    lines = []
    for old in summary["ids"]:
        r = by_id.get(old)
        if r is None:
            lines.append(f"#{old}: not processed")
        elif r.status == "kept_error":
            lines.append(f"#{old}: {r.error}")
        elif r.status != "replaced":
            lines.append(f"#{old}: {_REEXTRACT_REASONS[r.status]}")
    n_attention = sum(1 for r in replaced if r.has_blocking_flags)
    text = f"Re-extracted {len(replaced)} · unchanged {len(lines)}"
    if n_attention:
        text += f" · {n_attention} need attention (flags)"
    body = "\n".join([text, *(f"- {line}" for line in lines)])
    if summary["error"]:
        st.error(_runner_unavailable_message(summary["error"]))
    if summary["unexpected_error"]:
        st.error(
            f"Re-extraction stopped on an unexpected error: {summary['unexpected_error']}"
        )
    with st.container(horizontal=True, vertical_alignment="top"):
        if lines or summary["error"] or summary["unexpected_error"]:
            st.warning(body)
        else:
            st.success(body)
        if st.button("Dismiss", key="inbox_reextract_summary_dismiss"):
            st.session_state["inbox_reextract_summary"] = None
            st.rerun()


@st.fragment(run_every=1)
def _reextract_progress() -> None:
    batch = st.session_state.get("inbox_reextract_batch")
    if batch is None:
        return
    if batch["done"]:
        # A fragment rerun only redraws itself — rerun the whole app so the
        # list and the review panel pick up the replaced rows.
        st.rerun()
    with batch["lock"]:
        progress = dict(batch["progress"])
    is_stopping = batch["stop_event"].is_set()
    total = progress["total"]
    with st.container(horizontal=True, vertical_alignment="center"):
        st.progress(
            progress["done"] / total if total else 0.0,
            text=f"Re-extracting {progress['done']}/{total}…",
        )
        # on_click runs before the (fragment) rerun, so the label flips to
        # "Stopping…" at once, without a scope-sensitive st.rerun here.
        st.button(
            "Stopping…" if is_stopping else "Stop",
            key="inbox_reextract_stop",
            disabled=is_stopping,
            on_click=batch["stop_event"].set,
        )


def _render_inline_findings(findings: list[ReviewFinding], field_ref: str) -> None:
    """Render the validation findings anchored to `field_ref` (e.g.
    "field:norm_variant", "field:assessment_note") directly under the widget
    they're about, instead of only in the panel's top status list.
    """
    for finding in findings:
        if finding.field_ref != field_ref:
            continue
        alert = _ALERT_BY_SEVERITY[finding.severity]
        alert(finding.message)


_ALERT_BY_SEVERITY = {"error": st.error, "warning": st.warning, "info": st.info}


def _render_inline_findings_by_prefix(
    findings: list[ReviewFinding],
    prefix: str,
    computed_summary: list[tuple[str, str]] | None = None,
    group_label: str | None = None,
    sum_summary: list[tuple[str, str]] | None = None,
) -> None:
    """Like `_render_inline_findings`, but groups every finding whose
    `field_ref` starts with `prefix` (e.g. "metric:", "subtest:") — used
    above the metrics/subtests `data_editor` tables, which have no per-row
    hook to anchor a finding to a single cell.

    `computed_summary` is `[(label, display_value), ...]` built by the
    caller from the stored rows (not from finding messages, which can be
    stale relative to the just-saved rows) — every field it names is
    collapsed into a single neutral line instead of one `info` alert per
    cell (ADR 0013). Any other `info` finding under this prefix (e.g.
    rw_total's "computed as sum" note, deliberately excluded from the
    collapsed line because it wasn't looked up in a norm table) still
    renders individually.

    `sum_summary` (ADR 0015) is the same idea for RW sums the app computed
    (`rw_provenance`/sum-target `value_provenance` == 'computed'), collapsed
    into its own „Summen: …“ line — kept apart from norm lookups, and keyed
    on provenance columns, never on finding message text.
    """
    computed_summary = computed_summary or []
    sum_summary = sum_summary or []
    collapsed_labels = {label for label, _value in computed_summary} | {
        label for label, _value in sum_summary
    }
    for finding in findings:
        if finding.field_ref is None or not finding.field_ref.startswith(prefix):
            continue
        label = finding.field_ref.split(":", 1)[1]
        if finding.severity == "info":
            if label in collapsed_labels:
                continue
            st.info(f"**{label}**: {finding.message}")
            continue
        alert = _ALERT_BY_SEVERITY[finding.severity]
        alert(f"**{label}**: {finding.message}")
    if computed_summary:
        items = [f"**{label}** {value}" for label, value in computed_summary]
        st.info(f"Aus Normtabelle {group_label or '—'}: " + " · ".join(items))
    if sum_summary:
        items = [f"**{label}** {value}" for label, value in sum_summary]
        st.info("Summen: " + " · ".join(items))


def _metric_sum_targets(definition: TestDefinition | None) -> set[str]:
    """Metric keys filled by arithmetic, not by a norm lookup (ADR 0013/0015)."""
    targets = {"rw_total"}
    if definition is not None:
        targets |= {r.metric for r in definition.sum_rules_metric() if r.metric}
    return targets


def _sum_summaries(
    subtest_rows: list[dict[str, Any]],
    metrics_rows: list[dict[str, Any]],
    definition: TestDefinition | None,
) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """(subtest sums, metric sums) as `[(label, "7 = ZS+AD+SU+MU"), ...]`,
    built from the stored provenance columns."""
    over_by_target: dict[str, str] = {}
    if definition is not None:
        for rule in definition.rules_sum:
            target = rule.subtest or rule.metric
            if target is None:
                continue
            over_by_target[target] = (
                "Subtest-RW" if rule.is_legacy else "+".join(definition.over_keys(rule))
            )
    subtest_sums = [
        (
            r["subtest_key"],
            f"{r.get('value_raw')} = {over_by_target.get(r['subtest_key'], '…')}",
        )
        for r in subtest_rows
        if r.get("rw_provenance") == "computed"
    ]
    metric_targets = _metric_sum_targets(definition)
    metric_sums = [
        (
            # The label doubles as the finding-collapse key (field_ref suffix).
            r["metric_key"],
            f"{r.get('value_raw')} = {over_by_target.get(r['metric_key'], '…')}",
        )
        for r in metrics_rows
        if r.get("value_provenance") == "computed" and r["metric_key"] in metric_targets
    ]
    return subtest_sums, metric_sums


def _has_computed_sum(
    subtest_rows: list[dict[str, Any]],
    metrics_rows: list[dict[str, Any]],
    definition: TestDefinition | None,
) -> bool:
    subtest_sums, metric_sums = _sum_summaries(subtest_rows, metrics_rows, definition)
    return bool(subtest_sums or metric_sums)


def _relative_date(sqlite_utc: str) -> str:
    """Compact relative time (e.g. "2d ago") for a SQLite `datetime('now')`
    timestamp — naive, UTC. Compares against UTC "now", not local time, to
    avoid an offset-driven "in 1 hour" right after generation.
    """
    try:
        dt = datetime.fromisoformat(sqlite_utc)
    except ValueError:
        return sqlite_utc
    seconds = (datetime.now(UTC).replace(tzinfo=None) - dt).total_seconds()
    if seconds < 60:
        return "just now"
    minutes = seconds / 60
    if minutes < 60:
        return f"{int(minutes)}min ago"
    hours = minutes / 60
    if hours < 24:
        return f"{int(hours)}h ago"
    days = int(hours / 24)
    if days < 30:
        return f"{days}d ago"
    months = days // 30
    if months < 12:
        return f"{months}mo ago"
    return f"{days // 365}y ago"


def _norm(value: object) -> object:
    """Normalize a value for edit-diffing: NaN/empty-string collapse to
    None so a round-tripped-but-unchanged `st.data_editor` cell doesn't
    look like an edit.
    """
    if value is None:
        return None
    if isinstance(value, float) and value != value:  # NaN != NaN
        return None
    if isinstance(value, str) and value == "":
        return None
    return value


# Read-only "Quelle" column labels for the metrics/subtests editors (ADR
# 0013) — `sheet`/NULL both mean "written on the sheet", `blank` means
# "left blank on the sheet and not (yet) completed".
_QUELLE_LABELS = {
    None: "Bogen",
    "sheet": "Bogen",
    "computed": "Normtabelle",
    "unavailable": "nicht veröffentlicht",
    "manual": "manuell",
    "blank": "offen",
}
# Same idea for a subtest's RW cell (ADR 0015): only a sum-target subtest's
# RW is ever computed — as a sum, not a lookup.
_QUELLE_RW_LABELS = {**_QUELLE_LABELS, "computed": "Summe"}


def _resolved_norm_group_label(
    definition: TestDefinition | None, norm_variant: str | None
) -> str | None:
    """The same norm-group resolution `norm_completion.plan_completion` and
    `review_validation._check_norm_completion` use, so the UI's group label
    can't disagree with the stored values (ADR 0013).
    """
    if definition is None or definition.norm_test is None or not norm_variant:
        return None
    try:
        group, _label, _variant = resolve_norm_group(definition.norm_test, norm_variant)
    except ValueError:
        return None
    return group


@st.dialog("Source document", width="large")
def _open_source_dialog(assessment: dict) -> None:
    if assessment.get("page_image_path"):
        st.image(assessment["page_image_path"])
    stored_pdf_path = assessment.get("stored_pdf_path")
    if stored_pdf_path and Path(stored_pdf_path).exists():
        st.download_button(
            "Download original PDF",
            data=Path(stored_pdf_path).read_bytes(),
            file_name=Path(stored_pdf_path).name,
            mime="application/pdf",
            key=f"download_pdf_{assessment['assessment_id']}",
        )
    else:
        st.caption(
            "Original PDF not available for this assessment (uploaded before "
            "PDF retention, or not yet retained)."
        )


error_banner = st.empty()

try:
    if page == "Load Assessments":
        uploaded_files = st.file_uploader(
            "Upload assessment PDFs",
            type="pdf",
            accept_multiple_files=True,
        )

        # One Normtabelle selector per uploaded *file* (ADR 0010): a file's
        # norm_test isn't known until extraction runs (triage happens per
        # sheet, not per upload), so options can't be scoped to the file's
        # actual norm_test in advance. Instead every variant of every active
        # test is offered, labelled with its test, and the choice is applied
        # at save time only if it turns out valid for the sheet's actual
        # resolved norm_test (demat/pipeline.py); a mismatch is left unset,
        # correctable per-assessment in review. A per-`norm_test` selector
        # (the original design here) was rejected: it would force one
        # variant onto every sheet of that version across the whole batch,
        # even when two files of the same version need different variants
        # (e.g. one DEMAT 3+ file scored "Ende Klassenstufe 3", another
        # "Anfang Klassenstufe 4").
        all_variant_options: list[tuple[str, str]] = [
            (norm_test, v.key)
            for norm_test in sorted(norm_variants.VARIANTS)
            for v in norm_variants.variants_for(norm_test)
        ]

        def _format_variant_option(
            opt: tuple[str, str] | str,
        ) -> str:
            if not isinstance(opt, tuple):
                return "— (nicht angegeben)"
            norm_test, key = opt
            return f"{norm_test}: {norm_variants.label_for(norm_test, key)} ({key})"

        norm_variants_by_file: dict[str, str] = {}
        if uploaded_files:
            st.write(f"{len(uploaded_files)} file(s) uploaded:")
            for uploaded_file in uploaded_files:
                file_sha256 = hashlib.sha256(uploaded_file.getvalue()).hexdigest()
                file_col, variant_col = st.columns([2, 3])
                with file_col:
                    st.write(uploaded_file.name)
                with variant_col:
                    chosen = st.selectbox(
                        "Normtabelle",
                        options=["", *all_variant_options],
                        format_func=_format_variant_option,
                        index=0,
                        key=f"upload_norm_variant_{file_sha256}",
                        label_visibility="collapsed",
                        help=(
                            "Welche gedruckte Normtabelle die Bögen in dieser "
                            "Datei ausgewertet wurden — korrigierbar pro Bogen "
                            "in der Prüfung."
                        ),
                    )
                    if chosen:
                        norm_variants_by_file[file_sha256] = chosen[1]
            if not norm_variants_by_file:
                st.info(
                    "Keine Normtabelle ausgewählt — die RW→PR-Prüfung wird für "
                    "diese Bögen übersprungen, bis eine Normtabelle gesetzt wird."
                )

        if "last_results" not in st.session_state:
            st.session_state["last_results"] = {}
        if "extraction_batch" not in st.session_state:
            st.session_state["extraction_batch"] = None

        extract_clicked = st.button(
            "Extract",
            disabled=not uploaded_files
            or st.session_state["extraction_batch"] is not None,
        )

        if extract_clicked:
            runner = get_default_runner()
            pdf_inputs = []
            for uploaded_file in uploaded_files:
                pdf_bytes = uploaded_file.getvalue()
                file_sha256 = hashlib.sha256(pdf_bytes).hexdigest()
                # Retain the uploaded PDF (ADR 0007) so the Inbox review
                # panel can offer the original document later.
                store.save_uploaded_document(file_sha256, uploaded_file.name, pdf_bytes)
                pdf_inputs.append(PdfInput(uploaded_file.name, pdf_bytes, file_sha256))

            # Extraction runs on a background thread, not the main script
            # thread, so the "Stop" button below can actually be clicked
            # while it's in progress: Streamlit only processes a widget
            # click on the *next* script run, and a run blocked inside
            # process_pdfs() would never let that next run start. The main
            # thread instead polls this dict (a plain object mutated by the
            # background thread — see process_pdfs' `stop_event`) and
            # reruns itself every 0.5s until `done` is set.
            batch: dict[str, Any] = {
                "thread": None,
                "stop_event": threading.Event(),
                "lock": threading.Lock(),
                "progress": {"done": 0, "total": len(pdf_inputs), "label": "starting…"},
                "results": None,
                "error": None,
                "done": False,
            }

            def _progress_cb(
                done: int, total: int, label: str, _batch: dict = batch
            ) -> None:
                with _batch["lock"]:
                    _batch["progress"] = {"done": done, "total": total, "label": label}

            def _run_batch(
                _pdf_inputs: list[PdfInput] = pdf_inputs,
                _runner: SkillRunner = runner,
                _norm_variants_by_file: dict[str, str] = norm_variants_by_file,
                _batch: dict = batch,
            ) -> None:
                try:
                    _batch["results"] = process_pdfs(
                        _pdf_inputs,
                        _runner,
                        progress_cb=_progress_cb,
                        norm_variants_by_file=_norm_variants_by_file,
                        stop_event=_batch["stop_event"],
                    )
                except RunnerUnavailableError as exc:
                    _batch["error"] = str(exc)
                finally:
                    _batch["done"] = True

            batch["thread"] = threading.Thread(target=_run_batch, daemon=True)
            st.session_state["extraction_batch"] = batch
            batch["thread"].start()
            st.rerun()

        batch = st.session_state["extraction_batch"]
        if batch is not None:
            with batch["lock"]:
                progress = dict(batch["progress"])
            is_done = batch["done"]
            is_stopping = batch["stop_event"].is_set()
            with st.status(
                "Extraction complete" if is_done else "Extracting...",
                expanded=True,
                state="complete" if is_done else "running",
            ):
                st.progress(
                    progress["done"] / progress["total"] if progress["total"] else 0.0,
                    text=f"page {progress['done']}/{progress['total']} — "
                    f"{progress['label']}",
                )
                if not is_done and st.button(
                    "Stopping…" if is_stopping else "Stop",
                    key="stop_extraction",
                    disabled=is_stopping,
                ):
                    batch["stop_event"].set()
                    st.rerun()

            if is_done:
                if batch["error"]:
                    st.error(
                        "The default extraction runner (Claude Agent SDK, via the `claude` "
                        "CLI) is not available in this environment.\n\n"
                        f"{batch['error']}\n\n"
                        "Fix: install the `claude` CLI and Node.js, or set the environment "
                        "variable `DEMAT_RUNNER=direct` to use the DirectMessagesRunner "
                        "fallback (requires `ANTHROPIC_API_KEY`) and restart the app."
                    )
                else:
                    for result in batch["results"]:
                        st.session_state["last_results"][result.file_sha256] = result
                    if is_stopping:
                        st.warning(
                            "Extraction stopped before all pages were processed."
                        )
                st.session_state["extraction_batch"] = None
            else:
                time.sleep(0.5)
                st.rerun()

        if st.session_state["last_results"]:
            st.subheader("Last extraction summary")
            for _file_sha256, pdf_result in st.session_state["last_results"].items():
                saved = [s for s in pdf_result.sheets if s.status == "saved"]
                duplicates = [
                    s for s in pdf_result.sheets if s.status == "skipped_duplicate"
                ]
                not_sheets = [
                    s for s in pdf_result.sheets if s.status == "skipped_not_sheet"
                ]
                errors = [s for s in pdf_result.sheets if s.status == "error"]
                needs_review = [s for s in saved if s.has_blocking_flags]

                st.markdown(f"**{pdf_result.file_name}**")
                st.write(
                    f"saved: {len(saved)} · duplicates skipped: {len(duplicates)} · "
                    f"not a sheet: {len(not_sheets)} · needs review: {len(needs_review)} · "
                    f"errors: {len(errors)}"
                )
                for s in needs_review:
                    st.warning(
                        f"page {s.page_index}: person_id={s.person_id} "
                        f"skill={s.skill_used} flags={s.flags}"
                    )
                for s in errors:
                    st.error(f"page {s.page_index}: {s.error} (skill={s.skill_used})")

    elif page == "Assessment Review":
        # Before anything reads the selection or the list: a finished
        # re-extract batch remaps old ids to new ones (ADR 0016).
        _finish_reextract_batch()

        review_layout = st.container(key="inbox_review_layout")
        left, right = review_layout.columns([1, 2])

        with left:
            filter_col, sort_col = st.columns(2)
            with filter_col:
                status_filter = st.multiselect(
                    "Status",
                    options=[
                        "needs_review",
                        "validated_ok",
                        "validated_with_changes",
                        "rejected",
                    ],
                    default=["needs_review"],
                    key="rv_status",
                    bind="query-params",
                    persist_state="session",
                )
            with sort_col:
                sort_order = st.selectbox(
                    "Sort by created date",
                    options=["Newest first", "Oldest first"],
                    key="rv_sort",
                    bind="query-params",
                    persist_state="session",
                )
            validation_filter = st.multiselect(
                "Validation",
                options=["Not run", "0 errors", ">0 errors"],
                default=["Not run", "0 errors", ">0 errors"],
                key="rv_validation",
                bind="query-params",
                persist_state="session",
            )
            duplicates_only = st.checkbox(
                "Duplicates only",
                key="rv_duplicates",
                bind="query-params",
                persist_state="session",
            )
            inbox_rows = store.list_inbox_assessments(
                statuses=status_filter or None,
                order="asc" if sort_order == "Oldest first" else "desc",
            )
            if duplicates_only:
                inbox_rows = [
                    r for r in inbox_rows if "duplicate_suspected" in r["flags"]
                ]
            if len(validation_filter) < 3:

                def _validation_bucket(r: dict) -> str:
                    if r["review_validation_at"] is None:
                        return "Not run"
                    findings = findings_from_json(r["review_validation_json"])
                    has_errors = any(f.severity == "error" for f in findings)
                    return ">0 errors" if has_errors else "0 errors"

                inbox_rows = [
                    r for r in inbox_rows if _validation_bucket(r) in validation_filter
                ]
            unread_count = sum(1 for r in inbox_rows if r["opened_at"] is None)
            st.caption(f"{len(inbox_rows)} shown · {unread_count} unread")

            # Outside the `inbox_rows` check: replaced rows can drop out of
            # the current filter mid-batch, and the poller must keep running.
            reextract_active = _reextract_batch_active()
            if reextract_active:
                _reextract_progress()
            else:
                _render_reextract_summary()

            if not inbox_rows:
                st.info("No assessments match the selected filters.")
            else:
                visible_ids = [row["assessment_id"] for row in inbox_rows]
                st.session_state["inbox_visible_ids"] = visible_ids
                selected_ids = [
                    aid
                    for aid in visible_ids
                    if st.session_state.get(f"inbox_select_{aid}", False)
                ]

                def _toggle_select_all() -> None:
                    new_value = st.session_state["inbox_select_all"]
                    for aid in st.session_state.get("inbox_visible_ids", []):
                        st.session_state[f"inbox_select_{aid}"] = new_value

                batch_running_key = "inbox_batch_revalidate_running"
                is_batch_running = st.session_state.get(batch_running_key, False)
                delete_confirm_key = "inbox_batch_delete_confirm"
                is_delete_confirming = st.session_state.get(delete_confirm_key, False)
                force_validate_confirm_key = "inbox_batch_force_validate_confirm"
                is_force_validate_confirming = st.session_state.get(
                    force_validate_confirm_key, False
                )
                reextract_confirm_key = "inbox_batch_reextract_confirm"
                is_reextract_confirming = st.session_state.get(
                    reextract_confirm_key, False
                )

                if reextract_active:
                    # The progress block above replaces the batch bar; no
                    # second batch (or any other batch action) meanwhile.
                    pass
                elif is_reextract_confirming:
                    selected_rows = [
                        row
                        for row in (store.get_assessment(aid) for aid in selected_ids)
                        if row is not None
                    ]
                    n_edited = sum(1 for row in selected_rows if row.get("edited_at"))
                    n_final = sum(
                        1
                        for row in selected_rows
                        if row["status"] in ("validated_ok", "validated_with_changes")
                    )
                    n_rejected = sum(
                        1 for row in selected_rows if row["status"] == "rejected"
                    )
                    counts = (
                        f": {n_edited} edited · {n_final} already validated · "
                        f"{n_rejected} rejected"
                        if n_edited or n_final or n_rejected
                        else ""
                    )
                    with st.container(
                        horizontal=True,
                        horizontal_alignment="distribute",
                        vertical_alignment="center",
                    ):
                        st.warning(
                            f"Re-extract {len(selected_ids)} assessment(s) from "
                            "their stored source pages? Each row is replaced by a "
                            "fresh extraction with a new ID and goes back to "
                            "*needs_review*. The child link and the Normtabelle "
                            "selection are kept. **Manual edits, notes and review "
                            f"results on these rows are discarded**{counts}. If "
                            "extraction fails for a row, that row is left "
                            "unchanged."
                        )
                        with st.container(horizontal=True, gap="small"):
                            if st.button("Cancel", key="inbox_batch_reextract_cancel"):
                                st.session_state[reextract_confirm_key] = False
                                st.rerun()
                            if st.button(
                                "Confirm re-extract",
                                key="inbox_batch_reextract_confirm_btn",
                                type="primary",
                            ):
                                st.session_state[reextract_confirm_key] = False
                                try:
                                    _start_reextract_batch(selected_ids)
                                except RunnerUnavailableError as exc:
                                    st.error(_runner_unavailable_message(str(exc)))
                                else:
                                    st.rerun()
                elif is_force_validate_confirming:
                    with st.container(
                        horizontal=True,
                        horizontal_alignment="distribute",
                        vertical_alignment="center",
                    ):
                        st.warning(
                            f"Force-validate {len(selected_ids)} assessment(s)? "
                            "This finalizes them as validated **even if they have "
                            "blocking validation errors, or validation has never "
                            "been run** — it bypasses every safeguard the normal "
                            "Validate button enforces. This cannot be undone."
                        )
                        with st.container(horizontal=True, gap="small"):
                            if st.button(
                                "Cancel", key="inbox_batch_force_validate_cancel"
                            ):
                                st.session_state[force_validate_confirm_key] = False
                                st.rerun()
                            if st.button(
                                "Confirm force-validate",
                                key="inbox_batch_force_validate_confirm_btn",
                                type="primary",
                            ):
                                for aid in selected_ids:
                                    try:
                                        store.finalize_assessment(aid)
                                    except ValueError:
                                        # Already terminal — nothing to force.
                                        continue
                                    st.session_state.pop(f"inbox_select_{aid}", None)
                                    if (
                                        st.session_state.get(
                                            "inbox_selected_assessment"
                                        )
                                        == aid
                                    ):
                                        st.session_state[
                                            "inbox_selected_assessment"
                                        ] = None
                                st.session_state[force_validate_confirm_key] = False
                                st.rerun()
                elif is_delete_confirming:
                    with st.container(
                        horizontal=True,
                        horizontal_alignment="distribute",
                        vertical_alignment="center",
                    ):
                        st.warning(
                            f"Delete {len(selected_ids)} assessment(s)? This also "
                            "removes each one's page image and skill-run output, "
                            "and cannot be undone."
                        )
                        with st.container(horizontal=True, gap="small"):
                            if st.button("Cancel", key="inbox_batch_delete_cancel"):
                                st.session_state[delete_confirm_key] = False
                                st.rerun()
                            if st.button(
                                "Confirm delete",
                                key="inbox_batch_delete_confirm_btn",
                                type="primary",
                            ):
                                store.delete_assessments(selected_ids)
                                for aid in selected_ids:
                                    st.session_state.pop(f"inbox_select_{aid}", None)
                                    if (
                                        st.session_state.get(
                                            "inbox_selected_assessment"
                                        )
                                        == aid
                                    ):
                                        st.session_state[
                                            "inbox_selected_assessment"
                                        ] = None
                                st.session_state[delete_confirm_key] = False
                                st.rerun()
                else:
                    with st.container(
                        horizontal=True,
                        horizontal_alignment="distribute",
                        vertical_alignment="center",
                    ):
                        st.checkbox(
                            "Select all",
                            key="inbox_select_all",
                            on_change=_toggle_select_all,
                        )
                        with st.container(
                            horizontal=True,
                            vertical_alignment="center",
                            gap="small",
                        ):
                            batch_action = st.selectbox(
                                "Batch action",
                                options=[
                                    "Revalidate",
                                    "Re-extract",
                                    "Validate (forced)",
                                    "Delete",
                                ],
                                key="inbox_batch_action",
                                label_visibility="collapsed",
                            )
                            if st.button(
                                "Running…"
                                if is_batch_running
                                else f"Apply ({len(selected_ids)})",
                                key="inbox_batch_apply",
                                disabled=is_batch_running or not selected_ids,
                            ):
                                st.session_state["inbox_reextract_summary"] = None
                                if batch_action == "Delete":
                                    st.session_state[delete_confirm_key] = True
                                elif batch_action == "Re-extract":
                                    st.session_state[reextract_confirm_key] = True
                                elif batch_action == "Validate (forced)":
                                    st.session_state[force_validate_confirm_key] = True
                                else:
                                    st.session_state[batch_running_key] = True
                                st.rerun()

                    if is_batch_running:
                        try:
                            runner = get_default_runner()
                            with st.spinner(
                                f"Revalidating {len(selected_ids)} assessment(s)..."
                            ):
                                for aid in selected_ids:
                                    batch_assessment = store.get_assessment(aid)
                                    if batch_assessment is None:
                                        continue
                                    batch_result = run_review_validations(
                                        batch_assessment,
                                        store.get_assessment_metrics(aid),
                                        store.get_subtest_scores(aid),
                                        store.get_test_definition(
                                            batch_assessment.get("test_version")
                                        ),
                                        runner,
                                    )
                                    store.save_review_validation(
                                        aid,
                                        findings_to_json(batch_result.findings),
                                        batch_assessment.get("edited_at"),
                                    )
                        except RunnerUnavailableError as exc:
                            st.error(f"Revalidation runner unavailable: {exc}")
                        finally:
                            st.session_state[batch_running_key] = False
                            st.rerun()

                with st.container(key="inbox_list", height="stretch"):
                    for row in inbox_rows:
                        assessment_id = row["assessment_id"]
                        person_label = (
                            f"`{row['person_id']}`"
                            if row["person_id"]
                            else f"unlinked #{assessment_id}"
                        )
                        label = (
                            f"{person_label} · {row['test_version'] or '—'} · "
                            f"{row['test_date_iso'] or '—'}"
                        )
                        badges = []
                        if row["status"] in (
                            "validated_ok",
                            "validated_with_changes",
                        ):
                            badges.append("✅")
                        elif row["status"] == "rejected":
                            badges.append("❌")
                        if row["review_validation_at"] is not None and any(
                            f.severity == "error"
                            for f in findings_from_json(row["review_validation_json"])
                        ):
                            if "❌" not in badges:
                                badges.append("❌")
                        if any(flag_severity(f) != "info" for f in row["flags"]):
                            badges.append("⚠️")
                        if "norm_values_computed" in row["flags"]:
                            badges.append("🧮")
                        if row["edited_at"]:
                            badges.append("✏️")

                        is_unread = row["opened_at"] is None
                        display_label = f"**{label}**" if is_unread else label

                        with st.container(
                            horizontal=True,
                            horizontal_alignment="distribute",
                            vertical_alignment="center",
                        ):
                            st.checkbox(
                                "Select",
                                key=f"inbox_select_{assessment_id}",
                                label_visibility="collapsed",
                                width="content",
                            )
                            st.button(
                                display_label,
                                key=f"inbox_row_{assessment_id}",
                                width="stretch",
                                type=(
                                    "primary"
                                    if st.session_state["inbox_selected_assessment"]
                                    == assessment_id
                                    else "secondary"
                                ),
                                on_click=_select_inbox_row,
                                args=(assessment_id,),
                            )
                            if badges:
                                st.markdown(
                                    " ".join(badges),
                                    width="content",
                                    help=(
                                        "Werte berechnet/nachgeschlagen"
                                        if "🧮" in badges
                                        else None
                                    ),
                                )

        with right:
            with st.container(border=True, height="stretch", key="inbox_review_panel"):
                selected_id = st.session_state["inbox_selected_assessment"]
                reextract_batch = st.session_state.get("inbox_reextract_batch")
                if selected_id is None:
                    st.info("Select an assessment from the list to review it.")
                elif (
                    reextract_batch is not None
                    and selected_id in reextract_batch["ids"]
                ):
                    # Before the "no longer exists" branch: a row replaced
                    # mid-batch must keep its selection until the remap.
                    st.info(
                        "Re-extraction in progress for this assessment — editing "
                        "is disabled until it finishes."
                    )
                else:
                    assessment = store.get_assessment(selected_id)
                    if assessment is None:
                        st.session_state["inbox_selected_assessment"] = None
                        st.info("This assessment no longer exists.")
                    else:
                        is_read_only = assessment["status"] != "needs_review"
                        metrics_rows = store.get_assessment_metrics(selected_id)
                        subtest_rows = store.get_subtest_scores(selected_id)
                        definition = store.get_test_definition(
                            assessment.get("test_version")
                        )
                        norm_group_label = _resolved_norm_group_label(
                            definition, assessment.get("norm_variant")
                        )

                        person_label = (
                            f"`{assessment['person_id']}`"
                            if assessment["person_id"]
                            else f"unlinked #{selected_id}"
                        )
                        validate_label = (
                            "Validate (with changes)"
                            if assessment.get("edited_at")
                            else "Validate"
                        )
                        reject_confirm_key = f"reject_confirm_{selected_id}"

                        review_validation_findings = findings_from_json(
                            assessment.get("review_validation_json")
                        )
                        has_run_validation = (
                            assessment.get("review_validation_at") is not None
                        )
                        validation_stale = assessment.get(
                            "review_validation_edited_at"
                        ) != assessment.get("edited_at")
                        can_validate = (
                            has_run_validation
                            and not ReviewValidationResult(
                                findings=review_validation_findings
                            ).has_errors
                        )

                        def _run_review_validation(
                            _assessment_id: int = selected_id,
                        ) -> None:
                            # get_default_runner() first, same as before this
                            # function grew a completion step — if the runner
                            # is unavailable, fail before writing anything.
                            runner = get_default_runner()
                            # Fill any norm cells the sheet left blank before
                            # validating, so the validation reflects the
                            # completed row, not the pre-completion one
                            # (ADR 0013).
                            try:
                                norm_completion.run_completion(_assessment_id)
                            except Exception:
                                logger.exception(
                                    "norm_completion.run_completion failed for "
                                    "assessment_id=%s",
                                    _assessment_id,
                                )
                            _assessment = store.get_assessment(_assessment_id)
                            if _assessment is None:
                                return
                            _metrics_rows = store.get_assessment_metrics(_assessment_id)
                            _subtest_rows = store.get_subtest_scores(_assessment_id)
                            _definition = store.get_test_definition(
                                _assessment.get("test_version")
                            )
                            validation_result = run_review_validations(
                                _assessment,
                                _metrics_rows,
                                _subtest_rows,
                                _definition,
                                runner,
                            )
                            store.save_review_validation(
                                _assessment_id,
                                findings_to_json(validation_result.findings),
                                _assessment.get("edited_at"),
                            )

                        with st.container(
                            horizontal=True,
                            horizontal_alignment="distribute",
                            vertical_alignment="center",
                        ):
                            st.subheader(
                                f"{person_label} · {assessment.get('test_version') or '—'}",
                                width="content",
                            )
                            if st.session_state.get(reject_confirm_key):
                                with st.container(
                                    horizontal=True,
                                    horizontal_alignment="right",
                                    gap="small",
                                ):
                                    if st.button(
                                        "Cancel", key=f"reject_cancel_{selected_id}"
                                    ):
                                        st.session_state.pop(reject_confirm_key, None)
                                        st.rerun()
                                    if st.button(
                                        "Confirm reject",
                                        key=f"reject_confirm_btn_{selected_id}",
                                        type="primary",
                                    ):
                                        try:
                                            store.finalize_assessment(
                                                selected_id, rejected=True
                                            )
                                        except ValueError as exc:
                                            st.error(str(exc))
                                        else:
                                            st.session_state.pop(
                                                reject_confirm_key, None
                                            )
                                            st.session_state[
                                                "inbox_selected_assessment"
                                            ] = None
                                            st.rerun()
                            else:
                                with st.container(
                                    horizontal=True,
                                    horizontal_alignment="right",
                                    gap="small",
                                ):
                                    running_key = (
                                        f"run_validation_running_{selected_id}"
                                    )
                                    is_running = st.session_state.get(
                                        running_key, False
                                    )
                                    if st.button(
                                        "Running…"
                                        if is_running
                                        else (
                                            "Run validation"
                                            if not has_run_validation
                                            else "Re-run validation"
                                        ),
                                        key=f"run_validation_{selected_id}",
                                        disabled=is_running,
                                    ):
                                        st.session_state[running_key] = True
                                        st.rerun()
                                    if is_running:
                                        _run_review_validation()
                                        st.session_state[running_key] = False
                                        st.rerun()
                                    if st.button("Reject", key=f"reject_{selected_id}"):
                                        st.session_state[reject_confirm_key] = True
                                        st.rerun()
                                    with st.container(
                                        key="validate_action", width="content"
                                    ):
                                        if st.button(
                                            validate_label,
                                            key=f"validate_{selected_id}",
                                            type="primary",
                                            disabled=not can_validate,
                                            help=(
                                                None
                                                if can_validate
                                                else "Run validation (above), with no "
                                                "blocking errors, before finalizing."
                                            ),
                                        ):
                                            try:
                                                store.finalize_assessment(selected_id)
                                            except ValueError as exc:
                                                st.error(str(exc))
                                            else:
                                                st.session_state[
                                                    "inbox_selected_assessment"
                                                ] = None
                                                st.rerun()

                        klasse_schuljahre_label = klasse_schuljahre(
                            assessment.get("klasse"), assessment.get("test_date_iso")
                        )
                        with st.container(horizontal=True, vertical_alignment="center"):
                            st.caption(
                                f"Name: {assessment.get('person_name') or '—'}"
                                f"  ·  Klasse/Schuljahr: "
                                f"{klasse_schuljahre_label or '—'}"
                            )
                            if assessment.get("edited_at"):
                                with st.container(
                                    key="review_edited_marker", width="content"
                                ):
                                    st.caption("Fields were edited in this review ✏️")

                        if st.session_state.get(reject_confirm_key):
                            st.warning(
                                "**This will permanently reject this assessment.**"
                            )

                        st.write("**Status**")
                        # Findings with a field_ref render inline, next to that
                        # field in the edit form below — only flags (which have
                        # no field to anchor to) and unattributed findings stay
                        # in this top-of-panel list.
                        unattributed_findings = [
                            f for f in review_validation_findings if f.field_ref is None
                        ]
                        status_items = [
                            (flag_severity(flag), "flag", flag)
                            for flag in assessment["flags"]
                        ] + [
                            (finding.severity, finding.check, finding.message)
                            for finding in unattributed_findings
                        ]
                        if assessment.get("review_notes"):
                            status_items.append(
                                ("warning", "review notes", assessment["review_notes"])
                            )
                        error_items = [i for i in status_items if i[0] == "error"]
                        warning_items = [i for i in status_items if i[0] == "warning"]
                        blocking_findings = [
                            f
                            for f in review_validation_findings
                            if f.severity != "info"
                        ]

                        if st.session_state.pop(
                            f"norm_completion_failed_{selected_id}", False
                        ):
                            st.warning(
                                "Normwerte konnten nicht nachgeschlagen werden "
                                "(siehe Log)."
                            )
                        if has_run_validation and validation_stale:
                            st.info(
                                "These results are from before the last save — "
                                "re-run validation (above) to confirm, or finalize "
                                "as-is."
                            )
                        if error_items:
                            # Always visible — never collapsed, unlike warnings
                            # below — and grouped together instead of
                            # interleaved by discovery order.
                            st.error(
                                f"{len(error_items)} validation error"
                                f"{'s' if len(error_items) != 1 else ''} found."
                            )
                            for _severity, label, message in error_items:
                                st.error(f"**{label}**: {message}")
                        # Neutral, never-blocking notes (ADR 0013/0015) — values
                        # the app filled in, not something the evaluator needs
                        # to act on. A norm lookup and an RW sum are worded
                        # separately: a sum needs no Normtabelle.
                        if "norm_values_computed" in assessment["flags"]:
                            if store.has_computed_lookup(
                                subtest_rows, metrics_rows, definition
                            ):
                                st.caption(
                                    "Normwerte aus Normtabelle "
                                    f"{norm_group_label or '—'} nachgeschlagen "
                                    "(nicht handschriftlich auf dem Bogen)."
                                )
                            if _has_computed_sum(
                                subtest_rows, metrics_rows, definition
                            ):
                                st.caption(
                                    "Rohwert-Summen aus den Einzelwerten berechnet."
                                )
                        if warning_items:
                            with st.expander(
                                f"⚠️ Warnings ({len(warning_items)})", expanded=False
                            ):
                                for _severity, label, message in warning_items:
                                    st.warning(f"**{label}**: {message}")
                        if not error_items and not warning_items:
                            if has_run_validation and not blocking_findings:
                                st.success("All checks passed.")
                            elif has_run_validation:
                                st.caption(
                                    "See the inline notes next to the affected "
                                    "fields below."
                                )
                        if not has_run_validation:
                            st.caption(
                                "Not yet validated. Run validation (above) before "
                                "finalizing this assessment."
                            )

                        preview_col, detail_col = st.columns([2, 3])

                        with preview_col:
                            if assessment.get("page_image_path"):
                                st.image(assessment["page_image_path"], width="stretch")
                            else:
                                st.caption(
                                    "No page image available for this assessment."
                                )
                            if st.button(
                                "Open source document", key=f"open_source_{selected_id}"
                            ):
                                _open_source_dialog(assessment)

                        with detail_col:
                            with st.form(key=f"edit_form_{selected_id}"):
                                name_editable = config.STORE_RAW_NAMES
                                name = st.text_input(
                                    "Name",
                                    value=assessment.get("person_name") or "",
                                    key=f"edit_{selected_id}_name",
                                    disabled=not name_editable,
                                    help=(
                                        None
                                        if name_editable
                                        else "Name editing is disabled "
                                        "(DEMAT_STORE_RAW_NAMES=false)."
                                    ),
                                )
                                klasse_col, school_year_col = st.columns(2)
                                with klasse_col:
                                    stored_klasse = assessment.get("klasse") or ""
                                    klasse_options = store.list_known_klassen()
                                    if (
                                        stored_klasse
                                        and stored_klasse not in klasse_options
                                    ):
                                        klasse_options = sorted(
                                            [*klasse_options, stored_klasse]
                                        )
                                    klasse_key = f"edit_{selected_id}_klasse"
                                    # A value accepted as "new" that's only a case
                                    # variant of an existing class (e.g. typed "3c"
                                    # for stored "3C") isn't actually new — snap it
                                    # to the existing uppercase option so it doesn't
                                    # keep showing as an add-new entry.
                                    prior_value = st.session_state.get(klasse_key)
                                    if (
                                        isinstance(prior_value, str)
                                        and prior_value not in klasse_options
                                    ):
                                        upper_match = next(
                                            (
                                                opt
                                                for opt in klasse_options
                                                if opt == prior_value.upper()
                                            ),
                                            None,
                                        )
                                        if upper_match is not None:
                                            st.session_state[klasse_key] = upper_match
                                    prior_value = st.session_state.get(klasse_key)
                                    klasse_new_too_short = (
                                        isinstance(prior_value, str)
                                        and prior_value not in klasse_options
                                        and len(prior_value.strip()) < 2
                                    )
                                    if klasse_new_too_short:
                                        # A new class name needs at least 2 chars to
                                        # be offered as an addition — revert instead
                                        # of letting a stray keystroke get added.
                                        st.session_state[klasse_key] = (
                                            stored_klasse
                                            if stored_klasse in klasse_options
                                            else None
                                        )
                                    klasse = st.selectbox(
                                        "Klasse",
                                        options=klasse_options,
                                        index=(
                                            klasse_options.index(stored_klasse)
                                            if stored_klasse in klasse_options
                                            else None
                                        ),
                                        accept_new_options=True,
                                        placeholder="Klasse wählen oder eingeben",
                                        key=klasse_key,
                                    )
                                    if klasse_new_too_short:
                                        st.caption(
                                            "New class names need at least 2 "
                                            "characters — not added."
                                        )
                                with school_year_col:
                                    st.text_input(
                                        "Schuljahr",
                                        value=school_year(
                                            assessment.get("test_date_iso")
                                        )
                                        or "",
                                        key=f"edit_{selected_id}_school_year",
                                        disabled=True,
                                        help="Derived from the test date; not stored directly.",
                                    )
                                edit_defn = store.get_test_definition(
                                    assessment.get("test_version")
                                )
                                edit_norm_test = (
                                    edit_defn.norm_test if edit_defn else None
                                )
                                stored_norm_variant = assessment.get("norm_variant")
                                norm_variant_options = [
                                    "",
                                    *[
                                        v.key
                                        for v in norm_variants.variants_for(
                                            edit_norm_test
                                        )
                                    ],
                                ]
                                if (
                                    stored_norm_variant
                                    and stored_norm_variant not in norm_variant_options
                                ):
                                    # Stored value not in the current list (KB/table
                                    # drift, or a version edit) — keep it as an extra
                                    # selected option so saving doesn't silently
                                    # discard it.
                                    norm_variant_options.append(stored_norm_variant)
                                norm_variant_disabled = edit_norm_test is None
                                norm_variant_choice = st.selectbox(
                                    "Normtabelle",
                                    options=norm_variant_options,
                                    index=(
                                        norm_variant_options.index(stored_norm_variant)
                                        if stored_norm_variant in norm_variant_options
                                        else 0
                                    ),
                                    format_func=lambda k, nt=edit_norm_test, sv=stored_norm_variant: (
                                        "— (nicht angegeben)"
                                        if k == ""
                                        else (
                                            f"{k} (unbekannt)"
                                            if k == sv
                                            and norm_variants.label_for(nt, k) is None
                                            else f"{norm_variants.label_for(nt, k)} ({k})"
                                        )
                                    ),
                                    key=f"edit_{selected_id}_norm_variant",
                                    disabled=norm_variant_disabled,
                                    help=(
                                        "Test version has no known norm_test."
                                        if norm_variant_disabled
                                        else "Welche gedruckte Normtabelle für diesen "
                                        "Bogen verwendet wurde."
                                    ),
                                )
                                _render_inline_findings(
                                    review_validation_findings, "field:norm_variant"
                                )
                                st.caption(
                                    norm_variants.label_for(
                                        edit_norm_test, stored_norm_variant
                                    )
                                    or "Normtabelle nicht erfasst"
                                )
                                gender_labels = {
                                    "maedchen": "Mädchen",
                                    "junge": "Junge",
                                    "unknown": "unbekannt",
                                }
                                stored_gender = assessment.get("gender") or "unknown"
                                gender_options = ["maedchen", "junge", "unknown"]
                                if stored_gender not in gender_options:
                                    gender_options.append(stored_gender)
                                gender = st.radio(
                                    "Gender",
                                    options=gender_options,
                                    index=gender_options.index(stored_gender),
                                    format_func=lambda g: gender_labels.get(g, g),
                                    key=f"edit_{selected_id}_gender",
                                    horizontal=True,
                                )
                                _render_inline_findings(
                                    review_validation_findings, "field:gender"
                                )
                                test_version = st.text_input(
                                    "Test version",
                                    value=assessment.get("test_version") or "",
                                    key=f"edit_{selected_id}_test_version",
                                )
                                test_form = st.text_input(
                                    "Test form",
                                    value=assessment.get("test_form") or "",
                                    key=f"edit_{selected_id}_test_form",
                                )
                                date_raw_col, date_iso_col = st.columns(2)
                                with date_raw_col:
                                    test_date_raw = st.text_input(
                                        "Test date (raw)",
                                        value=assessment.get("test_date_raw") or "",
                                        key=f"edit_{selected_id}_test_date_raw",
                                    )
                                with date_iso_col:
                                    stored_iso = assessment.get("test_date_iso")
                                    try:
                                        default_date = (
                                            date.fromisoformat(stored_iso)
                                            if stored_iso
                                            else None
                                        )
                                    except ValueError:
                                        default_date = None
                                    test_date_iso_value = st.date_input(
                                        "Test date (ISO)",
                                        value=default_date,
                                        format="DD.MM.YYYY",
                                        key=f"edit_{selected_id}_test_date_iso",
                                    )
                                    test_date_iso = (
                                        test_date_iso_value.isoformat()
                                        if test_date_iso_value
                                        else ""
                                    )
                                assessment_note = st.text_area(
                                    "Assessment note",
                                    value=assessment.get("assessment_note") or "",
                                    key=f"edit_{selected_id}_assessment_note",
                                )
                                _render_inline_findings(
                                    review_validation_findings, "field:assessment_note"
                                )

                                st.write("Overall metrics")
                                subtest_sum_summary, metric_sum_summary = (
                                    _sum_summaries(
                                        subtest_rows, metrics_rows, definition
                                    )
                                )
                                metric_sum_targets = _metric_sum_targets(definition)
                                metric_computed_summary = [
                                    (r["metric_key"], str(r.get("value_raw")))
                                    for r in metrics_rows
                                    if r.get("value_provenance") == "computed"
                                    and r["metric_key"] not in metric_sum_targets
                                ]
                                _render_inline_findings_by_prefix(
                                    review_validation_findings,
                                    "metric:",
                                    computed_summary=metric_computed_summary,
                                    group_label=norm_group_label,
                                    sum_summary=metric_sum_summary,
                                )
                                metrics_df = pd.DataFrame(
                                    metrics_rows,
                                    columns=["position", "metric_key", "value_raw"],
                                )
                                metrics_df["quelle"] = [
                                    _QUELLE_LABELS.get(
                                        r.get("value_provenance"),
                                        r.get("value_provenance"),
                                    )
                                    for r in metrics_rows
                                ]
                                edited_metrics_df = st.data_editor(
                                    metrics_df,
                                    disabled=["position", "metric_key", "quelle"],
                                    hide_index=True,
                                    width="stretch",
                                    key=f"edit_{selected_id}_metrics",
                                )

                                st.write("Subtests")
                                subtest_computed_summary = [
                                    (r["subtest_key"], f"PR {r.get('pr')}")
                                    for r in subtest_rows
                                    if r.get("pr_provenance") == "computed"
                                ]
                                _render_inline_findings_by_prefix(
                                    review_validation_findings,
                                    "subtest:",
                                    computed_summary=subtest_computed_summary,
                                    group_label=norm_group_label,
                                    sum_summary=subtest_sum_summary,
                                )
                                subtests_df = pd.DataFrame(
                                    subtest_rows,
                                    columns=[
                                        "position",
                                        "subtest_key",
                                        "value_raw",
                                        "rw",
                                        "rw_max",
                                        "pr",
                                    ],
                                )
                                subtests_df["quelle"] = [
                                    _QUELLE_LABELS.get(
                                        r.get("pr_provenance"), r.get("pr_provenance")
                                    )
                                    for r in subtest_rows
                                ]
                                subtests_df["quelle_rw"] = [
                                    _QUELLE_RW_LABELS.get(
                                        r.get("rw_provenance"), r.get("rw_provenance")
                                    )
                                    for r in subtest_rows
                                ]
                                edited_subtests_df = st.data_editor(
                                    subtests_df,
                                    disabled=[
                                        "position",
                                        "subtest_key",
                                        "quelle",
                                        "quelle_rw",
                                    ],
                                    column_config={"quelle_rw": "Quelle RW"},
                                    hide_index=True,
                                    width="stretch",
                                    key=f"edit_{selected_id}_subtests",
                                )

                                save_clicked = st.form_submit_button("Save changes")

                        if save_clicked:
                            saved_any = False
                            new_scalar_values = {
                                "klasse": klasse.upper() if klasse else None,
                                "gender": gender or None,
                                "test_version": test_version or None,
                                "test_form": test_form or None,
                                "test_date_raw": test_date_raw or None,
                                "test_date_iso": test_date_iso or None,
                                "norm_variant": norm_variant_choice or None,
                                "assessment_note": assessment_note or None,
                            }
                            field_changes = {
                                field_name: new_value
                                for field_name, new_value in new_scalar_values.items()
                                if _norm(assessment.get(field_name)) != _norm(new_value)
                            }
                            if field_changes:
                                store.update_assessment_fields(
                                    selected_id, field_changes
                                )
                                saved_any = True

                            if (
                                name_editable
                                and name.strip()
                                and name.strip()
                                != (assessment.get("person_name") or "")
                            ):
                                store.rename_assessment_person(selected_id, name)
                                saved_any = True

                            for orig, edited in zip(
                                metrics_rows,
                                edited_metrics_df.to_dict("records"),
                                strict=False,
                            ):
                                if _norm(orig["value_raw"]) != _norm(
                                    edited["value_raw"]
                                ):
                                    store.update_assessment_metric(
                                        selected_id,
                                        orig["position"],
                                        edited["value_raw"],
                                    )
                                    saved_any = True

                            for orig, edited in zip(
                                subtest_rows,
                                edited_subtests_df.to_dict("records"),
                                strict=False,
                            ):
                                if any(
                                    _norm(orig[f]) != _norm(edited[f])
                                    for f in ("value_raw", "rw", "rw_max", "pr")
                                ):
                                    store.update_subtest_score(
                                        selected_id,
                                        orig["position"],
                                        value_raw=edited["value_raw"],
                                        rw=edited["rw"],
                                        rw_max=edited["rw_max"],
                                        pr=edited["pr"],
                                    )
                                    saved_any = True

                            # A no-op Save must not run completion (I6 —
                            # completion only follows a real edit that
                            # stamped `edited_at`).
                            if saved_any:
                                try:
                                    norm_completion.run_completion(selected_id)
                                except Exception:
                                    logger.exception(
                                        "norm_completion.run_completion failed "
                                        "for assessment_id=%s",
                                        selected_id,
                                    )
                                    # st.rerun() below throws away this run —
                                    # stash the failure so the status section
                                    # (which survives the rerun) can show it.
                                    st.session_state[
                                        f"norm_completion_failed_{selected_id}"
                                    ] = True
                            st.rerun()

    elif page == "Class Profile":

        def _class_data_csv_bytes(group: class_groups.ClassGroup) -> bytes:
            """One row per child: person_id, display name when available,
            the definition's metric columns, one column per subtest PR
            (ADR 0008 spec §0/OQ-D — the per-class replacement for the
            removed cross-class Results CSV export).
            """
            definition = store.get_test_definition(group.test_version)
            metric_cols = (
                [m.key for m in definition.overall_metrics] if definition else []
            )
            subtest_cols = [s.key for s in definition.subtests] if definition else []

            records = []
            for row in store.get_class_assessments(
                group.klasse, group.schuljahr, group.test_version
            ):
                assessment_id = row["assessment_id"]
                metrics = {
                    m["metric_key"]: (
                        m["value_num"]
                        if m["value_num"] is not None
                        else (m["value_text"] or m["value_raw"])
                    )
                    for m in store.get_assessment_metrics(assessment_id)
                }
                subtest_prs = {
                    s["subtest_key"]: s["pr"]
                    for s in store.get_subtest_scores(assessment_id)
                }
                full = store.get_assessment(assessment_id)
                record = {
                    "person_id": row.get("person_id") or f"unlinked #{assessment_id}",
                    "name": (
                        full.get("person_name")
                        if full and config.STORE_RAW_NAMES
                        else None
                    ),
                }
                for mc in metric_cols:
                    record[mc] = metrics.get(mc)
                for sc in subtest_cols:
                    record[f"pr_{sc}"] = subtest_prs.get(sc)
                records.append(record)

            return pd.DataFrame(records).to_csv(index=False).encode("utf-8")

        profile_layout = st.container(key="class_profile_layout")
        left, right = profile_layout.columns([1, 2])
        groups = class_groups.list_groups()

        with left:
            filter_col1, filter_col2, filter_col3 = st.columns(3)
            all_schuljahre = sorted({g.schuljahr for g in groups}, reverse=True)
            with filter_col1:
                schuljahr_filter = st.multiselect(
                    "Schuljahr",
                    options=all_schuljahre,
                    default=all_schuljahre[:1],
                    key="cp_schuljahr",
                    bind="query-params",
                    persist_state="session",
                )
            all_versions = sorted({g.test_version for g in groups})
            with filter_col2:
                version_filter = st.multiselect(
                    "Test version",
                    options=all_versions,
                    default=all_versions,
                    key="cp_version",
                    bind="query-params",
                    persist_state="session",
                )
            with filter_col3:
                state_filter = st.multiselect(
                    "State",
                    options=["new", "generated", "stale"],
                    default=["new", "generated", "stale"],
                    key="cp_state",
                    bind="query-params",
                    persist_state="session",
                )

            filtered_groups = [
                g
                for g in groups
                if (not schuljahr_filter or g.schuljahr in schuljahr_filter)
                and (not version_filter or g.test_version in version_filter)
                and (not state_filter or g.state in state_filter)
            ]
            n_stale = sum(1 for g in filtered_groups if g.state == "stale")
            n_new = sum(1 for g in filtered_groups if g.state == "new")
            st.caption(
                f"{len(filtered_groups)} classes · {n_stale} stale · "
                f"{n_new} not yet generated"
            )

            if not filtered_groups:
                st.info("No classes match the selected filters.")
            else:
                # Recomputed from filtered_groups only, so a row hidden by a
                # filter change doesn't leave a stale selection behind.
                selected_class_keys = [
                    "␟".join(g.key)
                    for g in filtered_groups
                    if st.session_state.get(f"cp_select_{'␟'.join(g.key)}", False)
                ]
                with st.container(key="class_list", height="stretch"):
                    for g in filtered_groups:
                        row_key = "␟".join(g.key)  # unit separator: safe delimiter
                        label = (
                            f"**{g.klasse}** · {g.schuljahr} · {g.test_version} · "
                            f"{g.child_count} Kinder"
                        )
                        badges = []
                        if g.awaiting_review_count > 0:
                            badges.append("⏳")
                        if g.state == "new":
                            badges.append("🆕")
                        elif g.state == "generated":
                            generated_at = (g.latest_profile or {}).get(
                                "generated_at"
                            ) or ""
                            badges.append(
                                f"{_relative_date(generated_at)} ✅"
                                if generated_at
                                else "✅"
                            )
                        elif g.state == "stale":
                            badges.append("♻️")

                        with st.container(
                            horizontal=True,
                            horizontal_alignment="distribute",
                            vertical_alignment="center",
                        ):
                            st.checkbox(
                                "Select",
                                key=f"cp_select_{row_key}",
                                label_visibility="collapsed",
                                width="content",
                            )
                            st.button(
                                label,
                                key=f"class_row_{row_key}",
                                width="stretch",
                                type=(
                                    "primary"
                                    if st.session_state["class_profile_selected"]
                                    == row_key
                                    else "secondary"
                                ),
                                on_click=(
                                    lambda k=row_key: st.session_state.update(
                                        class_profile_selected=k
                                    )
                                ),
                            )
                            if badges:
                                st.markdown(" ".join(badges), width="content")

            ungrouped_count = store.count_ungrouped_validated_assessments()
            if ungrouped_count:
                st.caption(
                    f"{ungrouped_count} validated assessments have no Klasse / "
                    "Test version / Testdatum and belong to no class."
                )

        with right:
            with st.container(border=True, height="stretch", key="class_profile_panel"):
                selected_key = st.session_state["class_profile_selected"]
                selected_group = next(
                    (g for g in groups if "␟".join(g.key) == selected_key), None
                )
                if selected_group is None:
                    st.info(
                        "Select a class from the list to view or generate its profile."
                    )
                else:
                    g = selected_group
                    generate_clicked = False
                    with st.container(
                        horizontal=True,
                        horizontal_alignment="distribute",
                        vertical_alignment="center",
                    ):
                        st.subheader(
                            f"{g.klasse} · {g.schuljahr} · {g.test_version}",
                            width="content",
                        )
                        with st.container(
                            horizontal=True, horizontal_alignment="right", gap="small"
                        ):
                            generate_clicked = st.button(
                                "Regenerate profile"
                                if g.state != "new"
                                else "Generate profile",
                                key=f"generate_{selected_key}",
                                type="primary",
                            )
                            md_path = None
                            if g.latest_profile:
                                candidate = Path(g.latest_profile["markdown_path"])
                                if candidate.exists():
                                    md_path = candidate
                            if md_path is not None:
                                preview_url = "?" + urlencode(
                                    {
                                        "view": "print",
                                        "klasse": g.klasse,
                                        "schuljahr": g.schuljahr,
                                        "test_version": g.test_version,
                                    }
                                )
                                st.link_button(
                                    "Preview",
                                    preview_url,
                                    help="Opens the report in a new tab, "
                                    "without the app chrome.",
                                )
                            with st.popover("Download"):
                                if md_path is not None:
                                    st.download_button(
                                        "Markdown",
                                        data=md_path.read_bytes(),
                                        file_name=md_path.name,
                                        mime="text/markdown",
                                        key=f"dl_md_{selected_key}",
                                        width="stretch",
                                    )
                                st.download_button(
                                    "Class data (CSV)",
                                    data=_class_data_csv_bytes(g),
                                    file_name=(
                                        f"{g.klasse}_{g.schuljahr}_class_data.csv"
                                    ),
                                    mime="text/csv",
                                    key=f"dl_csv_{selected_key}",
                                    width="stretch",
                                )

                    tab_profile, tab_children = st.tabs(
                        ["Class profile", "Child reports"]
                    )
                    with tab_profile:
                        if g.latest_profile:
                            st.caption(
                                f"Vergleichsnorm: {g.latest_profile.get('norm_group') or '—'}"
                            )
                        if g.state == "stale":
                            st.warning(
                                "This profile is stale: the class's validated "
                                "assessments have changed since it was generated."
                            )
                        if g.awaiting_review_count > 0:
                            st.info(
                                f"{g.awaiting_review_count} of "
                                f"{g.child_count + g.awaiting_review_count} sheets in "
                                "this class are still awaiting review."
                            )

                        if generate_clicked:
                            runner = get_default_runner()
                            with st.status(
                                "Generating profile...", expanded=True
                            ) as status:
                                progress_bar = st.progress(0.0, text="starting...")

                                def progress_cb(
                                    i: int,
                                    total: int,
                                    label: str,
                                    _bar: DeltaGenerator = progress_bar,
                                ) -> None:
                                    _bar.progress(
                                        i / total if total else 0.0,
                                        text=f"{i}/{total} — {label}",
                                    )

                                try:
                                    generate_class_profile(
                                        g, runner, progress_cb=progress_cb
                                    )
                                    status.update(
                                        label="Profile generated", state="complete"
                                    )
                                    st.rerun()
                                except RunnerUnavailableError as exc:
                                    status.update(
                                        label="Extraction runner unavailable",
                                        state="error",
                                    )
                                    st.error(
                                        "The default extraction runner (Claude Agent SDK, "
                                        "via the `claude` CLI) is not available in this "
                                        f"environment.\n\n{exc}\n\n"
                                        "Fix: install the `claude` CLI and Node.js, or set "
                                        "the environment variable DEMAT_RUNNER=direct to "
                                        "use the DirectMessagesRunner fallback (requires "
                                        "ANTHROPIC_API_KEY) and restart the app."
                                    )
                                except KnowledgeBaseUnavailableError as exc:
                                    status.update(
                                        label="Knowledge base unavailable",
                                        state="error",
                                    )
                                    st.error(str(exc))
                                except ValueError as exc:
                                    status.update(
                                        label="Profile generation failed", state="error"
                                    )
                                    st.error(str(exc))

                        if g.latest_profile:
                            md_path = Path(g.latest_profile["markdown_path"])
                            if md_path.exists():
                                st.markdown(
                                    md_path.read_text(encoding="utf-8"),
                                    unsafe_allow_html=True,
                                )
                            else:
                                st.warning(
                                    "The generated report file is missing on disk."
                                )

                    with tab_children:
                        child_reports_by_assessment = {
                            aid: store.latest_child_report(aid)
                            for aid in g.assessment_ids
                        }
                        n_generated = sum(
                            1
                            for r in child_reports_by_assessment.values()
                            if r is not None
                        )
                        with st.container(
                            horizontal=True,
                            horizontal_alignment="distribute",
                            vertical_alignment="center",
                        ):
                            st.caption(
                                f"{n_generated} of {len(g.assessment_ids)} "
                                "child reports generated"
                            )
                            with st.container(
                                horizontal=True,
                                horizontal_alignment="right",
                                gap="small",
                            ):
                                generate_children_clicked = st.button(
                                    "Generate child reports"
                                    if n_generated == 0
                                    else "Regenerate child reports",
                                    key=f"generate_children_{selected_key}",
                                    type="primary",
                                )
                                if n_generated > 0:
                                    batch_preview_url = "?" + urlencode(
                                        {
                                            "view": "child_batch",
                                            "klasse": g.klasse,
                                            "schuljahr": g.schuljahr,
                                            "test_version": g.test_version,
                                        }
                                    )
                                    st.link_button(
                                        "Print all",
                                        batch_preview_url,
                                        help="Opens one child per page in a new "
                                        "tab, without the app chrome.",
                                    )

                        if generate_children_clicked:
                            try:
                                with st.spinner("Generating child reports..."):
                                    child_results = generate_class_child_reports(g)
                                n_ok = sum(
                                    1
                                    for r in child_results
                                    if not isinstance(r, Exception)
                                )
                                n_err = len(child_results) - n_ok
                                if n_err:
                                    error_summary = "; ".join(
                                        sorted(
                                            {
                                                str(r)
                                                for r in child_results
                                                if isinstance(r, Exception)
                                            }
                                        )
                                    )
                                    st.warning(
                                        f"{n_ok} of {len(child_results)} child "
                                        f"reports generated, {n_err} failed: "
                                        f"{error_summary}"
                                    )
                                else:
                                    st.success(f"{n_ok} child reports generated.")
                                child_reports_by_assessment = {
                                    aid: store.latest_child_report(aid)
                                    for aid in g.assessment_ids
                                }
                            except KnowledgeBaseUnavailableError as exc:
                                st.error(str(exc))

                        for child_assessment_id in g.assessment_ids:
                            child_assessment = store.get_assessment(child_assessment_id)
                            if child_assessment is None:
                                continue
                            child_label = (
                                (
                                    child_assessment.get("person_name")
                                    if config.STORE_RAW_NAMES
                                    else None
                                )
                                or child_assessment.get("person_id")
                                or f"unlinked #{child_assessment_id}"
                            )
                            child_report_row = child_reports_by_assessment.get(
                                child_assessment_id
                            )
                            child_row_md_path = None
                            if child_report_row is not None:
                                candidate = Path(child_report_row["markdown_path"])
                                if candidate.exists():
                                    child_row_md_path = candidate
                            with st.container(
                                horizontal=True,
                                horizontal_alignment="distribute",
                                vertical_alignment="center",
                            ):
                                st.write(child_label)
                                with st.container(
                                    horizontal=True,
                                    horizontal_alignment="right",
                                    gap="small",
                                ):
                                    if child_row_md_path is not None:
                                        child_preview_url = "?" + urlencode(
                                            {
                                                "view": "child",
                                                "assessment_id": str(
                                                    child_assessment_id
                                                ),
                                            }
                                        )
                                        st.link_button(
                                            "Preview",
                                            child_preview_url,
                                            key=f"child_preview_{child_assessment_id}",
                                        )
                                        st.download_button(
                                            "Markdown",
                                            data=child_row_md_path.read_bytes(),
                                            file_name=(
                                                f"{child_row_md_path.parent.name}"
                                                f"_{child_row_md_path.name}"
                                            ),
                                            mime="text/markdown",
                                            key=(f"child_dl_{child_assessment_id}"),
                                        )
                                    else:
                                        st.caption("Not generated yet")

except Exception:
    logger.exception("Unhandled error while rendering the %r page", page)
    error_banner.error(
        "Something went wrong loading this page. Details were written to the app logs."
    )

# Mirror the current nav page and selected resource into the URL so a
# reload or a shared link lands back on the same spot — read on the *next*
# run's session-state init above, not this one. Filter widgets persist
# themselves to the URL via `bind="query-params"` (ADR 0011); this mirror
# only ever touches the fixed `OWNED_URL_KEYS` set, never the whole
# `st.query_params`, since bound keys raise on `clear()`/`update()`.
_url_params: dict[str, str] = {"page": NAV_PAGE_SLUGS[st.session_state["page"]]}
if st.session_state["page"] == "Assessment Review":
    selected_assessment = st.session_state.get("inbox_selected_assessment")
    if selected_assessment is not None:
        _url_params["assessment"] = str(selected_assessment)
elif st.session_state["page"] == "Class Profile":
    selected_class = st.session_state.get("class_profile_selected")
    if selected_class is not None:
        klasse, schuljahr, test_version = selected_class.split("␟")
        _url_params["klasse"] = klasse
        _url_params["schuljahr"] = schuljahr
        _url_params["test_version"] = test_version
url_state.sync_owned_params(st.query_params, _url_params)
