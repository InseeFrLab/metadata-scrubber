"""make_demo_files.py — Génère 2 fichiers DDI de démo depuis BTS.xml et BPE.xml (S3).

Produit demo/BTS_demo.xml et demo/BPE_demo.xml (≤10 CodeLists chacun) :
  - BTS_demo : paires réelles intra-fichier (exact, fuzzy, sémantique) + 2 listes uniques ;
  - BPE_demo : variantes annuelles intra-fichier + 2 copies de listes BTS avec ids neufs
    (une exacte, une fuzzy) pour démontrer la détection contre le registre nettoyé
    à l'opération 2.

Usage : uv run python demo/make_demo_files.py [--no-upload]
Upload : s3://projet-metadonnees-rmes/demo/{BTS_demo.xml,BPE_demo.xml}
"""

from __future__ import annotations

import copy
import json
import sys
import uuid
from pathlib import Path

from lxml import etree

_repo = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_repo / "src"))

from metadata_scrubber.extractor import _child, _local, _localized, _text_of  # noqa: E402
from metadata_scrubber.s3 import make_s3_filesystem  # noqa: E402

DDI_NS = "ddi:instance:3_3"
NSMAP = {"ddi": DDI_NS, "r": "ddi:reusable:3_3"}

# ── Sélection BTS (ids issus de audit/codelist_duplicates.json) ─────────
BTS_PAIRS = {
    "exact": ("83e7cfd6-238a-4fc9-a8d2-31509d6eae36",   # N_NAF_6
              "659fb0d2-c282-42bb-8926-a253930275e1"),  # N-ACTIVITE-FRANCAISE-6-POSTES
    "fuzzy+semantic": ("e8150570-6553-449f-be7d-279f37cf24e6",   # N_nat_etab_23
                       "35ce90ea-452d-48a0-a2f6-85e9801ac744"),  # NAT_ETAB
    "fuzzy": ("7354587f-a35f-40fc-b5cf-e73cc5e67730",   # N_champ_23
              "5e7f9d52-0865-4059-8f01-cd756a784934"),  # N_CHAMP_2022
    "semantic": ("44938100-e3db-4a1b-a802-14fc02aa2c33",   # N_etat_23
                 "3191589c-b2ff-4326-a185-83ecaceb86bb"),  # Etat_2020
}

# ── Sélection BPE (par nom, ids résolus au run) ─────────────────────────
BPE_NAMES = [
    "L_VAR_SPECIF_2023", "L_VAR_SPECIF_2021",      # variantes annuelles
    "L_AN_BPE_EVOL_2023", "L_AN_BPE_EVOL_2022",    # variantes annuelles
    "L_SECT_2023", "L_SSTYPHEB",                   # uniques
    "L_QUALITE_ZONAGE_2023",                       # unique — démo ajout manuel au registre
]

# Altérations de libellés pour la copie fuzzy (style de la vraie paire NAT_ETAB)
FUZZY_LABEL_TWEAKS = {
    "Unité active": "Unité en activité",
    "Non renseigné": "Non renseignée",
    "Unité inscrite sans activité": "Unités inscrites sans activité",
}


# ────────────────────────────────────────────────────────────────────────
# Lecture + indexation
# ────────────────────────────────────────────────────────────────────────


def read_s3_root(url: str) -> etree._Element:
    fs = make_s3_filesystem()
    with fs.open(url, "rb") as f:
        return etree.fromstring(f.read())


def index_fragments(root):
    """Retourne (frag, obj, type, id) pour chaque Fragment du document."""
    out = []
    for frag in root.iter():
        if _local(frag) != "Fragment":
            continue
        for obj in frag:
            out.append((frag, obj, _local(obj), _text_of(obj, "ID")))
    return out


def category_ids_of(cl_obj) -> list[str]:
    """Ids des catégories référencées par les Codes d'une CodeList."""
    ids = []
    for code in cl_obj.iter():
        if _local(code) != "Code":
            continue
        ref = _child(code, "CategoryReference")
        if ref is not None:
            cid = _text_of(ref, "ID")
            if cid:
                ids.append(cid)
    return ids


def codelist_ref_of(var_obj) -> str:
    """Id de la CodeList référencée par une Variable/RepresentedVariable."""
    code_repr = _child(var_obj, "CodeRepresentation")
    if code_repr is None:
        var_repr = _child(var_obj, "VariableRepresentation")
        if var_repr is not None:
            code_repr = _child(var_repr, "CodeRepresentation")
    if code_repr is None:
        return ""
    ref = _child(code_repr, "CodeListReference")
    return _text_of(ref, "ID") if ref is not None else ""


def collect_fragments(fragments, cl_ids: list[str]) -> list:
    """Fragments CodeList + Category + Variable pour les CodeLists données."""
    frag_by_cl = {}
    cat_frag_by_id = {}
    var_frags = []
    for frag, obj, typ, oid in fragments:
        if typ == "CodeList" and oid in cl_ids:
            frag_by_cl[oid] = (frag, obj)
        elif typ == "Category":
            cat_frag_by_id[oid] = frag
        elif typ in ("Variable", "RepresentedVariable"):
            var_frags.append((frag, obj))

    selected = []
    seen_cats = set()
    for cl_id in cl_ids:
        if cl_id not in frag_by_cl:
            raise SystemExit(f"CodeList {cl_id} introuvable dans le fichier source")
        frag, obj = frag_by_cl[cl_id]
        selected.append(frag)
        for cid in category_ids_of(obj):
            if cid not in seen_cats and cid in cat_frag_by_id:
                seen_cats.add(cid)
                selected.append(cat_frag_by_id[cid])
    # Variables référentes (signaux d'usage)
    for frag, obj in var_frags:
        if codelist_ref_of(obj) in cl_ids:
            selected.append(frag)
    return selected


# ────────────────────────────────────────────────────────────────────────
# Clonage inter-fichiers (ids neufs)
# ────────────────────────────────────────────────────────────────────────


def _set_id(obj, new_id: str) -> None:
    """Remplace r:ID et r:URN d'un objet DDI."""
    id_el = _child(obj, "ID")
    old_id = id_el.text
    id_el.text = new_id
    urn = _child(obj, "URN")
    if urn is not None and urn.text and old_id:
        urn.text = urn.text.replace(old_id, new_id)


def clone_codelist(fragments, cl_id: str, new_name: str,
                   label_tweaks: dict[str, str] | None = None) -> list:
    """Clone une CodeList + ses Category avec des ids entièrement neufs.

    Returns:
        Liste de fragments clonés (CodeList + Categories).
    """
    src_frag = None
    cat_frag_by_id = {}
    for frag, obj, typ, oid in fragments:
        if typ == "CodeList" and oid == cl_id:
            src_frag = frag
        elif typ == "Category":
            cat_frag_by_id[oid] = frag
    if src_frag is None:
        raise SystemExit(f"CodeList {cl_id} introuvable (clonage)")

    new_cl_frag = copy.deepcopy(src_frag)
    cl_obj = next(iter(new_cl_frag))
    _set_id(cl_obj, str(uuid.uuid4()))

    # Nom de la liste
    name_el = _child(cl_obj, "CodeListName")
    if name_el is not None and len(name_el):
        name_el[0].text = new_name

    # Codes : ids neufs + catégories clonées (map ancien cat id → nouveau)
    cat_id_map: dict[str, str] = {}
    cloned_cats = []
    for code in cl_obj.iter():
        if _local(code) != "Code":
            continue
        _set_id(code, str(uuid.uuid4()))
        ref = _child(code, "CategoryReference")
        if ref is None:
            continue
        old_cid = _text_of(ref, "ID")
        if old_cid not in cat_id_map:
            new_cid = str(uuid.uuid4())
            cat_id_map[old_cid] = new_cid
            cat_frag = copy.deepcopy(cat_frag_by_id[old_cid])
            cat_obj = next(iter(cat_frag))
            _set_id(cat_obj, new_cid)
            # Altération éventuelle du libellé (variante fuzzy)
            if label_tweaks:
                label_el = _child(cat_obj, "Label")
                if label_el is not None and len(label_el):
                    content = label_el[0]
                    if content.text in label_tweaks:
                        content.text = label_tweaks[content.text]
            cloned_cats.append(cat_frag)
        ref_id_el = _child(ref, "ID")
        ref_id_el.text = cat_id_map[old_cid]

    return [new_cl_frag] + cloned_cats


# ────────────────────────────────────────────────────────────────────────
# Assemblage + écriture
# ────────────────────────────────────────────────────────────────────────


def build_document(selected_fragments) -> bytes:
    root = etree.Element(f"{{{DDI_NS}}}FragmentInstance", nsmap=NSMAP)
    for frag in selected_fragments:
        root.append(copy.deepcopy(frag))
    return etree.tostring(root, xml_declaration=True, encoding="utf-8", pretty_print=True)


def pick_unique_bts_codelists(fragments, exclude: set[str], count: int = 2) -> list[str]:
    """Choisit des CodeLists BTS sans doublon connu (hors paires de l'audit)."""
    audit_path = _repo / "audit" / "codelist_duplicates.json"
    implicated: set[str] = set(exclude)
    if audit_path.exists():
        audit = json.loads(audit_path.read_text(encoding="utf-8"))
        implicated |= set(audit.keys())
        implicated |= {d["id"] for v in audit.values() for d in v.get("duplicates", [])}

    picked = []
    for _frag, obj, typ, oid in fragments:
        if typ != "CodeList" or oid in implicated:
            continue
        n_codes = len([c for c in obj.iter() if _local(c) == "Code"])
        if 3 <= n_codes <= 12:
            picked.append(oid)
            if len(picked) == count:
                break
    return picked


def summarize(path: Path) -> None:
    from metadata_scrubber.extractor import extract_codelists, parse_xml

    objects = parse_xml(path.read_bytes())
    cls = extract_codelists(objects)
    print(f"  {path.name}: {len(cls)} CodeLists")
    for cl in cls:
        labels_ok = all(label for _v, label in cl.codes)
        print(f"    - {cl.name or cl.id[:8]} ({len(cl.codes)} codes, "
              f"libellés {'OK' if labels_ok else 'MANQUANTS'}, {len(cl.vars) or len(cl.var_ids)} vars)")


def main() -> None:
    upload = "--no-upload" not in sys.argv
    out_dir = Path(__file__).resolve().parent

    print("Lecture de BTS.xml et BPE.xml depuis S3...")
    bts_root = read_s3_root("s3://projet-metadonnees-rmes/BTS.xml")
    bpe_root = read_s3_root("s3://projet-metadonnees-rmes/BPE.xml")
    bts_frags = index_fragments(bts_root)
    bpe_frags = index_fragments(bpe_root)

    # ── BTS_demo : 4 paires réelles + 2 uniques ──
    bts_pair_ids = [i for pair in BTS_PAIRS.values() for i in pair]
    bts_unique = pick_unique_bts_codelists(bts_frags, set(bts_pair_ids))
    bts_ids = bts_pair_ids + bts_unique
    print(f"\nBTS_demo : {len(bts_ids)} CodeLists "
          f"({', '.join(f'{k}: 1 paire' for k in BTS_PAIRS)}, {len(bts_unique)} uniques)")
    bts_selected = collect_fragments(bts_frags, bts_ids)

    # ── BPE_demo : listes BPE + 2 copies de BTS (ids neufs) ──
    name_to_id = {}
    for _f, obj, typ, oid in bpe_frags:
        if typ == "CodeList":
            name = _localized(obj, "CodeListName")
            if name in BPE_NAMES:
                name_to_id[name] = oid
    missing = [n for n in BPE_NAMES if n not in name_to_id]
    if missing:
        raise SystemExit(f"CodeLists BPE introuvables : {missing}")
    bpe_ids = [name_to_id[n] for n in BPE_NAMES]
    bpe_selected = collect_fragments(bpe_frags, bpe_ids)

    # Copies inter-fichiers depuis BTS : exacte (N_NAF_6) et fuzzy (N_etat_23)
    print(f"BPE_demo : {len(BPE_NAMES)} listes BPE + copie exacte de N_NAF_6 (NAF_6_POSTES_BPE) "
          "+ variante fuzzy de N_etat_23 (ETAT_UNITE_BPE)")
    bpe_selected += clone_codelist(
        bts_frags, BTS_PAIRS["exact"][0], "NAF_6_POSTES_BPE",
    )
    bpe_selected += clone_codelist(
        bts_frags, BTS_PAIRS["semantic"][0], "ETAT_UNITE_BPE",
        label_tweaks=FUZZY_LABEL_TWEAKS,
    )

    # ── Écriture ──
    print("\nÉcriture des fichiers...")
    bts_path = out_dir / "BTS_demo.xml"
    bpe_path = out_dir / "BPE_demo.xml"
    bts_path.write_bytes(build_document(bts_selected))
    bpe_path.write_bytes(build_document(bpe_selected))

    print("\nContrôle d'extraction :")
    summarize(bts_path)
    summarize(bpe_path)

    if upload:
        print("\nUpload vers s3://projet-metadonnees-rmes/demo/ ...")
        fs = make_s3_filesystem()
        for p in (bts_path, bpe_path):
            dest = f"s3://projet-metadonnees-rmes/demo/{p.name}"
            fs.put(str(p), dest)
            print(f"  → {dest}")

    print("\nPaires attendues :")
    print("  BTS_demo (op 1) : N_NAF_6↔N-ACTIVITE-FRANCAISE-6-POSTES (exact), "
          "N_nat_etab_23↔NAT_ETAB (fuzzy), N_champ_23↔N_CHAMP_2022 (fuzzy), "
          "N_etat_23↔Etat_2020 (sémantique, avec LLM)")
    print("  BPE_demo (op 2, avec registre) : NAF_6_POSTES_BPE↔registre N_NAF_6 (exact), "
          "ETAT_UNITE_BPE↔registre N_etat_23 (fuzzy), "
          "L_VAR_SPECIF_2023↔2021 et L_AN_BPE_EVOL_2023↔2022 (intra)")


if __name__ == "__main__":
    main()
