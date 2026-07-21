# metadata-scrubber

**Meta Scrubber — Pipeline de dédoublonnage des CodeLists DDI 3.3/6.0.**

Identifie les redondances entre listes de codes d'un référentiel statistique DDI,
produit un registre structuré (`codelist_duplicates.json`), et offre une interface
de validation interactive (API REST + HTML/JS intégré) pour l'expert métier.

## Installation & lancement

```bash
# Préparer l'environnement (uv + lockfile)
uv sync
```

**Environnements requis :**

| Variable | Rôle |
|---|---|
| `OPENAI_BASE_URL` + `OPENAI_API_KEY` | Embeddings + juge LLM (phases sémantiques) |
| `AWS_S3_ENDPOINT` | Lecture/écriture S3/MinIO (S3/Onyxia/SSP Cloud) |

---

## Lancer le serveur (API + Frontend)

```bash
# Démarrer le serveur monolithique FastAPI
uv run scrubber-web
```

Le serveur écoute sur `http://localhost:8000`.

- **Frontend** : ouvrez `http://localhost:8000` — dashboard de validation avec cartes CodeLists, filtres et décisions batch.
- **Swagger** : `/docs` — documentation API automatique (OpenAPI).
- **SSE Streaming** : `/api/pipeline/{job_id}/sse` — progression en temps réel.

**Arrêter :** `Ctrl+C` ou `kill` le processus.

---

## Pipeline CLI

```bash
# Exécuter le pipeline complet (Toutes détections + LLM)
uv run scrubber

# Fichier personnalisé
uv run scrubber /path/to/input.xml

# Sans phase LLM (plus rapide, prototypage)
uv run scrubber --no-llm

# Dossier de sortie personnalisé
uv run scrubber input.xml --audit-dir ./audit

# Mode verbeux (détails LLM/embeddings)
uv run scrubber input.xml --verbose
```

**Arguments CLI :**

| Argument | Défaut | Rôle |
|---|---|---|
| `xml_source` | `s3://projet-metadonnees-rmes/BTS.xml` | Source DDI (locale ou S3) |
| `--audit-dir` | `s3://projet-metadonnees-rmes/scrubber_output` | Sortie `codelist_duplicates.json` |
| `--run-llm` | `True` | Activer embeddings + juge LLM |
| `--no-llm` | — | Sauter les phases sémantiques |
| `--verbose` | `False` | Afficher détails LLM/embeddings |
| `--registry` | `—` | Injecter un registre nettoyé (v. ci-dessous) |

---

## Architecture

```
scrubber-web  ─── FastAPI monolithique ────────────────────────────┐
  (uv run scrubber-web)              │                             │
                                     ▼                             │
                    ┌────────────────────────────────────────────┐  │
                    │ src/metadata_scrubber/ui/server.py          │  │
                    │  • SSE temps réel pour progression pipeline │──┘
                    │  • API REST (registry CRUD, pipeline, upload)
                    │  • Frontend HTML/JS inline (importlib_resources)
                    │  • API cleaned (CRUD registre nettoyé v2)
                    └────────────┬─────────────────────────────────┘
                                 │
              ┌──────────────────┼──────────────────┐
              ▼                  ▼                  ▼
    src/metadata_scrubber/   tests/         demo/
    ui/services/     ┌───────┴───────┐   BTS_demo.xml
    job_manager.py   │  main.py      │   BPE_demo.xml
    pipeline_service │  (orchestr.)  │   make_demo_files.py
    registry_service └───────┬───────┘
                             │
              ┌──────────────┴─────────────────┐
              ▼               ▼                  ▼
     ┌──────────────┐ ┌─────────────┐ ┌──────────────────┐
     │ extractor.py │ │ funnel.py   │ │ semantic.py      │
     │ • Parsing DDI│ │ • Exact     │ │ • Embeddings CL  │
     │ • 2 passes   │ │ • Fuzzy     │ │ • Embeddings var │
     │ • XPath XML  │ │             │ │ • Juge LLM       │
     └──────────────┘ └─────────────┘ └──────────────────┘
              │               │                  │
              ▼               ▼                  ▼
     ┌────────────────────────────────────────────────────┐
     │ signals.py  → var_sig, usage_groups, cross_check   │
     │ normalize.py → NFC, lowercase, signature_from_codes│
     │ types.py     → CodeList, CandidateFusion           │
     │ reporting/   → write_duplicates_registry (JSON)    │
     └────────────────────────────────────────────────────┘
```

### Pipeline interne (9 phases)

```
Source DDI XML (S3 / local)
        │
        ▼
[1/9] Lecture + parsing XML (lxml, namespaces ignorés)
        │
        ▼
[2/9] Extraction CodeLists + Catégories (2 passes)
        │
        ▼
[3/9] Extraction références Variables → CodeList
        │
        ▼
[4/9] Signature de contenu : trier paires (valeur,libellé)
        │
        ▼
[5/9] Détection exacte : regroupement par signature identique
        │
        ▼
[6/9] Détection floue : SequenceMatcher (≥ 0.90)
        │
        ▼
[7/9] Signaux d'usage : var_sig, usage_groups, cross_check
        │
        ▼
[8/9] Détection sémantique (facultative — --no-llm la saute)
  ┌─────┴─────┐
  ▼           ▼
Embeddings   Juge LLM
directs CL   {meme_concept, confiance, raison}
(≥ 0.90)
  ▼
Embeddings variables (≥ 0.92)
        │
        ▼
[9/9] Génération de codelist_duplicates.json
        │
        ▼
codelist_duplicates.json  (regroupe toutes les paires)
        │
        ▼
  synced → cleaned_codelists.json (via API /api/registry/save)
```

---

## Feature : `--registry` (dédoublonnage transversal itératif)

Le registre nettoyé (`cleaned_codelists.json`, schéma v2) est un **catalogue transversal
de vérité** qui s'incrémente opération par opération (BTS.xml → cleaned → BPE.xml →
cleaned → CPE.xml → …). Chaque fichier traité enrichit le registre, ce qui améliore la
détection sur les fichiers suivants, y compris les appariements **cross-opération**.

### 1. Flux transversal

```
Opération 1 : BTS_demo.xml (sans --registry)
  → codelist_duplicates.json → 4 paires détectées (exact+flou+sémantique)
  → Interface web : examiner / approuver / rejeter
  → /api/registry/save → cleaned_codelists.json (mères BTS)

Opération 2 : BPE_demo.xml avec --registry cleaned_codelists.json
  → masters BTS injectés dans la détection
  → CodeLists BTS doublons exclues (déjà validées)
  → codelist_duplicates.json → paires BPE intra + BPE↔BTS (cross-fichier)
  → /api/registry/save → cleaned_codelists.json mis à jour (BTS + BPE)

Opération N : fichier suivant avec --registry cleaned_codelists.json
  → registry = tous les masters validés précédemment
  → détection = intra-fichier + cross-opération sur les masters cumulés
  → registre s'enrichit, les paires résiduelles diminuent à chaque étape

Plus le registre grossit, plus la détection évite les réitérations
et capture les appariements inter-opérations.
```

### 2. Exemple rapide avec les données démo

```bash
# Opération 1 — BTS_demo : 4 paires
uv run scrubber demo/BTS_demo.xml         # → codelist_duplicates.json
# Interface web → approuver → /api/registry/save → cleaned_codelists.json

# Opération 2 — BPE_demo avec registre
uv run scrubber --registry cleaned_codelists.json demo/BPE_demo.xml
# → NAF_6_POSTES_BPE ↔ N_NAF_6          (exact, cross-fichier)
# → ETAT_UNITE_BPE ↔ N_etat_23          (fuzzy, cross-fichier)
# → L_VAR_SPECIF_2023 ↔ 2021            (intra-BPE)
# → L_AN_BPE_EVOL_2023 ↔ 2022           (intra-BPE)
# → registre enrichi BTS + BPE
```

### 3. Schéma `cleaned_codelists.json` (v2)

```json
{
  "version": 2,
  "generated_at": "2025-xx-xxTxx:xx:xx+00:00",
  "source_registry": "s3://.../codelist_duplicates.json",
  "codelists": {
    "uuid-master": {
      "id": "uuid-master",
      "name": "Nom_master",
      "label": "Libellé master",
      "codes": [["1","Valeur 1"],["2","Valeur 2"],...],
      "codes_count": 10,
      "vars": ["var1", "var2"],
      "replaces": [
        {
          "id": "uuid-dup",
          "name": "Nom_dup",
          "detection_types": ["exact", "usage"],
          "confidence": 1.0
        }
      ],
      "first_added_at": "2025-xx-xx...",
      "updated_at": "2025-xx-xx..."
    }
  }
}
```

### 4. Comportement injecté dans le pipeline (`main.py` §2 bis)

- Les entrées du registre deviennent les **masters** de leur groupe (prépended à la liste).
- Les CodeLists dont l'`id` figure dans `replaces[].id` sont **exclues** de la détection.

---

## Registre de sortie : `codelist_duplicates.json`

Le pipeline produit un seul fichier JSON par CodeList :

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
        "detection_types": ["exact", "semantic_list", "usage"],
        "confidence": 1.0,
        "decision": "pending"
      }
    ]
  }
}
```

**Champs des décisions :** `pending`, `approve` (consolider), `reject` (distinct).

---

## API REST (FastAPI)

| Méthode | Chemin | Rôle |
|---|---|---|
| `GET` | `/` | Frontend HTML/JS (index.html inliné) |
| `GET` | `/health` | `{"status":"healthy","jobs_active":N,"version":"1.0.0"}` |
| `POST` | `/api/pipeline` | Lancer un job pipeline → retourne `job_id` |
| `GET` | `/api/pipeline/{id}/status` | État d'un job (progress, phase, logs) |
| `GET` | `/api/pipeline/{id}/sse` | Stream SSE progression temps réel |
| `GET` | `/api/registry/stats` | Statistiques globales du registre |
| `GET` | `/api/registry` | Charger `codelist_duplicates.json` |
| `POST` | `/api/registry/save` | Sauvegarder + sync `cleaned_codelists.json` |
| `GET` | `/api/registry/cleaned` | Lire le registre nettoyé associé |
| `PATCH` | `/api/registry/decision` | Mettre à jour une décision |
| `POST` | `/api/registry/bulk` | Décisions batch (approve/reject par critère) |
| `GET` | `/api/registry/codelists` | Liste paginée avec filtres |
| `GET` | `/api/registry/codelist/{id}` | Détail d'une CodeList |
| `GET` | `/api/cleaned?path=...` | Lire un registre nettoyé v2 |
| `POST` | `/api/cleaned/save?path=...` | Sauvegarder un registre nettoyé v2 |
| `POST` | `/api/registry/add-to-cleaned` | Ajouter une CodeList (sans doublon) au registre nettoyé |
| `POST` | `/api/upload` | Upload d'un fichier vers S3 |

> **OpenAPI auto-découvrable** : `/docs` (ReDoc) et `/openapi.json`.

---

## Datasets de démonstration

Le dossier `demo/` contient des fichiers XML de petite taille, générés à partir des sources S3, avec des paires connues pour illustrer le pipeline.

### `BTS_demo.xml` (4 paires connues + 2 uniques)

| Paire | Codes | Type | Partagé par |
|---|---|---|---|
| `N_nom_23` ↔ `NOM` | 7 codes identiques | Exact + usage | `sdsi`, `idcc`, `reglem` |
| `N_pcs_23` ↔ `N_PCS` | 3 codes, ordre différent | Exact (sig identique) | — (variables disjointes) |
| `N_cp_23` ↔ `CP` | 6 codes identiques | Exact + usage | `cp_siege`, `cp_lieu_trav` |
| `N_departements_23` ↔ `DEPARTEMENTS` | 101 codes, ordre différent | Exact (sig identique) | `codp_siege` |

Générés par `demo/make_demo_files.py`.

### `BPE_demo.xml` (variantes annuelles + clones cross-fichier)

| Paire | Codes | Type |
|---|---|---|
| `N_annee_24` ↔ `N_ANNEE_2023` ↔ `N_ANNEE_22` | 1 code ("Année") | Exact + usage |
| `N_nature_juridique_24` ↔ `NATURE_JURIDI_2023` | 36 codes identiques | Exact + usage |

Ces exemples couvrent : copies conformes, variantes d'ordre, clones cross-fichier.

> **Générer les fichiers** : `uv run demo/make_demo_files.py` (télécharge les sources S3 et écrit les XML dans `demo/`).

---

## Statistiques sur BTS.xml (réf.)

| Métrique | Valeur |
|---|---|
| CodeLists extraites | 156 |
| Codes totaux | 998 |
| Catégories indexées | 831 (+ 1 orpheline) |
| Références Variable → CodeList | 503 |
| CodeLists liées à des variables | 156 / 156 (100 %) |
| Groupes de doublons exacts | 29 (54 redondantes) |
| Signatures uniques | 102 |
| Groupes de même usage | 17 |

> Voir la [note méthodologique](doc/note_methodologique_dedoublonnage.qmd) pour le détail.

---

## Structure du projet

```
metadata-scrubber/
├── demo/
│   ├── BTS_demo.xml              # 4 paires connues + 2 uniques
│   ├── BPE_demo.xml              # Variantes annuelles + clones
│   └── make_demo_files.py        # Générateur depuis sources S3
├── src/
│   ├── metadata_scrubber/
│   │   ├── main.py               # Orchestrateur CLI (scrubber)
│   │   ├── cleaned_registry.py   # Cycle v2 cleaned_codelists.json
│   │   ├── extractor.py          # Parsing DDI 3.3 (lxml)
│   │   ├── normalize.py          # NFC, lowercase, signature
│   │   ├── funnel.py             # Détection exacte + flou
│   │   ├── semantic.py           # Embeddings + juge LLM
│   │   ├── signals.py            # var_sig, usage_groups
│   │   ├── types.py              # CodeList, CandidateFusion
│   │   ├── registry_io.py        # I/O JSON local/S3
│   │   ├── reporting/
│   │   │   └── duplicates_registry.py  # codelist_duplicates.json
│   │   └── ui/
│   │       ├── server.py         # FastAPI entry point (scrubber-web)
│   │       ├── models.py         # Pydantic schemas
│   │       ├── services/         # job_manager, pipeline_service, registry_service, upload_service
│   │       ├── templates/
│   │       │   └── index.html    # Frontend embarqué
│   │       └── static/
│   │           ├── css/style.css
│   │           └── js/*.js       # config, main, pipeline, registry, cleaned
│   └── metadata_scrubber.egg-info/
├── tests/
│   └── test_scrubber.py          # ~63 tests unitaires
├── doc/
│   └── note_methodologique_dedoublonnage.qmd
├── pyproject.toml                # uv (pipeline + app + dev)
├── uv.lock                       # Verrouillage des dépendances
└── README.md
```

---

## Qualité du code

Le projet suit les standards Python data science d'Onyxia/SSP Cloud.

| Pratique | Outil | Commande |
|---|---|---|
| Formatage | `ruff format` | `uv run ruff format src/ tests/` |
| Linting (rules + import tri) | `ruff check` | `uv run ruff check src/ tests/` |
| Tests unitaires | `pytest` | `uv run pytest tests/ -v` |
| Lockfile | `uv` | `uv lock` (après modification `pyproject.toml`) |
| Exécution | `uv run` | `uv run scrubber` / `uv run scrubber-web` |

**Structure `src/`** : le code source est dans `src/metadata_scrubber/` (peut être importé par son nom de package, testable sans installation).
**Entrées de console** : `scrubber` (CLI pipeline) et `scrubber-web` (FastAPI) dans `pyproject.toml`.
**Aucun secret en clair** : les variables `OPENAI_*`, `AWS_*` sont lues depuis l'environnement.

---

## Licence

Projet interne SSP Cloud / Insee — usage académique et interne uniquement.
Données publiques/non sensibles uniquement sur instance publique.

> **Note méthodologique complète** : [doc/note_methodologique_dedoublonnage.qmd](doc/note_methodologique_dedoublonnage.qmd)

---

## Déploiement Kubernetes (SSP Cloud / Onyxia)

> Le déploiement se fait en GitOps via **ArgoCD** sur le namespace
> `projet-metadonnees-rmes`. L'application est exposée sous le hostname
> `metadata-scrubber.lab.sspcloud.fr` avec un contrôle d'accès par mot de passe
> partagé (Basic Auth).

### 1. Secret Kubernetes `scrubber-config`

Un seul Secret regroupe toutes les variables d'environnement — S3 et Basic Auth :

```bash
kubectl create secret generic scrubber-config \
  --from-literal=AWS_ACCESS_KEY_ID="xxx" \
  --from-literal=AWS_SECRET_ACCESS_KEY="yyy" \
  --from-literal=AWS_SESSION_TOKEN="zzz" \
  --from-literal=AWS_S3_ENDPOINT="minio.lab.sspcloud.fr" \
  --from-literal=AWS_DEFAULT_REGION="us-east-1" \
  --from-literal=SCRUBBER_AUTH_PASSWORD="********" \
  -n projet-metadonnees-rmes \
  --dry-run=client -o yaml | kubectl apply -f -
```

**Variables injectées :**

| Clé du Secret | Rôle |
|---|---|
| `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` / `AWS_SESSION_TOKEN` | Accès S3/MinIO (jetons persistants) |
| `AWS_S3_ENDPOINT` | Endpoint MinIO (défaut `minio.lab.sspcloud.fr`) |
| `AWS_DEFAULT_REGION` | Région (défaut `us-east-1`) |
| `SCRUBBER_AUTH_PASSWORD` | Mot de passe partagé pour le Basic Auth |

> **Création des clés S3** :
> `mc admin accesskey create`

### 2. Ingress ArgoCD

Déclarer l'application ArgoCD (une seule fois) :

```bash
kubectl apply -f deploy/application.yaml -n projet-metadonnees-rmes
```

ArgoCD surveille ensuite le dépôt et synchronise automatiquement le cluster
(~5 min ou avec `kubectl argo app sync metadata-scrubber`).

Cela crée :

| Ressource | Rôle |
|---|---|
| **Deployment** | 1 replica, image `ghcr.io/inseefrlab/metadata-scrubber:latest`, probes sur `/health` |
| **Service** | ClusterIP port 80 → 8000 |
| **Ingress** | Host `metadata-scrubber.lab.sspcloud.fr`, timeout SSE 3600s, body 100m |

### 3. CI — GitHub Actions → GHCR

À chaque push sur `main` ou tag `v*`, le workflow
[`.github/workflows/deploy.yml`](.github/workflows/deploy.yml) construit l'image
et la push sur **GHCR** (`ghcr.io/inseefrlab/metadata-scrubber`).

### Accès

Ouvrir `https://metadata-scrubber.lab.sspcloud.fr` dans un navigateur — une
boîte Basic Auth demande le mot de passe partagé.

- `/health` — check public (jamais protégé)
- `/docs` — swagger UI (jamais protégé)
- Reste de l'API et du Frontend — protégé par Basic Auth
