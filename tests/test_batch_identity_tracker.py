"""`pipeline.BatchIdentityTracker`: the batch-scoped guardrail against
merging two different, same-named children into one `person_id` within a
single upload batch. A real incident (two same-named, different-gender
children in one class both hashing to the same deterministic id) is what
this guards against — see the class-profile duplicate-child bug it was
added for.
"""

from __future__ import annotations

import threading

from demat.pipeline import BatchIdentityTracker


def test_first_occurrence_keeps_the_deterministic_id_and_key():
    tracker = BatchIdentityTracker()
    person_id, key = tracker.resolve("DMZC", "sina mueller")
    assert (person_id, key) == ("DMZC", "sina mueller")


def test_repeat_normalization_key_gets_a_disambiguated_id_and_key(caplog):
    tracker = BatchIdentityTracker()
    first_id, first_key = tracker.resolve("DMZC", "sina mueller")
    with caplog.at_level("WARNING"):
        second_id, second_key = tracker.resolve("DMZC", "sina mueller")

    assert (first_id, first_key) == ("DMZC", "sina mueller")
    assert second_id != first_id
    assert len(second_id) == 4
    assert second_id[:3] == "DMZ"
    assert second_key == f"sina mueller#{second_id[-1]}"
    assert "batch identity guardrail" in caplog.text


def test_different_names_never_collide_in_the_tracker():
    tracker = BatchIdentityTracker()
    a = tracker.resolve("DMZC", "sina mueller")
    b = tracker.resolve("ABCD", "leon schmidt")
    assert a == ("DMZC", "sina mueller")
    assert b == ("ABCD", "leon schmidt")


def test_three_occurrences_of_the_same_name_each_get_distinct_ids():
    tracker = BatchIdentityTracker()
    first, _ = tracker.resolve("DMZC", "sina mueller")
    second, _ = tracker.resolve("DMZC", "sina mueller")
    third, _ = tracker.resolve("DMZC", "sina mueller")
    assert len({first, second, third}) == 3


def test_thread_safe_under_concurrent_resolve_calls():
    # Simulates process_pdfs's concurrent workers all extracting a page
    # named the same thing in the same batch: exactly one call should keep
    # the deterministic id, and every id handed out must be unique.
    tracker = BatchIdentityTracker()
    results: list[str] = []
    lock = threading.Lock()

    def worker():
        person_id, _key = tracker.resolve("DMZC", "sina mueller")
        with lock:
            results.append(person_id)

    threads = [threading.Thread(target=worker) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(results) == 20
    assert len(set(results)) == 20
    assert results.count("DMZC") == 1
