# metadata-scrubber

**Meta Scrubber — Pipeline de dédoublonnage des CodeLists dans les métadonnées DDI 3.3/6.0.**

Ce projet identifie les redondances entre listes de codes d'un référentiel
statistique DDI, produit un registre structuré (`codelist_duplicates.json`),
et offre une interface de validation interactive (API REST + HTML/JS) pour
l'expert métier.

## Installation & lancement

```bash
# Avec uv (recommandé sur Onyxia/SSP Cloud)
uv sync
```

### Lancer le serveur API (Frontend + API)

```bash
# Dépendances de l'application (FastAPI)
uv run scrubber_app/server.py
```

Le serveur démarre par défaut sur `http://localhost:8000`.

- **Interface web** : ouvrez `http://localhost:8000` dans un navigateur.
- **API REST** : `/api/pipeline`, `/api/registry`, … — consultez la page
  d'accueil pour le swagger automatique.

### Lancer uniquement le pipeline (CLI)

```bash
# Dependencies du pipeline uniquement
uv run -- python main.py
```

### Interface Streamlit (fallback)

```bash
# Dependencies de l'application Streamlit
uv run -A streamlit run scrubber_app/app.py
```

## Architecture

Nouvelle architecture **FastAPI + HTML/JS + SSE** (déploiement monolithique —
backend API et frontend dans un même processus, idéal pour Onyxia).

```
┌──────────────────────────────────────────────────────┐
│  scrubber_app/server.py  — FastAPI entry point       │
│  • SSE streams pour progression temps réel           │
│  • REST API pour registry (CRUD decisions)           │
│  • Templates Jinja2 + static CSS/JS                   │
└──────────────────────────────────────────────────────┘
             │
    ┌────────┼────────┐
    ▼        ▼        ▼
 scrubber_app/    scrubber_app/   scrubber_app/
 services/       templates/     static/
 ┌───────────┐  ┌──────────┐  ┌──────────┐
 │job│        │  │index.html│  │ css/     │
 │pipeline│   │  │          │  │ js/      │
 │registry│   │  │          │  │ main.js  │
 │upload│     │  │          │  │ pipeline │
 └───────────┘  └──────────┘  │ registry │
                              │ main.js  │
                              └──────────┘
             │
    ┌────────┼────────┐
    ▼        ▼        ▼
┌──────────┐ ┌───────┐ ┌──────────────┐
│main.py   │ │src/   │ │scrubber_app/ │
│(pipeline │ │scrub- │ │apps.py       │
│orchestr.│ │ber/   │ │(Streamlit    │
│         │ │core    │ │ fallback)   │
└──────────┘ └───────┘ └──────────────┘
```

## Architecture du pipeline

```
source DDI XML (S3 / local)
        │
        ▼
┌────────────────────────────────────────────────────────┐
│  src/scrubber/extractor.py  — Parsing XML DDI          │
│  • CodeList (ID, Name, Label, resolved Codes)          │
│  • Category (ID → label dictionary)                    │
│  • Variable / RepresentedVariable → CodeListReference  │
│  • Support DDI 3.3 : VariableRepresentation → CodeRep │
│  • 2 passes : catégories indexées avant les codes      │
└────────────────────────────────────────────────────────┘
        │
        ▼
┌────────────────────────────────────────────────────────┐
│  src/scrubber/funnel.py  — Entonnoir de détection       │
│  Phase 1 — Exact  : regroupement par signature de      │
│                  contenu (paires triées valeur,label)    │
│  Phase 1 bis — Fuzzy : code_sim × 0.6 + name_sim × 0.4 │
│                  (seuil ≥ 0.90 détecté, ≥ 0.80 inspect) │
│  Boost : code_sim ≥ 0.95 + noms différents → suspect   │
└────────────────────────────────────────────────────────┘
        │
        ▼
┌────────────────────────────────────────────────────────┐
│  src/scrubber/semantic.py  — Détection sémantique      │
│  Phase 2a — Embeddings directs : nom + label + codes   │
│                  → cosinus ≥ 0.90 avec qwen3-embed-8b   │
│  Phase 2b — Embeddings variables : nom + label + noms  │
│                  des variables → cosinus ≥ 0.92          │
│  Juge LLM : gemma4-26b-moe validation paire par paire  │
│            JSON {meme_concept, confiance, raison}        │
└────────────────────────────────────────────────────────┘
        │
        ▼
┌────────────────────────────────────────────────────────┐
│  src/scrubber/signals.py  — Signaux d'usage            │
│  • var_sig : tuple trié des variables référentes       │
│  • usage_groups : même contextes → mêmes variables     │
│  • cross_check : same / partial / disjoint usages      │
└────────────────────────────────────────────────────────┘
        │
        ▼
┌────────────────────────────────────────────────────────┐
│  src/scrubber/reporting/duplicates_registry.py         │
│  • `codelist_duplicates.json` — registre unique        │
│    {cl_id → {id, name, label, codes, vars,              │
│              cat_ids, duplicates: [{...}]}}              │
└────────────────────────────────────────────────────────┘
        │
        ▼
┌────────────────────────────────────────────────────────┐
│  scrubber_app/app.py  — Interface Streamlit            │
│  • Onglet Pipeline : lancement complet (S3/local)      │
│  • Onglet Validation : cartes par CodeList, décide      │
│    approve/reject/pending, comparatif de codes, auto-   │
│   save, actions globales (approuver exactes, ≥ 0.95…)   │
└────────────────────────────────────────────────────────┘
```

## Installation

```bash
# Avec uv (recommandé sur Onyxia/SSP Cloud)
uv sync

# Dependencies du pipeline uniquement
uv run -- python main.py

# Dependencies de l'application Streamlit
uv run -A streamlit run scrubber_app/app.py
```

**Dépendances** : `lxml`, `s3fs`, `openai` (embeddings + LLM judge),
`numpy`, `pandas`, `pyarrow`.
**Développement** : `pytest`, `ruff`.
**Application** : `streamlit` (groupe `app`).

## Utilisation

### Pipeline CLI

```bash
# Pipeline complet (Toutes détections, LLM inclus)
python main.py

# Fichier personnalisé
python main.py /path/to/your.xml

# Sans phase LLM (plus rapide, pour prototypage)
uv run -- python main.py --no-llm

# Dossier de sortie personnalisé
uv run -- python main.py my_input.xml --audit-dir audit_custom/

# Mode verbeux (détails LLM/embeddings)
uv run -- python main.py --verbose
```

**Configuration LLM** (`--run-llm` activé par défaut) :

| Variable | Rôle |
|---|---|
| `OPENAI_BASE_URL` | Endpoint OpenAI-compatible (ex. LLM auto-hébergé) |
| `OPENAI_API_KEY` | Clé d'API pour les appels embeddings + LLM judge |

### Interface Streamlit

```bash
# Lancer l'application de validation
uv run -A streamlit run scrubber_app/app.py
```

Deux onglets :

1. **🚀 Pipeline** — saisir le chemin XML (S3 ou local), configurer les
   paramètres (run_llm, verbose), exécuter le pipeline et visualiser le résultat.
2. **🔍 Validation des doublons** — charger `codelist_duplicates.json`,
   valider chaque `duplicate` (approve / reject / pending), comparer les codes
   côte à côte, actions globales (approuver toutes les exactes, filtre par
   confiance ≥ 0.95), auto-save sur fichier.

### Registre de sortie : `codelist_duplicates.json`

Le pipeline écrit un **seul fichier JSON** dans le dossier d'audit :

```json
{
  "cl-id-uuid-1": {
    "id": "cl-id-uuid-1",
    "name": "N_domempl_23",
    "label": "Domaine d'emploi 2023",
    "codes_count": 10,
    "codes": [["1","Fonction publique d'État"],["2","Fonction publique territoriale"],...],
    "cat_ids": ["a9116bf7", "863966af", ...],
    "vars": ["domempl", "domempl_empl"],
    "duplicates": [
      {
        "id": "d6e60a74-...",
        "name": "DOMEMPL",
        "label": "",
        "codes_count": 10,
        "codes": [...],
        "cat_ids": [...],
        "vars": ["domempl", "domempl_empl", "DOMEMPL", "DOMEMPL_EMPL"],
        "detection_types": ["exact", "semantic_list"],
        "confidence": 1.0,
        "decision": "pending"
      }
    ]
  }
}
```

## Workflow de validation

1. **Exécuter le pipeline** : `uv run -- python main.py` (ou via onglet Streamlit).
2. **Consulter le registre** : `audit/codelist_duplicates.json`.
3. **Valider dans Streamlit** : `uv run -A streamlit run scrubber_app/app.py`,
   aller dans l'onglet Validation, examiner les cartes par CodeList, approuver /
   rejeter chaque duplicate.
4. **Auto-save** : les décisions sont sauvegardées directement dans le fichier
   `codelist_duplicates.json` chargé (local ou S3).

## Statistiques BTS.xml

| Métrique | Valeur |
|---|---|
| CodeLists extraites | 156 |
| Codes totaux | 998 |
| Catégories indexées | 831 (+ 1 orpheline) |
| Références Variable → CodeList | 503 |
| CodeLists liées à des variables | 156 / 156 (100 %) |
| Groupes de doublons exacts | 29 (54 redondantes) |
| Signatures uniques | 102 |

> Voir la [note méthodologique](doc/note_methodologique_dedoublonnage.qmd) pour le
> détail complet des méthodes et des exemples.

## Structure du projet

```
metadata-scrubber/
├── main.py                          # Orchestrateur CLI du pipeline
├── src/scrubber/
│   ├── extractor.py                 # Parsing XML DDI (lxml)
│   ├── normalize.py                 # Normalisation texte, signatures codes
│   ├── funnel.py                    # Détection exacte + flou (hybride)
│   ├── semantic.py                  # Embeddings + juge LLM (2 phases)
│   ├── signals.py                   # var_sig, usage_groups, cross_check
│   ├── types.py                     # CodeList, CandidateFusion, VariableRef
│   └── reporting/
│       └── duplicates_registry.py   # Registre JSON unique par CodeList
├── scrubber_app/
│   ├── pipeline_runner.py           # Wrapper pipeline pour Streamlit
│   └── app.py                       # Interface Streamlit (2 onglets)
├── exploration/
│   └── explore_standalone.py        # Script exploratoire historique
├── tests/
│   └── test_scrubber.py             # Tests unitaires
├── audit/
│   └── codelist_duplicates.json     # Registre généré par le pipeline
├── doc/
│   └── note_methodologique_dedoublonnage.qmd
├── pyproject.toml                   # Config uv (pipeline + app + dev)
└── README.md                        # Ce fichier
```

## Tests

```bash
uv run pytest tests/test_scrubber.py -v
```
