"""Tests de l'ajout en lot au registre nettoyé (add_entries_from_parents)."""

from metadata_scrubber.cleaned_registry import add_entries_from_parents, empty_cleaned_doc


def _registry():
    """Registre des doublons : deux listes sans doublon, une avec doublon."""
    def parent(cl_id, duplicates=()):
        return {
            "id": cl_id,
            "name": cl_id.upper(),
            "label": "",
            "codes": [["1", "Oui"], ["2", "Non"]],
            "codes_count": 2,
            "vars": [],
            "duplicates": list(duplicates),
        }

    return {
        "a": parent("a"),
        "b": parent("b"),
        "c": parent("c", [{"id": "c-dup", "decision": "pending"}]),
    }


def test_explicit_ids_are_classified():
    cleaned = empty_cleaned_doc()
    res = add_entries_from_parents(cleaned, _registry(), ["a", "c", "zzz", "a"])

    assert res["added"] == ["a"]
    assert res["skipped_with_duplicates"] == ["c"]
    assert res["not_found"] == ["zzz"]
    assert res["already_present"] == []
    assert list(cleaned["codelists"]) == ["a"]
    assert cleaned["codelists"]["a"]["replaces"] == []


def test_all_without_duplicates():
    cleaned = empty_cleaned_doc()
    res = add_entries_from_parents(cleaned, _registry())

    assert sorted(res["added"]) == ["a", "b"]
    assert res["skipped_with_duplicates"] == []
    assert sorted(cleaned["codelists"]) == ["a", "b"]


def test_second_run_reports_already_present():
    cleaned = empty_cleaned_doc()
    add_entries_from_parents(cleaned, _registry())
    res = add_entries_from_parents(cleaned, _registry())

    assert res["added"] == []
    assert sorted(res["already_present"]) == ["a", "b"]
    assert len(cleaned["codelists"]) == 2


def test_remove_entries_only_removes_manual_additions():
    from metadata_scrubber.cleaned_registry import remove_entries

    cleaned = empty_cleaned_doc()
    add_entries_from_parents(cleaned, _registry())
    # Une entrée issue d'une décision approuvée (replaces non vide) est protégée
    cleaned["codelists"]["b"]["replaces"] = [{"id": "dup-1"}]

    res = remove_entries(cleaned, ["a", "b", "zzz", "a"])

    assert res["removed"] == ["a"]
    assert res["protected"] == ["b"]
    assert res["not_present"] == ["zzz"]
    assert list(cleaned["codelists"]) == ["b"]
