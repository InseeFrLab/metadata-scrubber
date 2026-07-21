# mcp-rmes — Serveur MCP pour l'API SPARQL RMéS / INSEE

Serveur [MCP](https://modelcontextprotocol.io) exposant quatre outils pour interroger
le triplestore RMéS (Référentiel de Métadonnées et de Statistiques) de l'INSEE.

## Exécution

Lancer avec `uv` :

```bash
uv run mcp-rmes.py
```

`uv` installe automatiquement les dépendances déclarées (`fastmcp>=3.4.2`)
et gère un environnement virtuel isolé.

## Configuration

L'endpoint du triplestore est configuré dans le script :

```python
ENDPOINT = "https://rdf.insee.fr/sparql"
```

## Outils

| Outil | Description |
|---|---|
| `sparql_query(query)` | Exécute une requête SPARQL SELECT ou ASK. Retourne les résultats en JSON. |
| `describe_resource(uri)` | Exécute un DESCRIBE SPARQL sur une URI et retourne le RDF/XML. |
| `list_graphs()` | Liste les graphes nommés disponibles dans le triplestore. |
| `describe_graph(graph_uri)` | Retourne la description structurée (classes, propriétés, préfixes, shapes) d'un graphe connu. |

## Graphe connu

Actuellement, un seul graphe est enregistré en dur :

- `http://rdf.insee.fr/graphes/concepts/definitions` — Concepts statistiques RMéS
  (84 460 concepts `skos:Concept`, modèles SKOS/XKOS, 4 shapes SHACL)

`describe_graph` renvoie la liste des graphes connus si l'URI demandée n'est pas
dans le registre.

Le serveur expose les outils via le protocole MCP (stdio ou SSE).

## Modèle de données

Les concepts statistiques utilisent :

- `skos:Concept` — concept avec `skos:prefLabel` (fr/en), `skos:definition`
  (pointant vers `xkos:ExplanatoryNote`), `skos:broader`/`narrower`, etc.
- `xkos:ExplanatoryNote` — contenu textuel brut (`xkos:plainText`) et HTML
  (`eurovoc:noteLiteral`)
- `skos:Collection` — regroupements thématiques (utilise `dct:title`)

Voir `describe_graph` pour la documentation complète des classes et propriétés.

## Ajout sur opencode

L'ajout de ce serveur sur opencode se fait de la façon suivante, dans `opencode.json` :


```json
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "mcp-rmes": {
      "type": "local",
      "command": [
        "uv",
        "run",
        "</absolute/path/to/>mcp-rmes.py"
      ],
      "enabled": true
    }
    [...]
  }
}

```
