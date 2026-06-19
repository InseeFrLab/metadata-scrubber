"""Extraction en flux des listes de codes d'un fichier DDI 3.3.

Une seule passe ``lxml.etree.iterparse`` (le flux S3 n'est pas seekable) :
- on accumule les ``Category`` dans un dict ``{id: (name, label)}`` ;
- on accumule les ``CodeList`` avec leurs codes (références de catégorie **non
  résolues**) ;
- après la passe, on résout les libellés (un ``CodeList`` peut référencer une
  ``Category`` qui apparaît plus loin dans le fichier).

La mémoire est libérée au fil de l'eau (``clear()`` + suppression des fragments
déjà traités), ce qui permet de traiter RP.xml (162 Mo) sans tout charger.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import BinaryIO

from lxml import etree

from .model import CodeEntry, CodeListRecord

# Namespaces DDI 3.3
LP = "ddi:logicalproduct:3_3"
R = "ddi:reusable:3_3"
DDI = "ddi:instance:3_3"


def _q(ns: str, tag: str) -> str:
    return f"{{{ns}}}{tag}"


FRAGMENT = _q(DDI, "Fragment")
CODELIST = _q(LP, "CodeList")
CATEGORY = _q(LP, "Category")
CODE = _q(LP, "Code")
CODELIST_NAME = _q(LP, "CodeListName")
CATEGORY_NAME = _q(LP, "CategoryName")

R_ID = _q(R, "ID")
R_URN = _q(R, "URN")
R_VERSION = _q(R, "Version")
R_USERID = _q(R, "UserID")
R_STRING = _q(R, "String")
R_LABEL = _q(R, "Label")
R_CONTENT = _q(R, "Content")
R_DESCRIPTION = _q(R, "Description")
R_VALUE = _q(R, "Value")
R_CATEGORY_REF = _q(R, "CategoryReference")

_XML_LANG = "{http://www.w3.org/XML/1998/namespace}lang"


@dataclass
class ExtractStats:
    n_codelists: int = 0
    n_categories: int = 0
    n_codes: int = 0
    n_unresolved: int = 0


def _text(elem: etree._Element | None) -> str:
    return (elem.text or "").strip() if elem is not None else ""


def _localized(parent: etree._Element, container_tag: str) -> str:
    """Texte d'un conteneur (``CodeListName``, ``r:Label``...) → enfant ``r:String``
    ou ``r:Content``, en préférant la version française."""
    container = parent.find(container_tag)
    if container is None:
        return ""
    children = list(container)
    if not children:
        return _text(container)
    fr = [c for c in children if (c.get(_XML_LANG) or "").lower().startswith("fr")]
    chosen = fr[0] if fr else children[0]
    return _text(chosen)


def _source_id(elem: etree._Element) -> str:
    """r:UserID typeOfUserID='colectica:sourceId' s'il existe, sinon le premier."""
    user_ids = elem.findall(R_USERID)
    for uid in user_ids:
        if uid.get("typeOfUserID") == "colectica:sourceId":
            return _text(uid)
    return _text(user_ids[0]) if user_ids else ""


def _parse_codelist(elem: etree._Element) -> CodeListRecord:
    codes: list[CodeEntry] = []
    # .iter() pour inclure les codes hiérarchiques (Code imbriqués dans un Code).
    for code in elem.iter(CODE):
        value = _text(code.find(R_VALUE))
        ref = code.find(R_CATEGORY_REF)
        cat_id = _text(ref.find(R_ID)) if ref is not None else ""
        codes.append(CodeEntry(value=value, category_id=cat_id or None))

    return CodeListRecord(
        ddi_id=_text(elem.find(R_ID)),
        urn=_text(elem.find(R_URN)),
        version=_text(elem.find(R_VERSION)),
        source_id=_source_id(elem),
        name=_localized(elem, CODELIST_NAME),
        label=_localized(elem, R_LABEL),
        description=_localized(elem, R_DESCRIPTION),
        codes=codes,
    )


def _parse_category(elem: etree._Element) -> tuple[str, str]:
    cat_id = _text(elem.find(R_ID))
    label = _localized(elem, R_LABEL) or _localized(elem, CATEGORY_NAME)
    return cat_id, label


def parse_operation(stream: BinaryIO) -> tuple[list[CodeListRecord], ExtractStats]:
    """Lit un fichier d'opération et retourne les listes de codes résolues + stats."""
    records: list[CodeListRecord] = []
    categories: dict[str, str] = {}
    stats = ExtractStats()

    context = etree.iterparse(stream, events=("end",), tag=(CODELIST, CATEGORY))
    for _event, elem in context:
        if elem.tag == CODELIST:
            rec = _parse_codelist(elem)
            records.append(rec)
            stats.n_codelists += 1
            stats.n_codes += len(rec.codes)
        else:  # CATEGORY
            cat_id, label = _parse_category(elem)
            if cat_id:
                categories[cat_id] = label
            stats.n_categories += 1

        # libère la mémoire : l'élément et les fragments déjà traités
        elem.clear()
        parent = elem.getparent()
        if parent is not None:
            while parent.getprevious() is not None:
                gp = parent.getparent()
                if gp is None:
                    break
                del gp[0]

    # résolution des libellés
    for rec in records:
        for code in rec.codes:
            if code.category_id is not None:
                code.label = categories.get(code.category_id)
            if not code.resolved:
                stats.n_unresolved += 1

    return records, stats


def _generic_name(obj: etree._Element) -> str:
    """Nom d'un objet : 1er enfant dont le localname finit par « Name » (→ r:String)."""
    for child in obj:
        if etree.QName(child).localname.endswith("Name"):
            return _localized(obj, child.tag)
    return ""


def extract_all(stream: BinaryIO) -> tuple[list[dict], Counter]:
    """Extraction **générique** de tous les objets DDI (tous types).

    Une seule passe ; les libellés des codes (CodeList) sont résolus après coup via
    l'index des catégories. Retourne ``(objects, counts)`` où ``objects`` est une
    liste de dicts (champs communs + ``codes`` pour les CodeList).
    """
    objects: list[dict] = []
    categories: dict[str, str] = {}
    counts: Counter = Counter()

    for _event, frag in etree.iterparse(stream, events=("end",), tag=FRAGMENT):
        for obj in frag:
            otype = etree.QName(obj).localname
            counts[otype] += 1
            rec = {
                "type": otype,
                "id": _text(obj.find(R_ID)),
                "urn": _text(obj.find(R_URN)),
                "version": _text(obj.find(R_VERSION)),
                "source_id": _source_id(obj),
                "name": _generic_name(obj),
                "label": _localized(obj, R_LABEL),
                "description": _localized(obj, R_DESCRIPTION),
            }
            if otype == "CodeList":
                codes = []
                for code in obj.iter(CODE):  # inclut les codes hiérarchiques
                    ref = code.find(R_CATEGORY_REF)
                    cat_id = _text(ref.find(R_ID)) if ref is not None else ""
                    codes.append(
                        {"value": _text(code.find(R_VALUE)), "category_id": cat_id or None}
                    )
                rec["codes"] = codes
                counts["Code"] += len(codes)
            elif otype == "Category":
                categories[rec["id"]] = rec["label"] or rec["name"]
            objects.append(rec)

        # libère la mémoire : fragment courant + fragments déjà traités
        frag.clear()
        while frag.getprevious() is not None:
            del frag.getparent()[0]

    # résolution des libellés de codes
    for rec in objects:
        if rec["type"] == "CodeList":
            for c in rec["codes"]:
                c["code_label"] = (
                    categories.get(c["category_id"]) if c["category_id"] else None
                )

    return objects, counts
