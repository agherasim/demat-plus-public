"""Shared builders for child-report tests (not a pytest plugin file itself
— imported explicitly, kept out of `conftest.py` so it only loads where
needed). Builds `ChildReportData` fixtures directly, with explicit
`bands`/`median_zone`, so chart/render/budget tests never depend on the
knowledge base (only `test_band_segments.py` calls `lookup.py`).
"""

from __future__ import annotations

from demat.reports.child_data import ChildReportData, GesamtField, SheetSubtest
from demat.reports.data import ChildData
from demat.reports.norms import BandSegment

DEMAT2PLUS_BANDS = (
    BandSegment(lo=0, hi=7, category="sehr schwach", label="0–7", open_low=True),
    BandSegment(lo=8, hi=14, category="schwach", label="8–14"),
    BandSegment(lo=15, hi=29, category="unterdurchschnittlich", label="15–29"),
    BandSegment(lo=30, hi=70, category="durchschnittlich", label="30–70"),
    BandSegment(lo=71, hi=92, category="überdurchschnittlich", label="71–92"),
    BandSegment(
        lo=93, hi=100, category="ausgezeichnet", label="93–100", open_high=True
    ),
)

DEMAT1PLUS_BANDS = (
    BandSegment(
        lo=0, hi=31, category="weit unterdurchschnittlich", label="≤31", open_low=True
    ),
    BandSegment(lo=32, hi=41, category="unterdurchschnittlich", label="32–41"),
    BandSegment(lo=42, hi=58, category="durchschnittlich", label="42–58"),
    BandSegment(lo=59, hi=68, category="überdurchschnittlich", label="59–68"),
    BandSegment(
        lo=69, hi=100, category="weit überdurchschnittlich", label="≥69", open_high=True
    ),
)

DEMAT3PLUS_BANDS = (
    BandSegment(lo=0, hi=3, category="sehr schwach", label="0–3", open_low=True),
    BandSegment(lo=4, hi=13, category="schwach", label="4–13"),
    BandSegment(lo=14, hi=27, category="unterdurchschnittlich", label="14–27"),
    BandSegment(lo=28, hi=72, category="durchschnittlich", label="28–72"),
    BandSegment(lo=73, hi=88, category="überdurchschnittlich", label="73–88"),
    BandSegment(
        lo=89,
        hi=100,
        category="weit überdurchschnittlich",
        label="89–100",
        open_high=True,
    ),
)

DEMAT5PLUS_BANDS = (
    BandSegment(
        lo=0, hi=32, category="weit unterdurchschnittlich", label="≤32", open_low=True
    ),
    BandSegment(lo=32, hi=42, category="unterdurchschnittlich", label="32–42"),
    BandSegment(lo=42, hi=58, category="durchschnittlich", label="42–58"),
    BandSegment(lo=58, hi=68, category="überdurchschnittlich", label="58–68"),
    BandSegment(
        lo=68, hi=100, category="weit überdurchschnittlich", label="≥68", open_high=True
    ),
)

_2PLUS_SUBTESTS = [
    ("ZE", "Zahleneigenschaften", 2, 2, 81),
    ("LA", "Längenvergleich", 4, 4, 79),
    ("AD", "Addition", 4, 4, 85),
    ("SU", "Subtraktion", 3, 4, 64),
    ("DO", "Verdoppeln", 3, 3, 67),
    ("DI", "Division", 4, 4, 71),
    ("HA", "Halbieren", 2, 3, 43),
    ("GEL", "Rechnen mit Geld", 4, 4, 76),
    ("SA", "Sachaufgaben", 2, 4, 25),
    ("GEO", "Geometrie", 2, 4, 53),
]

_1PLUS_SUBTESTS = [
    ("MZ", "Mengen – Zahlen", 3, 3, 58),
    ("ZR", "Zahlenraum", 3, 5, 14),
    ("AD", "Addition", 1, 4, 3),
    ("SU", "Subtraktion", 2, 4, 24),
    ("ZZ", "Zahlenzerlegung – Zahlenergänzung", 1, 4, 4),
    ("TG", "Teil – Ganzes", 2, 4, 30),
    ("KA", "Kettenaufgaben", 0, 4, 2),
    ("UG", "Ungleichungen", 1, 4, 5),
    ("SA", "Sachaufgaben", 1, 4, 12),
]


def _subtests_rows_with_profile(rows: list[tuple]) -> list[SheetSubtest]:
    return [
        SheetSubtest(
            key=k,
            name=name,
            column_title=k,
            rw_display=f"{rw:g} ({mx:g})",
            pr=float(pr),
            pr_display=f"{pr:g}",
            pr_is_value=True,
            normed=True,
            category=None,
        )
        for k, name, rw, mx, pr in rows
    ]


def make_demat2plus_data(
    *, median_zone: tuple[float, float] | None = (30, 70)
) -> ChildReportData:
    subtests = _subtests_rows_with_profile(_2PLUS_SUBTESTS)
    child = ChildData(
        assessment_id=412,
        person_id="K7QM",
        person_ref="K7QM",
        display_label="Lena Beispiel",
        overall_value=78.0,
        overall_category="überdurchschnittlich",
        subtest_pr={k: pr for k, _, _, _, pr in _2PLUS_SUBTESTS},
        subtest_rw={k: rw for k, _, rw, _, _ in _2PLUS_SUBTESTS},
    )
    return ChildReportData(
        child=child,
        assessment_id=412,
        person_id="K7QM",
        person_ref="K7QM",
        display_name=None,
        klasse="3E",
        test_version="DEMAT 2+",
        test_key="demat2plus",
        norm_test="demat2plus",
        test_form="A",
        test_date_iso="2026-09-15",
        norm_variant="k3",
        norm_variant_label="3. Klasse, erste drei Schuljahresmonate",
        classification_basis="pr",
        classification_source="Manual DEMAT 2+, Kap. 7.2, S. 24",
        confidence_level="68 %",
        sheet_layout="rows_with_profile",
        subtests=subtests,
        rw_total_display="30 (36)",
        overall_value=78.0,
        overall_band_range=None,
        median_zone=median_zone,
        gesamt_fields=[
            GesamtField(
                label="Prozentrang (ges)", display="78", kind="value", present=True
            ),
            GesamtField(
                label="Prozentrang-Band (68 %)",
                display="64–83",
                kind="range",
                present=True,
            ),
            GesamtField(label="T-Wert (ges)", display="58", kind="value", present=True),
            GesamtField(
                label="T-Wert-Band (68 %)", display="52–60", kind="range", present=True
            ),
        ],
        rating_categories=[b.category for b in DEMAT2PLUS_BANDS],
        rating_rank=4,
        bands=DEMAT2PLUS_BANDS,
        profile_mode="categorised",
        profile_high="**Über dem Durchschnittsbereich:** Addition (PR 85)",
        profile_low="**Unter dem Durchschnittsbereich:** Sachaufgaben (PR 25, unterdurchschnittlich)",
        hint_lines=["a", "b", "c"],
        notes_lines=1,
        provenance_phrase=None,
        finalized_at="2026-09-23",
        generated_at="2026-09-27",
        template_version="1",
    )


def make_demat1plus_data() -> ChildReportData:
    subtests = _subtests_rows_with_profile(_1PLUS_SUBTESTS)
    child = ChildData(
        assessment_id=377,
        person_id="3HXT",
        person_ref="3HXT",
        display_label="Jonas Muster",
        overall_value=25.0,
        overall_category="weit unterdurchschnittlich",
        subtest_pr={k: pr for k, _, _, _, pr in _1PLUS_SUBTESTS},
        subtest_rw={k: rw for k, _, rw, _, _ in _1PLUS_SUBTESTS},
    )
    return ChildReportData(
        child=child,
        assessment_id=377,
        person_id="3HXT",
        person_ref="3HXT",
        display_name=None,
        klasse="2F",
        test_version="DEMAT 1+",
        test_key="demat1plus",
        norm_test="demat1plus",
        test_form="A",
        test_date_iso="2026-09-16",
        norm_variant="k2",
        norm_variant_label="2. Klasse, erste drei Schuljahresmonate",
        classification_basis="t",
        classification_source="Manual DEMAT 1+, Kap. 7.2, S. 25",
        confidence_level="68 %",
        sheet_layout="rows_with_profile",
        subtests=subtests,
        rw_total_display="14 (36)",
        overall_value=25.0,
        overall_band_range=None,
        median_zone=None,
        gesamt_fields=[
            GesamtField(
                label="Prozentrang (ges)", display="3", kind="value", present=True
            ),
            GesamtField(label="T-Wert (ges)", display="25", kind="value", present=True),
            GesamtField(
                label="T-Wert-Band (68 %)", display="18–23", kind="range", present=True
            ),
        ],
        rating_categories=[b.category for b in DEMAT1PLUS_BANDS],
        rating_rank=0,
        bands=DEMAT1PLUS_BANDS,
        profile_mode="relative",
        profile_high=(
            "**Relativ höchste Teilbereiche (innerhalb des eigenen Profils):** "
            "Mengen – Zahlen (PR 58), Teil – Ganzes (PR 30)"
        ),
        profile_low=(
            "**Relativ niedrigste Teilbereiche (innerhalb des eigenen Profils):** "
            "Kettenaufgaben (PR 2), Addition (PR 3)"
        ),
        hint_lines=["a", "b", "c", "d"],
        notes_lines=1,
        provenance_phrase=None,
        finalized_at="2026-09-23",
        generated_at="2026-09-27",
        template_version="1",
    )


def make_demat3plus_data(
    *, median_zone: tuple[float, float] | None = (28, 72), overall_band_range=(4, 18)
) -> ChildReportData:
    rows = [
        ("ARI", "Arithmetik", "Arithmetik", 7, 15, 30),
        ("SAR", "Sachrechnen und Größen", "Sachrechnen", 3, 8, 10),
        ("GEO", "Geometrie", "Geometrie", 2, 8, 6),
    ]
    subtests = [
        SheetSubtest(
            key=k,
            name=name,
            column_title=col,
            rw_display=f"{rw:g} ({mx:g})",
            pr=float(pr),
            pr_display=f"{pr:g}",
            pr_is_value=True,
            normed=True,
            category="schwach",
        )
        for k, name, col, rw, mx, pr in rows
    ]
    child = ChildData(
        assessment_id=455,
        person_id="W9RB",
        person_ref="W9RB",
        display_label="Mia Probe",
        overall_value=10.0,
        overall_category="schwach",
        subtest_pr={k: pr for k, _, _, _, _, pr in rows},
        subtest_rw={k: rw for k, _, _, rw, _, _ in rows},
    )
    gesamt_fields = (
        [
            GesamtField(
                label="Prozentrang-Band (68 %)",
                display="4–18",
                kind="range",
                present=True,
            )
        ]
        if overall_band_range is not None
        else []
    )
    return ChildReportData(
        child=child,
        assessment_id=455,
        person_id="W9RB",
        person_ref="W9RB",
        display_name=None,
        klasse="4C",
        test_version="DEMAT 3+",
        test_key="demat3plus",
        norm_test="demat3plus",
        test_form="B",
        test_date_iso="2026-09-14",
        norm_variant="anfang_k4",
        norm_variant_label="Anfang Klassenstufe 4",
        classification_basis="pr",
        classification_source="Manual DEMAT 3+, Kap. 7.2, Tabelle 4, S. 25",
        confidence_level="68 %",
        sheet_layout="columns_with_corridor",
        subtests=subtests,
        rw_total_display="12 (31)",
        overall_value=10.0,
        overall_band_range=overall_band_range,
        median_zone=median_zone,
        gesamt_fields=gesamt_fields,
        rating_categories=[b.category for b in DEMAT3PLUS_BANDS],
        rating_rank=1,
        bands=DEMAT3PLUS_BANDS,
        profile_mode="categorised",
        profile_high="**Über dem Durchschnittsbereich:** —",
        profile_low=(
            "**Unter dem Durchschnittsbereich:** Geometrie (PR 6, schwach), "
            "Sachrechnen und Größen (PR 10, schwach)"
        ),
        hint_lines=["a", "b", "c", "d", "e"],
        notes_lines=1,
        provenance_phrase=None,
        finalized_at="2026-09-25",
        generated_at="2026-09-27",
        template_version="1",
    )


def make_demat5plus_data() -> ChildReportData:
    rows = [
        ("A", "Arithmetik", 8, 14, 60),
        ("B", "Geometrie", 2, 4, None),
        ("C", "Sachrechnen", 9, 15, 55),
    ]
    subtests = []
    for k, name, rw, mx, pr in rows:
        normed = pr is not None
        subtests.append(
            SheetSubtest(
                key=k,
                name=name,
                column_title=k,
                rw_display=f"{rw:g} ({mx:g})",
                pr=float(pr) if pr is not None else None,
                pr_display=f"{pr:g}" if pr is not None else "n. n.",
                pr_is_value=pr is not None,
                normed=normed,
                category=None,
            )
        )
    child = ChildData(
        assessment_id=999,
        person_id="ABCD",
        person_ref="ABCD",
        display_label="Test Kind",
        overall_value=52.0,
        overall_category="durchschnittlich",
        subtest_pr={k: pr for k, _, _, _, pr in rows},
        subtest_rw={k: rw for k, _, rw, _, _ in rows},
    )
    return ChildReportData(
        child=child,
        assessment_id=999,
        person_id="ABCD",
        person_ref="ABCD",
        display_name=None,
        klasse="7A",
        test_version="DEMAT 5+",
        test_key="demat5plus",
        norm_test="demat5plus",
        test_form="A",
        test_date_iso="2026-09-10",
        norm_variant="gymnasium",
        norm_variant_label="Gymnasium",
        classification_basis="t",
        classification_source="Manual DEMAT 5+, Kap. 7.2, Tabelle 3, S. 21",
        confidence_level="90 %",
        sheet_layout="rows_with_profile",
        subtests=subtests,
        rw_total_display="19 (33)",
        overall_value=52.0,
        overall_band_range=None,
        median_zone=(15.8, 84.5),
        gesamt_fields=[
            GesamtField(
                label="Prozentrang (ges)", display="55", kind="value", present=True
            ),
            GesamtField(label="T-Wert (ges)", display="52", kind="value", present=True),
            GesamtField(
                label="T-Wert-Band (90 %)", display="47–57", kind="range", present=True
            ),
        ],
        rating_categories=[b.category for b in DEMAT5PLUS_BANDS],
        rating_rank=2,
        bands=DEMAT5PLUS_BANDS,
        profile_mode="relative",
        profile_high="**Relativ höchste Teilbereiche (innerhalb des eigenen Profils):** Arithmetik (PR 60)",
        profile_low="**Relativ niedrigste Teilbereiche (innerhalb des eigenen Profils):** Sachrechnen (PR 55)",
        hint_lines=["a", "b", "c"],
        notes_lines=2,
        provenance_phrase=None,
        finalized_at="2026-09-20",
        generated_at="2026-09-27",
        template_version="1",
    )
