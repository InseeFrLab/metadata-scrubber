# %%
# --- Imports & paramètres ---------------------------------------------------
import io
import json
import os
import re
from collections import Counter, defaultdict

import s3fs
from lxml import etree

# Fichier XML DDI à explorer (liste de codes + catégories)
SOURCE = (
    "s3://projet-metadonnees-rmes/"
    "CodeLists&Categories_Dev4_Hors_resource_package_2026-04-21.xml"
)

# Chemin de sortie JSON sur S3 (optionnel)
OUTPUT = "s3://projet-metadonnees-rmes/_exploration_xml_codelists.json"

# Connecteur S3  (pas de profile sur Onyxia : vars env → ok)
fs = s3fs.S3FileSystem(
    profile="default" ,
    endpoint_url="https://minio.lab.sspcloud.fr",
    client_kwargs={"region_name": "us-east-1"},
)
print("Bucket listing :", fs.ls("projet-metadonnees-rmes"))

# --- Pré-nettoyage S3 : suppression du namespace invalide ----------------------
# Le fichier contient un attribut xmlns:="ddi:instance:..." sans préfixe, ce qui
# est strictement interdit par la norme XML. lxml le rejette avec XMLSyntaxError.
# On lit le blob une fois en mémoire (~168 Mo, ok sur Onyxia), on le patche,
# et on le stream via io.BytesIO + iterparse(recover=True) comme filet de sécurité.
print("Téléchargement du fichier depuis S3…")
raw_data = fs.open(SOURCE, "rb").read()
# Supprimer les déclarations xmlns:="" (namespace sans préfixe) partout.
# Pattern : xmlns:=""ddi:instance:3_3"" — le regex matche xmlns:="" suivi de n'importe quoi jusqu'à ""
raw_data = re.sub(rb'xmlns:=""[^""]*""', b"", raw_data)
fh_xml = io.BytesIO(raw_data)
print(f"  ✅ {len(raw_data):,} octets prêts à streamer.")


# %%
# --- Helpers XML (mêmes que explore_standalone.py) ---------------------------
def local(el):
    """Nom de balise sans namespace."""
    return etree.QName(el).localname


def child(el, name):
    """Premier enfant direct dont le nom local est `name`."""
    for c in el:
        if local(c) == name:
            return c
    return None


def text_of(el, name):
    """Texte d'un enfant direct (ex. r:ID, r:Value)."""
    c = child(el, name)
    return (c.text or "").strip() if c is not None else ""


def localized(el, name):
    """
    Texte d'un conteneur (CodeListName, r:Label…) → son r:String / r:Content.
    Prennir le premier enfant texte s'il existe, sinon le texte brut du noeud.
    """
    c = child(el, name)
    if c is None:
        return ""
    kids = list(c)
    return (kids[0].text or "").strip() if kids else (c.text or "").strip()


# %%
# --- Pass 1 — Index des catégories ----------------------------------------
# Le fichier mélange Fragments <Category> et <CodeList> sans saut de ligne.
# Un premier stream itératif construit l'index cat_id → label.

categories_map = {}  # {cat_id: label}
frag_type_counts = Counter()

print("Pass 1 : indexation des catégories…")
fh_xml.seek(0)
for event, elem in etree.iterparse(fh_xml, events=("end",), tag=("*"), recover=True, huge_tree=True):
    tag_name = local(elem)
    frag_type_counts[tag_name] += 1

    if tag_name == "Fragment":
        children = [c for c in elem]
        if not children:
            elem.clear()
            continue
        obj = children[0]
        obj_type = local(obj)
        obj_id = text_of(obj, "ID")

        if obj_type == "Category":
            label = localized(obj, "Label") or localized(obj, "CategoryName") or ""
            if obj_id:
                categories_map[obj_id] = label

        # Libération mémoire (standard lxml iterparse)
        elem.clear()
        parent = elem.getparent()
        if parent is not None:
            while elem.getprevious() is not None:
                del parent[0]

print(f"  Fragments lus         : {frag_type_counts.get('Fragment', 0):,}")
print(f"  Catégories indexées   : {len(categories_map):,}")
print(f"  Types d'éléments top 10:")
for tn, tc in frag_type_counts.most_common(10):
    print(f"    {tn:30s} {tc:>8,}")


# %%
# --- Pass 2 — Extraction des CodeLists ---------------------------------------
# Deuxième stream : on extrait chaque CodeList et on résout les labels de ses
# codes via l'index construit au passage 1.

code_lists = []
cl_counts = Counter()

print("\nPass 2 : extraction des CodeLists…")
fh_xml.seek(0)
for event, elem in etree.iterparse(fh_xml, events=("end",), tag=("*"), recover=True, huge_tree=True):
    if local(elem) == "Fragment":
        children = [c for c in elem]
        if not children:
            elem.clear()
            continue
        obj = children[0]

        if local(obj) == "CodeList":
            cl = {
                "id": text_of(obj, "ID"),
                "name": localized(obj, "CodeListName") or localized(obj, "Label") or "",
                "version": text_of(obj, "Version"),
                "codes": [],
            }

            # Parcourir tous les <Code> descendants (pas seulement enfants directs)
            for code_elem in obj.iter():
                if local(code_elem) == "Code":
                    value = text_of(code_elem, "Value")
                    ref = child(code_elem, "CategoryReference")
                    ref_id = text_of(ref, "ID") if ref is not None else ""
                    ref_label = categories_map.get(ref_id, "")
                    cl["codes"].append({"value": value, "category_label": ref_label})

            code_lists.append(cl)
            cl_counts[len(cl["codes"])] += 1

        # Libération mémoire
        elem.clear()
        parent = elem.getparent()
        if parent is not None:
            while elem.getprevious() is not None:
                del parent[0]

print(f"  CodeLists extraites   : {len(code_lists):,}")
print(f"  Codes totaux          : {sum(len(cl['codes']) for cl in code_lists):,}")


# %%
# --- Stats & échantillons ---------------------------------------------------
all_code_counts = [len(cl["codes"]) for cl in code_lists]

if all_code_counts:
    all_code_counts_sorted = sorted(all_code_counts)
    n = len(all_code_counts_sorted)

    def pct(val):
        """Retourne le centile donné (0–100) avec interpolation linéaire."""
        idx = val / 100 * (n - 1)
        lo = int(idx)
        hi = min(lo + 1, n - 1)
        frac = idx - lo
        return all_code_counts_sorted[lo] * (1 - frac) + all_code_counts_sorted[hi] * frac

    unique_codes_global = set()
    for cl in code_lists:
        for c in cl["codes"]:
            unique_codes_global.add(c["value"])

    print(f"\n=== Statistiques détaillées ===")
    print(f"  CodeLists            : {n}")
    print(f"  Total codes          : {sum(all_code_counts):,}")
    print(f"  Codes uniques        : {len(unique_codes_global):,}")
    print(f"  Min codes/liste      : {min(all_code_counts):,}")
    print(f"  Max codes/liste      : {max(all_code_counts):,}")
    print(f"  Mean codes/liste     : {sum(all_code_counts) / n:.0f}")
    print(f"  Médiane              : {pct(50):,.0f}")
    print(f"  25è centile          : {pct(25):,.0f}")
    print(f"  75è centile          : {pct(75):,.0f}")
    print(f"  95è centile          : {pct(95):,.0f}")

    # Distribution par taille
    buckets = {
        "1-10": 0,
        "11-50": 0,
        "51-100": 0,
        "101-500": 0,
        "501-1000": 0,
        "1001-5000": 0,
        "5000+": 0,
    }
    for cnt in all_code_counts:
        if cnt <= 10:
            buckets["1-10"] += 1
        elif cnt <= 50:
            buckets["11-50"] += 1
        elif cnt <= 100:
            buckets["51-100"] += 1
        elif cnt <= 500:
            buckets["101-500"] += 1
        elif cnt <= 1000:
            buckets["501-1000"] += 1
        elif cnt <= 5000:
            buckets["1001-5000"] += 1
        else:
            buckets["5000+"] += 1

    print(f"\n=== Distribution du nombre de codes par liste ===")
    for label, count in buckets.items():
        bar = "█" * count
        print(f"  {label:>10} : {count:>4,}  {bar}")

    # Top 10 des listes par nombre de codes
    top10 = sorted(code_lists, key=lambda c: len(c["codes"]), reverse=True)[:10]
    print(f"\n=== Top 10 des listes de codes ===")
    for i, cl in enumerate(top10, 1):
        print(f"  {i:2d}. {cl['id']:30s} | {cl['name'][:35]:35s} | {len(cl['codes']):6,} codes")

    # Échantillon pour vérifier la résolution des labels
    sample = code_lists[0]
    print(f"\n--- Échantillon: {sample['name'] or sample['id']} ({len(sample['codes'])} codes) ---")
    for code in sample["codes"][:10]:
        lbl = f" — {code['category_label']}" if code["category_label"] else ""
        print(f"    {code['value']:10s} {lbl}")

else:
    print("Aucune CodeList trouvée.")


# %%
# --- Export JSON ------------------------------------------------------------
# Construire un résumé compact et le sauvegarder sur S3.

export_data = {
    "source": SOURCE,
    "fragments_stats": dict(frag_type_counts),
    "summary": {
        "code_lists": len(code_lists),
        "total_codes": sum(all_code_counts) if all_code_counts else 0,
        "unique_codes_global": len(unique_codes_global),
    },
    "stats": {
        "min_codes": min(all_code_counts) if all_code_counts else 0,
        "max_codes": max(all_code_counts) if all_code_counts else 0,
        "mean_codes": sum(all_code_counts) / len(all_code_counts) if all_code_counts else 0,
        "median_codes": pct(50) if all_code_counts else 0,
    },
    "distribution": buckets,
    "code_lists_sample": [],
}

# Échantillonner ~20 CodeLists pour inspecter la qualité
for cl in code_lists[:20]:
    export_data["code_lists_sample"].append({
        "id": cl["id"],
        "name": cl["name"],
        "codes_count": len(cl["codes"]),
        "codes_preview": [{"value": c["value"], "category_label": c["category_label"]}
                          for c in cl["codes"][:5]],
    })

print(f"\n=== Résumé d'export ===")
for k, v in export_data["summary"].items():
    print(f"  {k}: {v}")
for k, v in export_data["stats"].items():
    print(f"  {k}: {v}")

# Sauvegarder sur S3 (dossier diffusion/ pour partage, sinon fichier technique)
if OUTPUT:
    blob = json.dumps(export_data, indent=2, ensure_ascii=False).encode("utf-8")
    with fs.open(OUTPUT, "wb") as f:
        f.write(blob)
    print(f"\n💾 Export JSON sauvegardé sur S3 → {OUTPUT}")
else:
    print("\nOUTPUT non défini → export sauté.")

# %%
