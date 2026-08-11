"""The same generated file must not come back twice.

A model that produces a file rarely stops there: it re-reads it to check the
row count, or rewrites it after spotting a gap. The sandbox container still
holds the file, so every run that touches it reports it again, each copy is
downloaded, and each is attached to its own code result. Observed live: one
12,922-byte .xlsx returned twice from a three-run answer — two identical
download links for the reader, and twice the bytes stored and re-sent.

Both provider paths reach that shape by different routes (Anthropic attaches
per tool-result block; OpenAI collects every container_file_citation into one
list), which is why the rule lives in one shared place rather than in each.
"""

from __future__ import annotations

from typing import Any

from app.schemas import dedupe_code_files


def _file(name: str, data: str = "AAA") -> dict[str, Any]:
    return {
        "filename": name,
        "mime_type": "text/csv",
        "data": f"data:text/csv;base64,{data}",
    }


def _names(results: list[dict[str, Any]]) -> list[list[str]]:
    return [[f["filename"] for f in (r.get("files") or [])] for r in results]


def test_the_same_file_reported_by_two_runs_is_kept_once() -> None:
    results = [
        {"code": "write it", "files": [_file("out.xlsx")]},
        {"code": "read it back", "files": [_file("out.xlsx")]},
    ]
    dedupe_code_files(results)
    assert _names(results) == [[], ["out.xlsx"]]


def test_a_rewritten_file_keeps_the_later_version() -> None:
    """Same name, different bytes: one file at two moments, not two
    deliverables. The corrected version is the one that survives."""
    results = [
        {"code": "v1", "files": [_file("out.csv", data="T0xE")]},
        {"code": "v2 with the gap filled", "files": [_file("out.csv", data="TkVX")]},
    ]
    dedupe_code_files(results)
    assert _names(results) == [[], ["out.csv"]]
    assert str(results[1]["files"][0]["data"]).endswith("TkVX")  # the later bytes


def test_distinct_files_are_all_kept() -> None:
    results = [
        {"code": "a", "files": [_file("cons.xlsx"), _file("roadmap.xlsx")]},
        {"code": "b", "files": [_file("metrics.csv")]},
    ]
    dedupe_code_files(results)
    assert _names(results) == [["cons.xlsx", "roadmap.xlsx"], ["metrics.csv"]]


def test_duplicates_within_a_single_result_collapse() -> None:
    """The OpenAI path collects every citation across the response into ONE
    list, so its duplicates land side by side rather than across results."""
    results = [{"code": "a", "files": [_file("out.xlsx"), _file("out.xlsx")]}]
    dedupe_code_files(results)
    assert _names(results) == [["out.xlsx"]]


def test_images_are_untouched() -> None:
    """A repeated chart is a visible duplicate a reader scrolls past, not a
    fork in which file is the real one — and images render inline rather than
    as downloads."""
    results = [
        {"code": "a", "images": ["data:image/png;base64,X"], "files": []},
        {"code": "b", "images": ["data:image/png;base64,X"], "files": []},
    ]
    dedupe_code_files(results)
    assert [r["images"] for r in results] == [
        ["data:image/png;base64,X"],
        ["data:image/png;base64,X"],
    ]


def test_results_without_files_are_left_alone() -> None:
    """Most code runs produce logs and nothing else; the "files" key is often
    absent entirely rather than an empty list."""
    results: list[dict[str, Any]] = [
        {"code": "print(1)", "logs": "1"},
        {"code": "print(2)", "logs": "2", "files": None},
    ]
    dedupe_code_files(results)
    assert results[0] == {"code": "print(1)", "logs": "1"}
    assert results[1]["files"] is None


def test_an_empty_result_list_is_a_no_op() -> None:
    results: list[dict[str, Any]] = []
    dedupe_code_files(results)
    assert results == []


def test_a_malformed_file_entry_is_never_dropped() -> None:
    """These dicts come from provider responses whose shape this app does not
    control. Something unrecognizable is passed through rather than silently
    discarded — a dropped deliverable is much the worse failure."""
    results = [{"code": "a", "files": ["not-a-dict", _file("out.csv")]}]
    dedupe_code_files(results)
    assert results[0]["files"] == ["not-a-dict", _file("out.csv")]


def test_the_note_count_still_describes_the_runs_not_the_files() -> None:
    """Deduping files must not make the answer claim fewer code runs than
    happened: "Ran 3 snippets" counts RESULTS, and every result survives here
    — only their repeated file attachments are collapsed."""
    results = [
        {"code": "write", "files": [_file("out.csv")]},
        {"code": "verify", "files": [_file("out.csv")]},
        {"code": "count rows", "logs": "12"},
    ]
    dedupe_code_files(results)
    assert len(results) == 3
