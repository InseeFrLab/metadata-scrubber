"""engine.py — Comparaison des CodeLists d'un fichier DDI à l'index de référence.

Pour chaque liste fournie, dans l'ordre de l'entonnoir :
  1. réutilisée : même identifiant que dans l'index (le bon cas) ;
  2. doublon : exact, exact hors codes d'échappement, mêmes libellés métier
     (recherches directes dans les index inversés) ;
  3. inclusion : sous-ensemble d'une liste de l'index (ou extension : elle en
     contient une, avec des codes en plus) ;
  4. lignée : copiée d'une liste de l'index (BasedOnObject) ;
  5. proche : score flou du pipeline, calculé seulement avec les listes qui
     partagent au moins une valeur ou un libellé de code.
Les doublons internes au fichier sont détectés avec les fonctions de `funnel`.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from ..extractor import extract_codelists, extract_variables, parse_xml
from ..funnel import (
    MAX_CODES_FOR_FUZZY,
    _code_similarity,
    detect_business_duplicates,
    detect_exact_duplicates,
    detect_fuzzy_duplicates,
    detect_label_duplicates,
    fuzzy_score,
    prepare_signatures,
)
from ..normalize import normalize
from ..quality import PLACEHOLDER, split_codes
from ..types import CodeList
from .index import COMMUNE, LEVEL_RANK, OFFICIELLE, ReferenceIndex

REUTILISEE, DOUBLON, INCLUSION, PROCHE, NOUVELLE = (
    "reutilisee", "doublon", "inclusion", "proche", "nouvelle",
)
DETECTION_STATUS = {
    "exact": DOUBLON,
    "exact_business": DOUBLON,
    "labels": DOUBLON,
    "inclusion": INCLUSION,
    "extension": INCLUSION,
    "lineage": PROCHE,
    "fuzzy": PROCHE,
}
STATUS_RANK = {DOUBLON: 0, INCLUSION: 1, PROCHE: 2}

CONTAINMENT_THRESHOLD = 0.95
FUZZY_THRESHOLD = 0.80
MIN_SHARED_FOR_FUZZY = 0.5


@dataclass
class Match:
    """Correspondance entre une liste fournie et une liste de l'index."""
    target: CodeList
    detection: str
    confidence: float

    @property
    def status(self) -> str:
        return DETECTION_STATUS[self.detection]


@dataclass
class CheckResult:
    """Résultat du contrôle d'une liste fournie."""
    codelist: CodeList
    status: str
    matches: list[Match] = field(default_factory=list)
    reused: CodeList | None = None


def load_codelists(raw: bytes) -> list[CodeList]:
    """CodeLists d'un fichier DDI (dernière version de chacune), variables rattachées."""
    objects = parse_xml(raw)
    codelists = extract_codelists(objects)
    by_id = {cl.id: cl for cl in codelists}
    for var in extract_variables(objects):
        if var.cl_id in by_id:
            by_id[var.cl_id].vars.append(var.name)
    prepare_signatures(codelists)
    return codelists


def business_pairs(cl: CodeList) -> frozenset[tuple[str, str]]:
    business, _ = split_codes(cl.codes)
    return frozenset((normalize(v), normalize(lab)) for v, lab in business)


def find_matches(
    cl: CodeList,
    index: ReferenceIndex,
    *,
    containment: float = CONTAINMENT_THRESHOLD,
    fuzzy_threshold: float = FUZZY_THRESHOLD,
) -> list[Match]:
    """Correspondances d'une liste (signatures déjà calculées) dans l'index.

    Une liste de l'index n'apparaît qu'une fois, avec la détection la plus forte.
    Tri : doublon, inclusion, proche ; puis niveau (officielle d'abord) ; puis confiance.
    """
    found: dict[str, Match] = {}

    def add(target: CodeList, detection: str, confidence: float) -> None:
        if target.id != cl.id and target.id not in found:
            found[target.id] = Match(target, detection, round(confidence, 4))

    for target in index.by_sig.get(cl.sig, []):
        add(target, "exact", 1.0)

    # Liste « placeholder » (« Voir dictionnaire… ») : seul son nom est comparable
    if PLACEHOLDER not in cl.quality_flags:
        if len(cl.sig_business) >= 2:
            for target in index.by_business.get(cl.sig_business, []):
                add(target, "exact_business", 0.98)
        if len(cl.sig_labels) >= 2:
            for target in index.by_labels.get(cl.sig_labels, []):
                add(target, "labels", 0.95)

        pairs = business_pairs(cl)
        if len(pairs) >= 2:
            shared = Counter(t.id for p in pairs for t in index.by_pair.get(p, []))
            for target_id, n in shared.items():
                target_pairs = index.business_pairs[target_id]
                if len(pairs) < len(target_pairs) and n / len(pairs) >= containment:
                    add(index.by_id[target_id], "inclusion", 0.9 * n / len(pairs))
                elif 2 <= len(target_pairs) < len(pairs) and n / len(target_pairs) >= containment:
                    add(index.by_id[target_id], "extension", 0.9 * n / len(target_pairs))

        for parent_id in cl.based_on:
            parent = index.by_id.get(parent_id)
            if parent is not None:
                add(parent, "lineage", min(0.99, _code_similarity(parent, cl)))

        if len(cl.codes) <= MAX_CODES_FOR_FUZZY:
            # Codes de la liste qui partagent une valeur ou un libellé avec chaque cible.
            # Le score flou exige ~75 % de codes appariés : une cible qui partage moins
            # de la moitié des codes est écartée sans calcul (valeurs banales « 1 », « oui »).
            shared_codes: Counter[str] = Counter()
            for value, label in pairs:
                targets = {t.id for t in index.by_value.get(value, [])}
                if label:
                    targets |= {t.id for t in index.by_label.get(label, [])}
                shared_codes.update(targets)
            for target_id, n_shared in shared_codes.items():
                target = index.by_id[target_id]
                if (target.id in found or PLACEHOLDER in target.quality_flags
                        or len(target.codes) > MAX_CODES_FOR_FUZZY
                        or n_shared < MIN_SHARED_FOR_FUZZY * max(len(cl.codes), len(target.codes))):
                    continue
                scored = fuzzy_score(cl, target)
                if scored is not None and scored[0] >= fuzzy_threshold:
                    add(target, "fuzzy", min(scored[0], 0.99))

    return sorted(
        found.values(),
        key=lambda m: (STATUS_RANK[m.status], LEVEL_RANK.get(index.level(m.target), 3), -m.confidence),
    )


def check_codelists(
    codelists: list[CodeList], index: ReferenceIndex, *, max_matches: int = 3
) -> list[CheckResult]:
    """Contrôle chaque liste fournie contre l'index."""
    results = []
    for cl in codelists:
        reused = index.by_id.get(cl.id)
        if reused is not None:
            results.append(CheckResult(cl, REUTILISEE, reused=reused))
            continue
        matches = find_matches(cl, index)[:max_matches]
        status = matches[0].status if matches else NOUVELLE
        results.append(CheckResult(cl, status, matches))
    return results


def internal_duplicates(codelists: list[CodeList]) -> list[dict[str, Any]]:
    """Doublons entre les listes du fichier lui-même (mêmes règles que le pipeline)."""
    pairs: dict[tuple[str, str], dict[str, Any]] = {}

    def add(a: CodeList, b: CodeList, detection: str, confidence: float) -> None:
        key = tuple(sorted((a.id, b.id)))
        pairs.setdefault(key, {"a": a, "b": b, "detection": detection, "confidence": round(confidence, 4)})

    for group in detect_exact_duplicates(codelists).values():
        for other in group[1:]:
            add(group[0], other, "exact", 1.0)
    for detection, groups, confidence in (
        ("exact_business", detect_business_duplicates(codelists), 0.98),
        ("labels", detect_label_duplicates(codelists), 0.95),
    ):
        for group in groups:
            for other in group[1:]:
                add(group[0], other, detection, confidence)
    detected, _, _, _ = detect_fuzzy_duplicates(codelists)
    for pair in detected:
        add(pair["a"], pair["b"], "fuzzy", min(pair["score"], 0.99))
    return list(pairs.values())


def code_diff(new: CodeList, ref: CodeList) -> dict[str, list]:
    """Différences de codes (par valeur normalisée) entre la liste fournie et une référence."""
    new_codes = {normalize(v): (v, lab) for v, lab in new.codes}
    ref_codes = {normalize(v): (v, lab) for v, lab in ref.codes}
    return {
        "codes_en_plus": [list(new_codes[k]) for k in new_codes if k not in ref_codes],
        "codes_en_moins": [list(ref_codes[k]) for k in ref_codes if k not in new_codes],
        "libelles_differents": [
            [new_codes[k][0], new_codes[k][1], ref_codes[k][1]]
            for k in new_codes
            if k in ref_codes and normalize(new_codes[k][1]) != normalize(ref_codes[k][1])
        ],
    }


def suggested_action(result: CheckResult, index: ReferenceIndex) -> str:
    """Action conseillée au producteur, d'après la meilleure correspondance."""
    if result.status == REUTILISEE:
        return "Liste déjà publiée et réutilisée : rien à faire."
    if result.status == NOUVELLE:
        return "Aucune liste équivalente publiée : nouvelle liste."
    best = result.matches[0]
    name = f"« {best.target.name or best.target.id} »"
    level = index.level(best.target)
    if best.status == DOUBLON:
        if level == OFFICIELLE:
            return f"Utiliser la nomenclature officielle {name} au lieu de créer cette liste."
        if level == COMMUNE:
            return f"Réutiliser la liste commune {name}."
        series = ", ".join(sorted(best.target.series)) or "?"
        return f"Liste équivalente à {name} (série {series}) : la réutiliser plutôt que la recréer."
    if best.detection == "inclusion":
        return f"Sous-ensemble de {name} : réutiliser {name}, ou vérifier que la restriction est voulue."
    if best.detection == "extension":
        extra = len(code_diff(result.codelist, best.target)["codes_en_plus"])
        return (f"Étend {name} de {extra} code(s) : codes d'échappement à porter par une "
                f"représentation des valeurs manquantes, ou nouvelle version de {name} ?")
    if best.detection == "lineage":
        return (f"Copiée de {name} (BasedOnObject) puis modifiée : vérifier qu'une nouvelle "
                "liste est nécessaire.")
    return f"Proche de {name} : vérifier les différences avant de créer une nouvelle liste."
