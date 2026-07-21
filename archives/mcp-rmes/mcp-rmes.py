# /// script
# requires-python = ">=3.13"
# dependencies = [
#     "fastmcp>=3.4.2",
# ]
# ///




"""
Serveur MCP SPARQL — RMéS / INSEE
Expose quatre outils :
  - sparql_query       : exécute une requête SELECT ou ASK
  - describe_resource  : DESCRIBE d'une URI RDF
  - list_graphs        : liste les graphes nommés disponibles
  - describe_graph     : description structurée d'un graphe RDF
"""




from fastmcp import FastMCP
import httpx
import json

# ── Configuration ────────────────────────────────────────────────────────────

ENDPOINT = "https://rdf.insee.fr/sparql"

HEADERS_JSON = {
    "Accept": "application/sparql-results+json",
    "User-Agent": "MCP-SPARQL-RMeS/1.0",
}
HEADERS_XML = {
    "Accept": "application/rdf+xml",
    "User-Agent": "MCP-SPARQL-RMeS/1.0",
}

# ── Registre des graphes ──────────────────────────────────────────────────────

GRAPHS = {
    "http://rdf.insee.fr/graphes/concepts/definitions": {
        "graph": "http://rdf.insee.fr/graphes/concepts/definitions",
        "label": {
            "fr": "Concepts statistiques RMéS",
            "en": "RMéS Statistical Concepts"
        },
        "businessDomain": {
            "fr": "Référentiel de concepts statistiques de l'Insee, publié sur insee.fr",
            "en": "INSEE statistical concepts reference, published on insee.fr"
        },
        "prefixes": {
            "skos":    { "uri": "http://www.w3.org/2004/02/skos/core#",        "standard": True },
            "xkos":    { "uri": "http://rdf-vocabulary.ddialliance.org/xkos#", "standard": True },
            "dct":     { "uri": "http://purl.org/dc/terms/",                   "standard": True },
            "pav":     { "uri": "http://purl.org/pav/",                        "standard": True },
            "base":    { "uri": "http://rdf.insee.fr/def/base#",               "standard": False, "fetch": "http://rdf.insee.fr/def/base" },
            "eurovoc": { "uri": "http://eurovoc.europa.eu/schema#",            "standard": False, "fetch": "http://eurovoc.europa.eu/schema" }
        },
        "classes": {
            "skos:Concept": {
                "description": {
                    "fr": "Concept statistique du référentiel RMéS. URI de la forme http://id.insee.fr/concepts/definition/cXXXX",
                    "en": "Statistical concept from the RMéS reference. URI pattern: http://id.insee.fr/concepts/definition/cXXXX"
                },
                "properties": [
                    { "path": "skos:notation",            "cardinality": "1",    "traversable": False, "comment": { "fr": "Identifiant court, ex. c1020", "en": "Short identifier, e.g. c1020" } },
                    { "path": "skos:prefLabel",           "cardinality": "2",    "traversable": False, "comment": { "fr": "Un libellé par langue, fr et en obligatoires", "en": "One label per language, fr and en are mandatory" } },
                    { "path": "skos:altLabel",            "cardinality": "0..n", "traversable": False, "comment": { "fr": "Synonymes et acronymes", "en": "Synonyms and acronyms" } },
                    { "path": "skos:definition",          "cardinality": "1",    "traversable": True,  "comment": { "fr": "Pointe vers une xkos:ExplanatoryNote portant le texte via xkos:plainText (brut) et eurovoc:noteLiteral (HTML)", "en": "Points to a xkos:ExplanatoryNote carrying text via xkos:plainText (plain) and eurovoc:noteLiteral (HTML)" } },
                    { "path": "skos:scopeNote",           "cardinality": "0..2", "traversable": True,  "comment": { "fr": "Définition courte ≤ 350 caractères, une par langue. Usage : infobulles", "en": "Short definition ≤ 350 characters, one per language. Used for tooltips" } },
                    { "path": "skos:editorialNote",       "cardinality": "0..2", "traversable": True,  "comment": { "fr": "Note éditoriale publiée (remarques insee.fr), une par langue", "en": "Published editorial note (insee.fr remarks), one per language" } },
                    { "path": "skos:inScheme",            "cardinality": "1",    "traversable": True,  "comment": { "fr": "Scheme unique : http://id.insee.fr/concepts/definitions/scheme", "en": "Single scheme: http://id.insee.fr/concepts/definitions/scheme" } },
                    { "path": "skos:topConceptOf",        "cardinality": "0..1", "traversable": True,  "comment": { "fr": "Présent uniquement si pas de skos:broader. Mutuellement exclusif avec skos:broader", "en": "Present only if no skos:broader. Mutually exclusive with skos:broader" } },
                    { "path": "skos:broader",             "cardinality": "0..1", "traversable": True,  "comment": { "fr": "Concept parent. Réciproque obligatoire : skos:narrower sur la cible", "en": "Parent concept. Mandatory reciprocal: skos:narrower on the target" } },
                    { "path": "skos:narrower",            "cardinality": "0..n", "traversable": True,  "comment": { "fr": "Concepts enfants. Réciproque obligatoire : skos:broader sur la cible", "en": "Child concepts. Mandatory reciprocal: skos:broader on the target" } },
                    { "path": "skos:related",             "cardinality": "0..n", "traversable": True,  "comment": { "fr": "Concept connexe éditorial (voir aussi). Réciproque obligatoire. À distinguer de dct:references", "en": "Editorial related concept (see also). Mandatory reciprocal. Distinct from dct:references" } },
                    { "path": "dct:references",           "cardinality": "0..n", "traversable": True,  "comment": { "fr": "Concept explicitement cité dans le texte de la définition. Distinct de skos:related qui est une relation éditoriale", "en": "Concept explicitly cited in the definition text. Distinct from skos:related which is an editorial relation" } },
                    { "path": "dct:isReplacedBy",         "cardinality": "0..n", "traversable": True,  "comment": { "fr": "Concept successeur. Réciproque obligatoire : dct:replaces sur la cible", "en": "Successor concept. Mandatory reciprocal: dct:replaces on the target" } },
                    { "path": "dct:replaces",             "cardinality": "0..n", "traversable": True,  "comment": { "fr": "Concept remplacé. Réciproque obligatoire : dct:isReplacedBy sur la cible", "en": "Replaced concept. Mandatory reciprocal: dct:isReplacedBy on the target" } },
                    { "path": "dct:created",              "cardinality": "1",    "traversable": False, "comment": { "fr": "xsd:dateTime", "en": "xsd:dateTime" } },
                    { "path": "dct:modified",             "cardinality": "1",    "traversable": False, "comment": { "fr": "xsd:dateTime", "en": "xsd:dateTime" } },
                    { "path": "dct:valid",                "cardinality": "0..1", "traversable": False, "comment": { "fr": "Date de fin de validité. Si présent, dct:isReplacedBy est obligatoire", "en": "End of validity date. If present, dct:isReplacedBy is mandatory" } },
                    { "path": "base:disseminationStatus", "cardinality": "1",    "traversable": False, "comment": { "fr": "Valeurs : PublicGenerique ou Prive", "en": "Values: PublicGenerique or Prive" } },
                    { "path": "base:additionalMaterial",  "cardinality": "0..n", "traversable": True,  "comment": { "fr": "Lien vers document complémentaire externe. Usage non formalisé, présent sur ~10 concepts", "en": "Link to external supplementary document. Non-formalised usage, present on ~10 concepts" } }
                ]
            },
            "xkos:ExplanatoryNote": {
                "description": {
                    "fr": "Porte le contenu textuel des définitions et notes. Jamais instancié directement — toujours référencé depuis skos:Concept. URI versionnée : .../definition/v1/fr",
                    "en": "Carries the textual content of definitions and notes. Never instantiated directly — always referenced from skos:Concept. Versioned URI: .../definition/v1/fr"
                },
                "properties": [
                    { "path": "xkos:plainText",      "cardinality": "1",    "traversable": False, "comment": { "fr": "Contenu textuel brut", "en": "Plain text content" } },
                    { "path": "eurovoc:noteLiteral", "cardinality": "1",    "traversable": False, "comment": { "fr": "Contenu HTML (XMLLiteral) pour rendu web", "en": "HTML content (XMLLiteral) for web rendering" } },
                    { "path": "dct:language",        "cardinality": "1",    "traversable": False, "comment": { "fr": "fr ou en", "en": "fr or en" } },
                    { "path": "pav:version",         "cardinality": "1",    "traversable": False, "comment": { "fr": "Entier. Doit correspondre au numéro encodé dans l'URI", "en": "Integer. Must match the version number encoded in the URI" } },
                    { "path": "base:validFrom",      "cardinality": "0..1", "traversable": False, "comment": { "fr": "xsd:dateTime", "en": "xsd:dateTime" } },
                    { "path": "base:validUntil",     "cardinality": "0..1", "traversable": False, "comment": { "fr": "xsd:dateTime", "en": "xsd:dateTime" } }
                ]
            },
            "skos:Collection": {
                "description": {
                    "fr": "Regroupement thématique ou éditorial de concepts. Un concept peut appartenir à plusieurs collections. URI de la forme http://id.insee.fr/concepts/definitions/XXX",
                    "en": "Thematic or editorial grouping of concepts. A concept may belong to multiple collections. URI pattern: http://id.insee.fr/concepts/definitions/XXX"
                },
                "properties": [
                    { "path": "dct:title",       "cardinality": "1",    "traversable": False, "comment": { "fr": "Libellé. Écart SKOS : utilise dct:title et non skos:prefLabel", "en": "Label. SKOS deviation: uses dct:title instead of skos:prefLabel" } },
                    { "path": "dct:description", "cardinality": "0..1", "traversable": False, "comment": { "fr": "", "en": "" } },
                    { "path": "dct:created",     "cardinality": "0..1", "traversable": False, "comment": { "fr": "", "en": "" } },
                    { "path": "dct:modified",    "cardinality": "0..1", "traversable": False, "comment": { "fr": "", "en": "" } },
                    { "path": "skos:member",     "cardinality": "1..n", "traversable": True,  "comment": { "fr": "C'est la collection qui référence ses membres, pas l'inverse", "en": "The collection references its members, not the other way around" } }
                ]
            },
            "skos:ConceptScheme": {
                "description": {
                    "fr": "Singleton chapeautant l'ensemble des concepts. URI : http://id.insee.fr/concepts/definitions/scheme",
                    "en": "Singleton encompassing all concepts. URI: http://id.insee.fr/concepts/definitions/scheme"
                },
                "properties": [
                    { "path": "skos:prefLabel",      "cardinality": "1", "traversable": False, "comment": { "fr": "", "en": "" } },
                    { "path": "dct:title",           "cardinality": "1", "traversable": False, "comment": { "fr": "Redondant avec skos:prefLabel", "en": "Redundant with skos:prefLabel" } },
                    { "path": "dct:publisher",       "cardinality": "1", "traversable": False, "comment": { "fr": "", "en": "" } },
                    { "path": "pav:lastRefreshedOn", "cardinality": "1", "traversable": False, "comment": { "fr": "Date de dernière mise à jour du triplestore public", "en": "Last update date of the public triplestore" } }
                ]
            }
        },
        "shapes": ["ConceptShape", "ExplanatoryNoteShape", "CollectionShape", "ConceptSchemeShape"],
        "modelingNotes": [
            {
                "fr": "skos:definition pointe toujours vers une xkos:ExplanatoryNote, jamais vers un littéral — choix INSEE pour le versioning des définitions",
                "en": "skos:definition always points to a xkos:ExplanatoryNote, never to a literal — INSEE design choice for definition versioning"
            },
            {
                "fr": "skos:Collection utilise dct:title et non skos:prefLabel (écart au modèle SKOS standard)",
                "en": "skos:Collection uses dct:title instead of skos:prefLabel (deviation from the standard SKOS model)"
            },
            {
                "fr": "skos:topConceptOf et skos:broader sont mutuellement exclusifs",
                "en": "skos:topConceptOf and skos:broader are mutually exclusive"
            },
            {
                "fr": "Les liens skos:broader/narrower et skos:related doivent être réciproques",
                "en": "skos:broader/narrower and skos:related links must be reciprocal"
            },
            {
                "fr": "dct:references désigne un concept cité dans le texte de la définition, ce n'est pas une relation éditoriale comme skos:related",
                "en": "dct:references denotes a concept cited in the definition text, it is not an editorial relation like skos:related"
            },
            {
                "fr": "eurovoc:noteLiteral est utilisé pour le rendu HTML des définitions (XMLLiteral)",
                "en": "eurovoc:noteLiteral is used for HTML rendering of definitions (XMLLiteral)"
            }
        ]
    }
}

# ── Serveur MCP ───────────────────────────────────────────────────────────────

mcp = FastMCP("sparql-rmse")


# ── Outils ───────────────────────────────────────────────────────────────────

@mcp.tool()
async def sparql_query(query: str) -> str:
    """
    Exécute une requête SPARQL SELECT ou ASK sur l'endpoint RMéS.
    Retourne les résultats au format JSON (liaisons de variables).

    Args:
        query: Requête SPARQL valide (SELECT, ASK…)
    """
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(
            ENDPOINT,
            data={"query": query},
            headers=HEADERS_JSON,
        )
        response.raise_for_status()
        return response.text


@mcp.tool()
async def describe_resource(uri: str) -> str:
    """
    Effectue un DESCRIBE SPARQL sur une URI RDF et retourne le RDF/XML.
    Utile pour inspecter un concept, une nomenclature, un jeu de données…

    Args:
        uri: URI complète de la ressource RDF (ex: http://id.insee.fr/concepts/definition/c1020)
    """
    query = f"DESCRIBE <{uri}>"
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(
            ENDPOINT,
            data={"query": query},
            headers=HEADERS_XML,
        )
        response.raise_for_status()
        return response.text


@mcp.tool()
async def list_graphs() -> str:
    """
    Liste les graphes nommés disponibles dans le triplestore.
    Permet de connaître la structure du dépôt RDF avant d'interroger.
    """
    query = "SELECT DISTINCT ?g WHERE { GRAPH ?g { ?s ?p ?o } } ORDER BY ?g"
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(
            ENDPOINT,
            data={"query": query},
            headers=HEADERS_JSON,
        )
        response.raise_for_status()
        return response.text


@mcp.tool()
async def describe_graph(graph_uri: str) -> str:
    """
    Returns the structured description of an RDF graph from the RMéS triplestore.
    Call this tool first when working on an unknown graph — it provides the context
    needed to formulate correct SPARQL queries and interpret resources.

    The returned JSON contains:
    - `prefixes`: prefixes to use in SPARQL queries for this graph. Ontologies marked
      `standard: false` are uncommon and absent from standard LLM training — fetch
      their definition via the `fetch` URL if you need to understand their properties.
    - `classes`: classes present in the graph, each with their properties:
        - `cardinality`: expected number of occurrences (`1` = mandatory and unique,
          `0..1` = optional, `0..n` = optional and multiple, `1..n` = mandatory and
          multiple, `2` = exactly two)
        - `traversable`: `true` if the value is a navigable URI — you can call
          `describe_resource(uri)` on this value to explore the graph further.
          `false` if the value is a literal.
        - `comment`: details on property usage, deviations from standard models,
          reciprocity constraints to respect
    - `shapes`: list of applicable SHACL shapes — call `get_shapes` for validation rules
    - `modelingNotes`: INSEE-specific modeling choices that deviate from standards,
      read before formulating queries

    All textual fields are bilingual (fr/en).

    If the requested URI is not known, the tool returns the list of available graphs.

    ---

    Retourne la description structurée d'un graphe RDF du triplestore RMéS.
    Appelle ce tool en premier quand tu travailles sur un graphe inconnu — il fournit
    le contexte nécessaire pour formuler des requêtes SPARQL correctes et interpréter
    les ressources.

    Le JSON retourné contient :
    - `prefixes` : les préfixes à utiliser dans les requêtes SPARQL sur ce graphe.
      Les ontologies marquées `standard: false` sont peu répandues et absentes du
      training standard — récupère leur définition via l'URL `fetch` si tu as besoin
      de comprendre leurs propriétés.
    - `classes` : les classes présentes dans le graphe, chacune avec ses propriétés :
        - `cardinality` : nombre d'occurrences attendues (`1` = obligatoire et unique,
          `0..1` = optionnel, `0..n` = optionnel et multiple, `1..n` = obligatoire et
          multiple, `2` = exactement deux)
        - `traversable` : `true` si la valeur est une URI navigable — tu peux appeler
          `describe_resource(uri)` sur cette valeur pour explorer le graphe plus loin.
          `false` si la valeur est un littéral.
        - `comment` : précisions sur l'usage de la propriété, les écarts au modèle
          standard, les contraintes de réciprocité à respecter
    - `shapes` : liste des shapes SHACL applicables — appelle `get_shapes` pour le
      détail des règles de validation
    - `modelingNotes` : choix de modélisation propres à l'INSEE qui s'écartent des
      standards, à lire avant de formuler des requêtes

    Tous les champs textuels sont bilingues (fr/en).

    Si l'URI demandée n'est pas connue, le tool retourne la liste des graphes disponibles.

    Args:
        graph_uri: URI du graphe RDF (ex: http://rdf.insee.fr/graphes/concepts/definitions)
    """
    if graph_uri not in GRAPHS:
        return json.dumps({
            "error": "Graph not found in registry",
            "knownGraphs": list(GRAPHS.keys())
        }, ensure_ascii=False, indent=2)

    return json.dumps(GRAPHS[graph_uri], ensure_ascii=False, indent=2)


# ── Point d'entrée ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    mcp.run()

