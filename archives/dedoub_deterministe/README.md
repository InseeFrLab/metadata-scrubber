# Alignement DDI ↔ SKOS & Dé-doublonnage Déterministe

Pipeline d'alignement automatique des variables DDI (Data Documentation Initiative) avec les concepts SKOS de l'INSEE, et détection déterministe de doublons parmi les Variables et CodeLists.

## Vue d'ensemble du processus

```text
Fichiers DDI XML (.xml)  →  Extraction objets DDI  →  Indexé par ID
                      ↘
Fichier SKOS JSON (.json) → Chargement/construction concepts SKOS  →  Dict {uri → {label, definition}}
                      ↘
              [ÉTAPE 1] Alignement Variables ↔ Concepts
              Pour chaque variable DDI (Variable / RepresentedVariable) :
                → Concatène label + description
                → Pour chaque concept SKOS : calcule score pondéré
                  score = 0.8 × similarité_label + 0.2 × similarité_definition
                → Filtre bruit (< 0.02)
                → Garde le meilleur concept par variable
                → Si score ≥ 0.60 → alignement accepté
                → Sinon → rejeté
              → Écrit align_debug.md + rml_variable_concept.ttl
                      ↘
              [ÉTAPE 2] Détection Doublons Variables
              Pour chaque paire de variables :
                → Concatène VariableName + Label + Description
                → Compare avec SequenceMatcher (seuil ≥ 0.90)
              → Écrit rml_variable_duplicates.ttl
                      ↘
              [ÉTAPE 3] Détection Doublons CodeLists
              Pour chaque paire de CodeLists :
                → Concatène CodeListName + Label + Codes + Categories
                → Compare avec SequenceMatcher (seuil ≥ 0.90)
              → Écrit rml_codelists_duplicates.ttl
```

## 1. Chargement des Données

### Fichiers DDI (XML)

**Fichiers sources (namespace 3.3)**

| Fichier | Description |
|---------|-------------|
| `FQPDDI_out.xml` | Enquête sur la formation professionnelle des adultes |
| `RPEDDI_out.xml` | Données sur l'emploi |
| `RSLDDI_out.xml` | Enquête sur les qualifications (par défaut) |

**Espaces de noms**

| Préfixe | Namespace |
|---------|-----------|
| `ddi` | `ddi:instance:3_3` |
| `lp` | `ddi:logicalproduct:3_3` |
| `r` | `ddi:reusable:3_3` |

**Fonction `load_ddi(files)`** :

1. Parse chaque fichier XML avec `xml.etree.ElementTree`
2. Parcourt chaque `<Fragment>` → extrait chaque enfant
3. Indexe tous les objets DDI par leur `r:ID` dans un dictionnaire `{id → élément}`
4. Retourne les racines + l'index

### Concepts SKOS (JSON)

**Fichier source** : `skos_definition.json` (dump RDF JSON de l'INSEE)

**Fonction `load_concepts(definition_file)`** en 3 passes :

1. **Labels** : parcourt les entrées avec `skos:prefLabel`, garde le label en français (`lang="fr"`)
2. **Définitions** : parcourt les entrées avec `xkos:plainText`, applique les filtres :
   - Exclut les versions avec `base#validUntil` (non courantes)
   - Exige `dc:language = "fr"`
   - Garde la version `pav:version` la plus élevée
   - Extrait l'URI concept depuis l'URI définition (`.../c1223/definition/v1/fr` → `.../c1223/definition`)
3. **Fusion** : joint labels et définitions par URI concept

Résultat : dictionnaire `{concept_uri → {"label": str, "definition": str}}`

## 2. Alignement Variables ↔ Concepts

### Méthode de Similarité

```
score = (0.8 × similarité_label) + (0.2 × similarité_definition)
```

**Normalisation** (fonction `normalize(txt)`) :

```python
txt → .lower()
    → re.sub(r"[^\w\s]", " ", txt)   # garde alphanumérique + espaces
    → re.sub(r"\s+", " ", txt)       # réduit espaces multiples
    → .strip()
```

**Calcul** (fonction `text_similarity(a, b)`) :
- Utilise `difflib.SequenceMatcher.ratio()`
- Retourne 0.0 si texte vide

### Pipeline d'Alignement

Pour chaque objet DDI de type `Variable` ou `RepresentedVariable` :

1. Extrait `Label` et `Description`
2. Si les deux sont vides → marqué comme « sans texte exploitable »
3. Pour chaque concept SKOS :
   - `s_label` = similarité entre label DDI et `prefLabel` du concept
   - `s_def` = similarité entre description DDI et définition du concept
   - `score` = 0.8 × s_label + 0.2 × s_def
4. Filtrage : seules les paires avec score > 0.02 sont conservées (évite le bruit)
5. Le meilleur concept pour la variable est retenu
6. Classement :
   - score ≥ 0.60 → **alignement accepté**
   - score < 0.60 → **rejeté** (liste des 50 meilleurs rejetés dans le debug)

### Configuration

| Constante | Valeur | Rôle |
|-----------|--------|------|
| `SIM_THRESHOLD_CONCEPT` | `0.60` | Seuil minimum d'acceptation |
| `SIM_THRESHOLD_DUPLICATE` | `0.90` | Seuil pour les doublons |
| `WEIGHTS["label"]` | `0.8` | Poids du label dans le score |
| `WEIGHTS["definition"]` | `0.2` | Poids de la définition dans le score |
| Seuil bruit | `0.02` | Score minimum pour être évalué |

### Statistiques (exécution `RSLDDI_out.xml`)

| Métrique | Valeur |
|----------|--------|
| Objets DDI analysés | 664 |
| Concepts SKOS chargés | 1237 |
| Variables analysées | 267 |
| **Alignements retenus (≥ 0.60)** | **4** |
| Variables rejetées (< 0.60) | 260 |
| Variables sans texte exploitable | 3 |

### Alignements Retenus

| ID DDI | Type | Label DDI | Concept INSEE | Score |
|--------|------|-----------|---------------|-------|
| `448946b6...` | RepresentedVariable | Année de naissance | Rang de naissance (`c1223`) | **0.687** |
| `dbbd7aab...` | RepresentedVariable | Code département | Département (`c1762`) | **0.654** |
| `b09b950f...` | RepresentedVariable | Type de personne morale | Personne morale (`c1251`) | **0.633** |
| `fa02a2c5...` | RepresentedVariable | Type de ménage fiscal | Ménage fiscal (`c1063`) | **0.612** |

### Variables Rejetées (top 5)

| ID DDI | Type | Score |
|--------|------|-------|
| `fe70be4c...` | RepresentedVariable | 0.599 |
| `96766275...` | RepresentedVariable | 0.585 |
| `e334c4e2...` | RepresentedVariable | 0.585 |
| `7ad16e62...` | RepresentedVariable | 0.584 |
| `7f63f57a...` | RepresentedVariable | 0.581 |

Scores complets de 0.599 à 0.481.

### Variables Sans Texte

| ID DDI |
|--------|
| `b3ff78c1-9b1e-4ac2-ac6a-d86d2a991d3d` |
| `e235cd7e-4f94-4e56-bc42-dda844ad1538` |
| `8d5e031f-492f-4229-a599-f4633fd84cf6` |

## 3. Détection de Doublons

### Principe

Comparaison par paires exhaustives (`itertools.combinations`). Concaténation du texte disponible, puis calcul de similarité par `SequenceMatcher.ratio()`. Seuil de décision : **0.90**.

### Doublons Variables

**Textes comparés** : `VariableName + " " + Label + " " + Description`

**Retour** : liste de tuples `(id_a, id_b, score, uri_a, uri_b, label_a, label_b)`

**Résultat** : ~2 322 paires détectées (fichier de 579 006 lignes)

Exemples :

| Paire | Score |
|-------|-------|
| `Libellé act. éco. employeur avant 1998` ↔ `Libellé act. éco. employeur en 1998` | **0.946** |
| `Dép. employeurdern.emploi>98 (code)` ↔ `Dép. employeur dern. emploi >98 (libellé)` | **0.913** |

### Doublons CodeLists

**Textes comparés** : `CodeListName + " " + Label + " " + Codes + " " + CategoryName + " " + CategoryLabel`

Les codes incluent leur `r:Value` et les catégories liées (nom + label) via `r:CategoryReference`.

**Retour** : liste de tuples `(id_a, id_b, score, uri_a, uri_b, label_a, label_b)`

**Résultat** : ~1 958 paires détectées (fichier de 78 326 lignes)

Des paires avec score **1.000** (identiques) ont été détectées.

## 4. Format des Fichiers de Sortie

### 4.1 `align_debug.md`

Rapport humain-lisible au format Markdown. Contient :

- **Résumé** : nombre d'objets, concepts, seuil
- **Variables sans texte** : IDs des variables sans label ni description
- **Alignements retenus** : pour chaque alignment accepté
  - ID DDI
  - Type (Variable / RepresentedVariable)
  - Label DDI
  - Concept SKOS (label INSEE + URI)
  - Score (3 décimales)
- **Variables rejetées** : top 50 par score décroissant (ID + score)
- **Statistiques finales** : compteurs acceptés / rejetés / sans texte

**Structure** :

```markdown
# 🔗 Alignement Variables ↔ Concepts

## 📊 Résumé
- Objets DDI analysés : 664
- Concepts : 1237
- Seuil : 0.6

## ⚠️ Variables sans texte exploitable
- `b3ff78c1-...`
...

## ⭐ Alignements retenus
### 🧩 `{id}` ({type})
- 🏷️ **Label DDI** : *{label}*
- 🧠 **Concept** : {label_concept}
- 📈 **Score** : **{score:.3f}**
...

## ❌ Variables rejetées (meilleur score)
- 🔸 `{id}` ({type}) → {score:.3f}
...

## 📌 Statistiques finales
- Variables analysées : 267
- Alignements retenus : 4
- Rejetées : 260
```

### 4.2 `rml_variable_concept.ttl` — Alignements

**Type** : Turtle (`.ttl`), mapping R2RML

**Préfixes utilisés** :

| Préfixe | IRI |
|---------|-----|
| `rr` | `http://www.w3.org/ns/r2rml#` |
| `skos` | `http://www.w3.org/2004/02/skos/core#` |
| `owl` | `http://www.w3.org/2002/07/owl#` |
| `xsd` | `http://www.w3.org/2001/XMLSchema#` |
| `ex` | `http://example.org/ddi-align/` |

**Structure d'un alignement** :

```turtle
ex:Align{num}
  a rr:TriplesMap ;
  rr:subjectMap [ rr:constant <urn:ddi:{Type}:{id}> ] ;
  rr:predicateObjectMap [
    rr:predicate skos:closeMatch ;
    rr:object <{URI_concept_SKOS}>
  ] ;
  rr:predicateObjectMap [
    rr:predicate ex:confidence ;
    rr:objectMap [
      rr:constant "{score:.3f}"^^xsd:decimal
    ]
  ] .
```

**Détail des composants** :

| Composant | Valeur | Type | Signification |
|-----------|--------|------|---------------|
| Nom du `TriplesMap` | `ex:Align{N}` | URI | Identifiant unique, incrémental |
| `rr:subjectMap` | `<urn:ddi:{Type}:{id}>` | URI constante | Identifiant de la variable DDI |
| `rr:predicate` (1) | `skos:closeMatch` | URI | Relation sémantique (alignement SKOS) |
| `rr:object` (1) | `<http://id.insee.fr/concepts/definition/{code}>` | URI | URI du concept INSEE aligné |
| `rr:predicate` (2) | `ex:confidence` | URI | Attribut personnalisé pour le score |
| `rr:object` (2) | `"0.xxx"^^xsd:decimal` | Literal | Score d'alignement (3 décimales) |

**Exemple concret (Align1)** :

```turtle
ex:Align1
  a rr:TriplesMap ;
  rr:subjectMap [ rr:constant <urn:ddi:RepresentedVariable:448946b6-f199-42b4-a6a1-207948b66854> ] ;
  rr:predicateObjectMap [
    rr:predicate skos:closeMatch ;
    rr:object <http://id.insee.fr/concepts/definition/c1223>
  ] ;
  rr:predicateObjectMap [
    rr:predicate ex:confidence ;
    rr:objectMap [
      rr:constant "0.687"^^xsd:decimal
    ]
  ] .
```

**Fichier total** : 62 lignes, 4 `TriplesMap`.

### 4.3 `rml_variable_duplicates.ttl` — Doublons Variables

**Structure d'un doublon** :

```turtle
ex:VariableDup{num}
  a rr:TriplesMap ;
  rr:subjectMap [ rr:constant <{URI_VAR_A}> ] ;
  rr:predicateObjectMap [
    rr:predicate owl:sameAs ;
    rr:object <{URI_VAR_B}>
  ] ;
  rr:predicateObjectMap [
    rr:predicate ex:similarityScore ;
    rr:objectMap [
      rr:constant "{score:.3f}"^^xsd:decimal
    ]
  ] ;
  rr:predicateObjectMap [
    rr:predicate rdfs:label ;
    rr:objectMap [
      rr:constant "{label_a} ↔ {label_b}"
    ]
  ] .
```

**Détail des composants** :

| Composant | Valeur | Type | Signification |
|-----------|--------|------|---------------|
| `rr:subjectMap` | `{URI_VAR_A}` | URI | Première variable de la paire |
| `rr:predicate` | `owl:sameAs` | URI | Relation d'équivalence (doublon potentiel) |
| `rr:object` | `{URI_VAR_B}` | URI | Seconde variable de la paire |
| `ex:similarityScore` | `"0.xxx"^^xsd:decimal` | Literal | Score de similarité (3 décimales) |
| `rdfs:label` | `"{label_a} ↔ {label_b}"` | Literal | Description textuelle de la paire |

**Fichier total** : 579 006 lignes (~2 322 entrées).

> ⚠️ **Préfixe `rdfs` non déclaré** : le prédicat `rdfs:label` est généré ici (et dans les doublons CodeLists) mais le préfixe `rdfs` est **absent du `RML_HEADER`**. Les fichiers de doublons ne sont donc pas du Turtle valide en l'état. Voir [Limitations connues](#limitations-connues).

> ⚠️ **URIs vides** : certaines URIs peuvent être vides (`<>`) si la variable n'avait pas d'élément `r:URN` dans le DDI, ce qui produit des triplets sujet/objet invalides.

### 4.4 `rml_codelists_duplicates.ttl` — Doublons CodeLists

**Même structure que les variables, avec 2 différences** :

| Champ | Variable | CodeList |
|-------|----------|----------|
| Prédicat de relation | `owl:sameAs` | `skos:closeMatch` |
| Nom du TriplesMap | `ex:VariableDup{N}` | `ex:CodeListDup{N}` |

**Prédicat `skos:closeMatch`** : utilisé car les CodeLists sont dans le vocabulaire SKOS.

> ⚠️ Comme les doublons Variables, ce fichier génère `rdfs:label` sans déclarer le préfixe `rdfs`. Voir [Limitations connues](#limitations-connues).

**Fichier total** : 78 326 lignes (~1 958 entrées).

## 5. Structures de Données Internes

### Extraction DDI

Chaque objet DDI est indexé par son `r:ID`. Les champs extraits :

| Objet | Fichier RML | Champs extraits |
|-------|-------------|-----------------|
| `Variable` / `RepresentedVariable` | duplicates | `r:URN`, `VariableName/r:String`, `r:Label/r:Content`, `r:Description/r:Content` |
| `CodeList` | codelists duplicates | `r:URN`, `CodeListName/r:String`, `r:Label/r:Content`, `lp:Code` (chacun avec `r:Value`) |
| `Category` (lié via `r:CategoryReference`) | codelists duplicates | `r:ID`, `CategoryName/r:String`, `r:Label/r:Content` |

### Structure intermédiaire Variables (used for dedup)

```python
{
    "id": "<r:ID>",
    "uri": "<r:URN>",
    "name": "<VariableName>",
    "label": "<Label>",
    "description": "<Description>"
}
```

### Structure intermédiaire CodeList (used for dedup)

```python
{
    "id": "<r:ID>",
    "uri": "<r:URN>",
    "name": "<CodeListName>",
    "label": "<Label>",
    "codes": [
        {
            "value": "<r:Value>",
            "category": {
                "id": "<r:ID>",
                "name": "<CategoryName>",
                "label": "<Label>"
            }
        }
    ]
}
```

### Structure intermédiaire Concept SKOS

```python
{
    "http://id.insee.fr/concepts/definition/c1223": {
        "label": "Rang de naissance",
        "definition": "Texte de la définition en français..."
    }
}
```

### Structure des Alignements

```python
[
    {
        "id": "448946b6-f199-42b4-a6a1-207948b66854",
        "label": "Année de naissance",
        "type": "RepresentedVariable",
        "concept": "http://id.insee.fr/concepts/definition/c1223",
        "score": 0.687
    },
    ...
]
```

### Structure des Doublons

```python
(
    "id_a",       # r:ID de la première entité
    "id_b",       # r:ID de la deuxième entité
    0.946,        # score de similarité
    "uri_a",      # r:URN de la première entité (peut être "")
    "uri_b",      # r:URN de la deuxième entité (peut être "")
    "label_a",    # label de la première entité
    "label_b"     # label de la deuxième entité
)
```

## 6. Éléments du Code

`concepts_variables_alignment.py` (690 lignes, stdlib uniquement)

| Fonction | Lignes | Rôle |
|----------|--------|------|
| `normalize(txt)` | 57-63 | Normalisation texte (minuscule, suppression non-alphanumérique) |
| `text_similarity(a, b)` | 66-69 | `SequenceMatcher.ratio()` sur texte normalisé |
| `jaccard(a, b)` | 72-77 | Similarité Jaccard (définie mais non utilisée par défaut) |
| `load_ddi(files)` | 84-102 | Parse XML DDI, indexe objets par ID |
| `extract_urn(el)` | 109-111 | Extrait `<r:URN>` |
| `extract_label(el)` | 114-116 | Extrait `<r:Label/r:Content>` |
| `extract_variable_name(el)` | 135-142 | Extrait `<VariableName/r:String>` ou `<RepresentedVariableName/r:String>` |
| `extract_description(el)` | 145-147 | Extrait `<r:Description/r:Content>` |
| `extract_codelist_name(el)` | 119-124 | Extrait `<CodeListName/r:String>` |
| `extract_category_name(el)` | 127-132 | Extrait `<CategoryName/r:String>` |
| `extract_codelist_ref(el)` | 150-152 | Extrait `<r:CodeListReference/r:ID>` |
| `extract_codelist_values(...)` | 155-161 | Extrait tous les `r:Value` des codes d'une CodeList |
| `extract_pref_label_fr(props)` | 170-177 | Extrait le `prefLabel` en français d'un concept |
| `extract_concept_uri_from_definition(...)` | 180-184 | Convertit URI définition → URI concept |
| `load_concepts(file)` | 187-271 | Chargement 3 passes : labels, définitions, fusion |
| `align_objects(objects, concepts)` | 279-382 | Pipeline alignement + écriture debug + retour matches |
| `concat_codelist_text(cl)` | 392-422 | Concatène tous les champs textuels d'une CodeList pour scoring |
| `concat_variable_text(var)` | 427-443 | Concatène name + label + description pour scoring |
| `detect_codelist_duplicates(cls)` | 448-473 | Comparaison par paires seuil 0.90 |
| `detect_variable_duplicates(vars)` | 478-504 | Comparaison par paires seuil 0.90 |
| `write_rml_align(matches, file)` | 525-543 | Génère RML alignements |
| `rml_var_duplicates(dups, type, file)` | 550-581 | Génère RML doublons variables |
| `rml_codelist_duplicates(dups, type, file)` | 584-615 | Génère RML doublons codelists |

## 7. Exécution

```bash
python concepts_variables_alignment.py
```

Lecture par défaut : `RSLDDI_out.xml` + `skos_definition.json`
Génère : `rml_variable_concept.ttl`, `rml_variable_duplicates.ttl`, `rml_codelists_duplicates.ttl`, `align_debug.md`

Pour inclure tous les fichiers DDI, modifier la liste `ddi_files` dans le `__main__` :

```python
ddi_files = [
    "FQPDDI_out.xml",
    "RPEDDI_out.xml",
    "RSLDDI_out.xml"
]
```

## Limitations connues

| Limitation | Détail | Impact |
|------------|--------|--------|
| **Préfixe `rdfs` non déclaré** | `rml_var_duplicates` et `rml_codelist_duplicates` émettent `rdfs:label`, mais `RML_HEADER` ne déclare que `rr`, `skos`, `owl`, `xsd`, `ex`. | Les fichiers `rml_variable_duplicates.ttl` et `rml_codelists_duplicates.ttl` ne sont **pas du Turtle valide** tels quels (préfixe manquant : `@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .`). |
| **URIs vides** | Quand une entité DDI n'a pas de `r:URN`, le sujet et/ou l'objet sont générés comme `<>`. | Triplets RDF invalides ou sans cible exploitable. |
| **Comparaison en O(n²)** | Les doublons sont détectés par paires exhaustives (`itertools.combinations`) sans pré-filtrage ni blocking. | Volumétrie de sortie très élevée (ex. 579 006 lignes / ~13 Mo pour les variables) et temps de calcul croissant avec le nombre d'objets. |
| **Similarité purement lexicale** | `difflib.SequenceMatcher` compare des chaînes de caractères, pas du sens. | Faux positifs sur des libellés visuellement proches (ex. « …avant 1998 » ↔ « …en 1998 »), faux négatifs sur des synonymes ou reformulations. |
