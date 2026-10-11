"""German Schuljahr derivation. Moved verbatim from `app.py` (spec §4.1) so
the report module (`demat/reports/*`) never has to import the Streamlit
script.
"""

from __future__ import annotations

from datetime import date


def school_year(test_date_iso: str | None) -> str | None:
    """German Schuljahr label ("2026-27"), Aug-cutover: a date in
    Aug-Dec belongs to the year starting then; Jan-Jul belongs to the
    previous Aug's cohort. Based on the sheet's `test_date_iso` (a
    test-time fact), not today's date — pairing "3C" with a review-time
    year would silently drift as sheets get reviewed later. Falls back
    to today when the sheet has no test date.
    """
    if test_date_iso:
        try:
            year, month = int(test_date_iso[:4]), int(test_date_iso[5:7])
        except (ValueError, IndexError):
            return None
    else:
        today = date.today()
        year, month = today.year, today.month
    start_year = year if month >= 8 else year - 1
    return f"{start_year}-{str(start_year + 1)[2:]}"


def klasse_schuljahre(klasse: str | None, test_date_iso: str | None) -> str | None:
    """Composite display field: uppercased Klasse + Schuljahr, e.g. "3C 2026-27"."""
    if not klasse:
        return None
    year_label = school_year(test_date_iso)
    if not year_label:
        return None
    return f"{klasse.upper()} {year_label}"
