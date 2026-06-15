#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
===========================================================
Variables (représentées) DDI ↔ Concepts SKOS – Alignement
===========================================================

✔ Chargement de plusieurs fichiers DDI XML
✔ Résolution des *Reference
✔ Alignement Variables / RepresentedVariables ↔ Concepts SKOS
✔ Détection de doublons :
  - CodeLists
  - Variables
  - RepresentedVariables
✔ Génération RML

"""

# ==========================================================
# IMPORTS (stdlib uniquement)
# ==========================================================

import json
import re
import itertools
import xml.etree.ElementTree as ET
from difflib import SequenceMatcher

# ==========================================================
# PARAMÈTRES GLOBAUX
# ==========================================================

SIM_THRESHOLD_CONCEPT = 0.60
SIM_THRESHOLD_DUPLICATE = 0.90

WEIGHTS = {
    "label": 0.8,
    "definition": 0.2
}

# ==========================================================
# ESPACES DE NOM DDI 3.3
# ==========================================================

NS = {
    "ddi": "ddi:instance:3_3",
    "lp": "ddi:logicalproduct:3_3",
    "r": "ddi:reusable:3_3"
}

# ==========================================================
# NORMALISATION TEXTE
# ==========================================================


def normalize(txt):
    if not txt:
        return ""
    txt = txt.lower()
    txt = re.sub(r"[^\w\s]", " ", txt)
    txt = re.sub(r"\s+", " ", txt)
    return txt.strip()


def text_similarity(a, b):
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, normalize(a), normalize(b)).ratio()


def jaccard(a, b):
    sa = set(normalize(a).split())
    sb = set(normalize(b).split())
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)

# ==========================================================
# CHARGEMENT MULTI-FICHIERS DDI
# ==========================================================


def load_ddi(files):
    """
    Charge plusieurs fichiers DDI et indexe tous les objets par ID.
    """
    objects = {}
    roots = []

    for f in files:
        tree = ET.parse(f)
        root = tree.getroot()
        roots.append(root)

        for frag in root.findall("ddi:Fragment", NS):
            for child in frag:
                oid = child.find("r:ID", NS)
                if oid is not None:
                    objects[oid.text] = child

    return roots, objects

# ==========================================================
# EXTRACTION DDI
# ==========================================================


def extract_urn(el):
    urn = el.findtext(".//r:URN", namespaces=NS)
    return urn.strip() if urn else ""


def extract_label(el):
    label_entry = el.find(".//r:Label/r:Content", NS)
    return label_entry.text.strip() if label_entry is not None and label_entry.text else ""


def extract_codelist_name(el):
    """
    Retourne le CodeListName d'une CodeList DDI
    """
    name_el = el.find(".//CodeListName/r:String", namespaces=NS)
    return name_el.text.strip() if name_el is not None else ""


def extract_category_name(el):
    """
    Retourne le CategoryName d'une Category DDI
    """
    name_el = el.find(".//CategoryName/r:String", namespaces=NS)
    return name_el.text.strip() if name_el is not None else ""


def extract_variable_name(el):
    """
    Retourne le nom d'une Variable ou RepresentedVariable DDI
    """
    name_el = el.find(".//VariableName/r:String", namespaces=NS)
    if name_el is None:
        name_el = el.find(".//RepresentedVariableName/r:String", namespaces=NS)
    return name_el.text.strip() if name_el is not None else ""


def extract_description(el):
    d = el.find(".//r:Description/r:Content", NS)
    return d.text.strip() if d is not None and d.text else ""


def extract_codelist_ref(el):
    ref = el.find(".//r:CodeListReference/r:ID", NS)
    return ref.text if ref is not None else None


def extract_codelist_values(codelist):
    values = []
    for code in codelist.findall("lp:Code", NS):
        v = code.find("r:Value", NS)
        if v is not None and v.text:
            values.append(v.text)
    return values

# ==========================================================
# CHARGEMENT CONCEPTS SKOS (JSON RDF)
# ==========================================================

# --- Fonction pour le prefLabel en français ---


def extract_pref_label_fr(props):
    labels = props.get(
        "http://www.w3.org/2004/02/skos/core#prefLabel", []
    )
    for entry in labels:
        if entry.get("lang") == "fr":
            return entry.get("value", "")
    return ""


def extract_concept_uri_from_definition(def_uri):
    parts = def_uri.split("/definition/")
    if len(parts) < 3:
        return None
    return parts[0] + "/definition/" + parts[1]


def load_concepts(definition_file):
    """
    Charge les concepts SKOS et leurs définitions depuis un JSON INSEE.

    - concepts : skos:Concept avec skos:prefLabel
    - définitions : ExplanatoryNote avec pav:version, xkos:plainText
    - conserve uniquement la dernière version FR courante
    """

    with open(definition_file, encoding="utf-8") as f:
        data = json.load(f)

    # --- 1. index des labels de concepts ---
    concept_labels = {}

    for uri, props in data.items():
        if "http://www.w3.org/2004/02/skos/core#prefLabel" not in props:
            continue

        label_fr = extract_pref_label_fr(props)

        if label_fr:
            # normalisation URI concept
            concept_uri = uri.rstrip("/")
            concept_labels[uri] = label_fr

    # --- 2. index des définitions ---
    definitions_tmp = {}

    for def_uri, props in data.items():

        # uniquement les définitions
        if "http://rdf-vocabulary.ddialliance.org/xkos#plainText" not in props:
            continue

        # ignorer les définitions non courantes
        if "http://rdf.insee.fr/def/base#validUntil" in props:
            continue

        # langue
        lang = next(
            (lang_entry["value"] for lang_entry in props.get(
                "http://purl.org/dc/terms/language", [])),
            None
        )
        if lang != "fr":
            continue

        # version
        versions = props.get("http://purl.org/pav/version", [])
        if not versions:
            continue
        pav_version = max(int(v["value"]) for v in versions)

        # texte
        texts = props.get(
            "http://rdf-vocabulary.ddialliance.org/xkos#plainText", []
        )
        definition_text = " ".join(t["value"] for t in texts)

        # URI concept
        concept_uri = extract_concept_uri_from_definition(def_uri)
        if not concept_uri:
            continue

        if (
            concept_uri not in definitions_tmp
            or pav_version > definitions_tmp[concept_uri]["version"]
        ):
            definitions_tmp[concept_uri] = {
                "version": pav_version,
                "definition": definition_text
            }

    # --- 3. fusion finale ---
    concepts = {}

    for uri, def_data in definitions_tmp.items():
        concepts[uri] = {
            "label": concept_labels.get(uri, ""),
            "definition": def_data["definition"]
        }

    print(f"📘 Concepts chargés : {len(concepts)}")
    return concepts


# ==========================================================
# ALIGNEMENT VARIABLES ↔ CONCEPTS
# ==========================================================


def align_objects(objects, concepts, debug_md="align_debug.md"):
    matches = []

    total_vars = 0
    empty_vars = []
    accepted = []
    rejected = []

    with open(debug_md, "w", encoding="utf-8") as md:
        md.write("# 🔗 Alignement Variables ↔ Concepts\n\n")

        md.write("## 📊 Résumé\n")
        md.write(f"- Objets DDI analysés : {len(objects)}\n")
        md.write(f"- Concepts : {len(concepts)}\n")
        md.write(f"- Seuil : {SIM_THRESHOLD_CONCEPT}\n\n")

        for oid, el in objects.items():
            if not (
                el.tag.endswith("Variable")
                or el.tag.endswith("RepresentedVariable")
            ):
                continue

            total_vars += 1

            label = extract_label(el)
            desc = extract_description(el)

            if not label and not desc:
                empty_vars.append(oid)
                continue

            best_uri, best_score = None, 0.0
            score_details = []

            for curi, c in concepts.items():
                s_label = text_similarity(label, c["label"])
                s_def = text_similarity(desc, c["definition"])

                score = (
                    WEIGHTS["label"] * s_label +
                    WEIGHTS["definition"] * s_def
                )

                if score > 0.02:  # évite le bruit
                    score_details.append(
                        (curi, score, s_label, s_def)
                    )

                if score > best_score:
                    best_uri, best_score = curi, score

            var_type = el.tag.split("}")[-1]

            if best_score >= SIM_THRESHOLD_CONCEPT:
                accepted.append((oid, var_type, best_uri, best_score))
                matches.append({
                    "id": oid,
                    "label": label,
                    "type": var_type,
                    "concept": best_uri,
                    "score": best_score
                })
            else:
                rejected.append((oid, var_type, best_uri, best_score))

        # -----------------------------
        # ✍️ Écriture Markdown
        # -----------------------------

        md.write("## ⚠️ Variables sans texte exploitable\n")
        if empty_vars:
            for oid in empty_vars:
                md.write(f"- `{oid}`\n")
        else:
            md.write("- _Aucune_\n")
        md.write("\n---\n\n")

        md.write("## ⭐ Alignements retenus\n")
        for oid, vtype, curi, score in sorted(accepted, key=lambda x: -x[3]):
            c = concepts.get(curi, {})
            md.write(f"### 🧩 `{oid}` ({vtype})\n")
            # 🔹 label DDI (Variable / RepresentedVariable)
            var_label = extract_label(objects[oid])
            if var_label:
                md.write(f"- 🏷️ **Label DDI** : *{var_label}*\n")
            md.write(f"- 🧠 **Concept** : {c.get('label', curi)}\n")
            md.write(f"- 📈 **Score** : **{score:.3f}**\n\n")

        md.write("\n---\n\n")

        md.write("## ❌ Variables rejetées (meilleur score)\n")
        for oid, vtype, curi, score in sorted(rejected, key=lambda x: -x[3])[:50]:
            md.write(f"- 🔸 `{oid}` ({vtype}) → {score:.3f}\n")

        md.write("\n---\n\n")

        md.write("## 📌 Statistiques finales\n")
        md.write(f"- Variables analysées : {total_vars}\n")
        md.write(f"- Alignements retenus : {len(accepted)}\n")
        md.write(f"- Rejetées : {len(rejected)}\n")

    print(f"📝 Journal d’alignement écrit dans {debug_md}")
    return matches


# ==========================================================
# DÉTECTION DE DOUBLONS
# ==========================================================

# ---- Fonction de concaténation de texte des listes de code ----


def concat_codelist_text(cl):
    """
    Concatène tous les éléments textuels d'une CodeList pour le scoring :
      - CodeListName
      - CodeList Label
      - Codes / Value
      - Categories (CategoryName + Category Label)
    """
    parts = []

    # CodeListName
    if "name" in cl:
        parts.append(cl["name"])

    # CodeList Label
    if "label" in cl:
        parts.append(cl["label"])

    # Codes
    for code in cl.get("codes", []):
        if "value" in code:
            parts.append(code["value"])
        # Category (lien)
        cat = code.get("category")
        if cat:
            if "name" in cat:
                parts.append(cat["name"])
            if "label" in cat:
                parts.append(cat["label"])

    return " ".join(parts)

# ---- Fonction de concaténation de texte des variables ----


def concat_variable_text(var):
    """
    Concatène tous les éléments textuels d'une Variable ou RepresentedVariable :
      - VariableName ou RepresentedVariableName
      - Label
      - Description
    """
    parts = []

    if "name" in var:
        parts.append(var["name"])
    if "label" in var:
        parts.append(var["label"])
    if "description" in var:
        parts.append(var["description"])

    return " ".join(parts)

# ---- Détection des doublons CodeLists ----


def detect_codelist_duplicates(codelists, sim_threshold=SIM_THRESHOLD_DUPLICATE):
    """
    Détecte les doublons parmi les CodeLists en comparant tous les champs pertinents.
    Retourne une liste de tuples :
      (cl1_id, cl2_id, score, cl1_uri, cl2_uri, cl1_label, cl2_label)
    """
    dups = []

    for a, b in itertools.combinations(codelists, 2):
        text_a = concat_codelist_text(a)
        text_b = concat_codelist_text(b)

        s = text_similarity(text_a, text_b)

        if s >= sim_threshold:
            dups.append((
                a["id"],
                b["id"],
                s,
                a.get("uri", ""),
                b.get("uri", ""),
                a.get("label", ""),
                b.get("label", "")
            ))

    return dups

# ---- Détection des doublons Variables / RepresentedVariables ----


def detect_variable_duplicates(variables, sim_threshold=SIM_THRESHOLD_DUPLICATE):
    """
    Détecte les doublons parmi les Variables ou RepresentedVariables
    en comparant les champs textuels (name, label, description).
    Retourne une liste de tuples :
      (var1_id, var2_id, score, var1_uri, var2_uri, var1_label, var2_label)
    """
    dups = []

    for a, b in itertools.combinations(variables, 2):
        text_a = concat_variable_text(a)
        text_b = concat_variable_text(b)

        s = text_similarity(text_a, text_b)

        if s >= sim_threshold:
            dups.append((
                a["id"],
                b["id"],
                s,
                a.get("uri", ""),
                b.get("uri", ""),
                a.get("label", ""),
                b.get("label", "")
            ))

    return dups


# ==========================================================
# RML – PRÉFIXES
# ==========================================================


RML_HEADER = """@prefix rr: <http://www.w3.org/ns/r2rml#> .
@prefix skos: <http://www.w3.org/2004/02/skos/core#> .
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .
@prefix ex: <http://example.org/ddi-align/> .

"""

# ==========================================================
# RML – ALIGNEMENTS
# ==========================================================


def write_rml_align(matches, outfile):
    with open(outfile, "w", encoding="utf-8") as f:
        f.write(RML_HEADER)
        for i, m in enumerate(matches, 1):
            f.write(f"""
ex:Align{i}
  a rr:TriplesMap ;
  rr:subjectMap [ rr:constant <urn:ddi:{m['type']}:{m['id']}> ] ;
  rr:predicateObjectMap [
    rr:predicate skos:closeMatch ;
    rr:object <{m['concept']}>
  ] ;
  rr:predicateObjectMap [
    rr:predicate ex:confidence ;
    rr:objectMap [
      rr:constant "{m['score']:.3f}"^^xsd:decimal
    ]
  ] .
""")

# ==========================================================
# RML – DOUBLONS
# ==========================================================


def rml_var_duplicates(dups, obj_type, outfile):
    """
    Écrit un fichier RML TTL pour les doublons de Variables ou RepresentedVariables.
    Chaque tuple de dups doit être :
    (id_a, id_b, score, uri_a, uri_b, label_a, label_b)
    """
    with open(outfile, "w", encoding="utf-8") as f:
        # Préfixes RML
        f.write(RML_HEADER)

        for i, (a, b, s, a_uri, b_uri, a_label, b_label) in enumerate(dups, 1):
            f.write(f"""
ex:{obj_type}Dup{i}
  a rr:TriplesMap ;
  rr:subjectMap [ rr:constant <{a_uri}> ] ;
  rr:predicateObjectMap [
    rr:predicate owl:sameAs ;
    rr:object <{b_uri}>
  ] ;
  rr:predicateObjectMap [
    rr:predicate ex:similarityScore ;
    rr:objectMap [
      rr:constant "{s:.3f}"^^xsd:decimal
    ]
  ] ;
  rr:predicateObjectMap [
    rr:predicate rdfs:label ;
    rr:objectMap [
      rr:constant "{a_label} ↔ {b_label}"
    ]
  ] .
""")


def rml_codelist_duplicates(dups, obj_type, outfile):
    """
    Écrit un fichier RML TTL pour les doublons de CodeLists.
    Chaque tuple de dups doit être :
    (id_a, id_b, score, uri_a, uri_b, label_a, label_b)
    """
    with open(outfile, "w", encoding="utf-8") as f:
        # Préfixes RML
        f.write(RML_HEADER)

        for i, (a, b, s, a_uri, b_uri, a_label, b_label) in enumerate(dups, 1):
            f.write(f"""
ex:{obj_type}Dup{i}
  a rr:TriplesMap ;
  rr:subjectMap [ rr:constant <{a_uri}> ] ;
  rr:predicateObjectMap [
    rr:predicate skos:closeMatch ;
    rr:object <{b_uri}>
  ] ;
  rr:predicateObjectMap [
    rr:predicate ex:similarityScore ;
    rr:objectMap [
      rr:constant "{s:.3f}"^^xsd:decimal
    ]
  ] ;
  rr:predicateObjectMap [
    rr:predicate rdfs:label ;
    rr:objectMap [
      rr:constant "{a_label} ↔ {b_label}"
    ]
  ] .
""")


# ==========================================================
# MAIN
# ==========================================================


if __name__ == "__main__":

    # ---- FICHIERS DDI ----
    ddi_files = [
        "RSLDDI_out.xml"
    ]

    # ---- FICHIERS CONCEPTS ----
    definition_file = "skos_definition.json"

    # ---- CHARGEMENT ----
    roots, objects = load_ddi(ddi_files)
    concepts = load_concepts(definition_file)
    print(list(concepts.items())[:1])

    # ---- ALIGNEMENTS ----
    matches = align_objects(objects, concepts)
    write_rml_align(matches, "rml_variable_concept.ttl")

    # ---- DOUBLONS VARIABLES ET CODELISTS ----
    vars_simple = [
        {
            "id": oid,
            "uri": extract_urn(el),
            "name": extract_variable_name(el),
            "label": extract_label(el),
            "description": extract_description(el)
        }
        for oid, el in objects.items()
        if el.tag.endswith("Variable") or el.tag.endswith("RepresentedVariable")
    ]

    cl_simple = []
for oid, el in objects.items():
    if el.tag.endswith("CodeList"):
        cl_data = {
            "id": oid,
            "uri": extract_urn(el),
            "name": extract_codelist_name(el),
            "label": extract_label(el),
            "codes": []
        }
        # Parcours des codes
        for code_el in el.findall(".//Code"):
            code_data = {
                "value": code_el.findtext(".//r:Value", namespaces=NS),
                "category": None
            }
            cat_ref = code_el.find(".//r:CategoryReference", namespaces=NS)
            if cat_ref is not None:
                cat_id = cat_ref.findtext(".//r:ID", namespaces=NS)
                if cat_id and cat_id in objects:
                    cat_el = objects[cat_id]
                    code_data["category"] = {
                        "id": cat_id,
                        "name": extract_category_name(cat_el),
                        "label": extract_label(cat_el)
                    }
            cl_data["codes"].append(code_data)
        cl_simple.append(cl_data)

# Doublons
var_dups = detect_variable_duplicates(vars_simple)
cl_dups = detect_codelist_duplicates(cl_simple)
rml_var_duplicates(var_dups, "Variable", "rml_variable_duplicates.ttl")
rml_codelist_duplicates(cl_dups, "CodeList", "rml_codelists_duplicates.ttl")

print("✅ Fichiers d'appariement rml_variable_concept.ttl, rml_variable_duplicates.ttl et rml_codelists_duplicates.ttl écrits")
