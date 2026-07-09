"""Extraction des éléments DDI depuis un fichier XML."""

from __future__ import annotations

from lxml import etree

from .s3 import make_s3_filesystem
from .types import CodeList, VariableRef


def read_bytes(source: str) -> bytes:
    """Lit la source : objet S3 (via s3fs) ou fichier local."""
    if source.startswith("s3://"):
        fs = make_s3_filesystem()
        with fs.open(source, "rb") as f:
            return f.read()
    with open(source, "rb") as f:
        return f.read()


def _local(el: etree._Element) -> str:
    """Nom de balise sans namespace."""
    return etree.QName(el).localname


def _child(el: etree._Element, name: str) -> etree._Element | None:
    """Premier enfant direct dont le nom local est `name`."""
    for c in el:
        if _local(c) == name:
            return c
    return None


def _text_of(el: etree._Element, name: str) -> str:
    """Texte d'un enfant direct."""
    c = _child(el, name)
    return (c.text or "").strip() if c is not None else ""


def _localized(el: etree._Element, name: str) -> str:
    """Texte d'un conteneur (CodeListName, r:Label...) -> son r:String / r:Content."""
    c = _child(el, name)
    if c is None:
        return ""
    kids = list(c)
    return (kids[0].text or "").strip() if kids else (c.text or "").strip()


def parse_xml(raw: bytes) -> list[etree._Element]:
    """Parse le XML et retourne tous les objets (enfants des Fragments)."""
    root = etree.fromstring(raw)
    objects = [
        obj
        for frag in root.iter()
        if _local(frag) == "Fragment"
        for obj in frag
    ]
    return objects


def extract_categories(objects: list[etree._Element]) -> dict[str, str]:
    """Index {cat_id: libellé}."""
    index: dict[str, str] = {}
    for o in objects:
        if _local(o) != "Category":
            continue
        cat_id = _text_of(o, "ID")
        label = _localized(o, "Label") or _localized(o, "CategoryName") or ""
        if cat_id:
            index[cat_id] = label
    return index


def extract_codelists(objects: list[etree._Element]) -> list[CodeList]:
    """
    Extraire toutes les CodeLists avec leurs codes résolu (valeur + libellé).

    Args:
        objects: Liste des éléments XML parée depuis parse_xml.

    Returns:
        Liste d'objets CodeList.
    """
    cat_index = extract_categories(objects)

    codelists: list[CodeList] = []
    # Construire index {var_id -> cl_id} pour associer les variables aux CLs
    var_to_cl: dict[str, str] = {}
    for o in objects:
        if _local(o) not in ("Variable", "RepresentedVariable"):
            continue
        cl_ref_el = None
        code_repr_el = _child(o, "CodeRepresentation")
        if code_repr_el is None:
            vr_el = _child(o, "VariableRepresentation")
            if vr_el is not None:
                code_repr_el = _child(vr_el, "CodeRepresentation")
        if code_repr_el is not None:
            cl_ref_el = _child(code_repr_el, "CodeListReference")
        if cl_ref_el is not None:
            cl_id_val = _text_of(cl_ref_el, "ID")
            var_id_val = _text_of(o, "ID")
            if cl_id_val and var_id_val:
                var_to_cl.setdefault(cl_id_val, set()).add(var_id_val)

    for o in objects:
        if _local(o) not in ("CodeList",):
            continue

        codes: list[tuple[str, str]] = []
        cat_ids: set[str] = set()
        for code in o.iter():
            if _local(code) != "Code":
                continue
            value = _text_of(code, "Value")
            ref = _child(code, "CategoryReference")
            cat_id_val = _text_of(ref, "ID") if ref is not None else ""
            labels = cat_index.get(cat_id_val, "")
            codes.append((value, labels))
            if cat_id_val:
                cat_ids.add(cat_id_val)

        cl_id = _text_of(o, "ID")
        cl_name = _localized(o, "CodeListName")
        cl_label = _localized(o, "Label")

        if cl_id:
            codelists.append(
                CodeList(
                    id=cl_id,
                    name=cl_name,
                    label=cl_label,
                    codes=codes,
                    var_ids=var_to_cl.get(cl_id, set()),
                    cat_ids=cat_ids,
                )
            )

    return codelists


def extract_variables(objects: list[etree._Element]) -> list[VariableRef]:
    """
    Extraire toutes les Variable / RepresentedVariable qui référencent une CodeList.

    Returns:
        Liste d'objets VariableRef avec cl_id éventuellement résolu.
    """
    variables: list[VariableRef] = []
    for o in objects:
        if _local(o) not in ("Variable", "RepresentedVariable"):
            continue

        var_name = (
            _localized(o, "VariableName")
            or _localized(o, "RepresentedVariableName")
            or _text_of(o, "ID")
        )
        var_label = _localized(o, "Label") or _text_of(o, "Description") or ""
        var_id = _text_of(o, "ID")

        code_repr = None
        # En DDI 3.3, CodeRepresentation est sous VariableRepresentation
        vr = _child(o, "VariableRepresentation")
        if vr is not None:
            code_repr = _child(vr, "CodeRepresentation")
        if code_repr is None:
            # fallback : Direct child (XML samples / DDI antérieur)
            code_repr = _child(o, "CodeRepresentation")
        if code_repr is None:
            continue

        cl_ref = _child(code_repr, "CodeListReference")
        if cl_ref is None:
            continue

        cl_id = _text_of(cl_ref, "ID")

        variables.append(
            VariableRef(
                id=var_id,
                name=var_name,
                label=var_label,
                cl_id=cl_id if cl_id else None,
            )
        )

    return variables


def full_extract(source: str | bytes) -> dict:
    """
    Extraction complète : XML -> objets.

    Args:
        source: URL S3 ou chemin local, ou bytes bruts.

    Returns:
        Dict avec 'codelists', 'variables', 'category_count', 'cl_count', 'var_count'.
    """
    if isinstance(source, bytes):
        raw = source
    else:
        raw = read_bytes(source)

    objects = parse_xml(raw)
    codelists = extract_codelists(objects)
    variables = extract_variables(objects)

    return {
        "codelists": codelists,
        "variables": variables,
        "category_count": len(extract_categories(objects)),
        "cl_count": len(codelists),
        "var_count": len(variables),
    }
