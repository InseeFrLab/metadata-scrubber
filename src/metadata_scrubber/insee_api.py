"""insee_api.py — Client de l'API Métadonnées V2 de l'Insee (lecture seule).

Chaîne suivie pour retrouver les CodeLists d'une opération statistique :

    /operations/serie/{s}                   → opérations de la série, famille
    /structures/operation/{op}/fichiers     → StudyUnit : PhysicalInstanceReference
    /structures/fichier/{agency}/{id}/{v}   → Variables : CodeListReference
    /structures/codelist/{agency}/{id}/{v}  → CodeList + Codes + Catégories

Les objets DDI versionnés (fichiers, CodeLists) sont immuables : ils sont mis
en cache sous ``<cache_dir>/<type>/<agency>/<id>/<version>.xml`` (local ou S3),
si bien qu'une reconstruction ne retélécharge que les nouvelles versions.
L'API est publique : aucune authentification.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from typing import Any

import httpx
from lxml import etree

from .extractor import SAFE_PARSER, _child, _local, _text_of, parse_xml
from .registry_io import read_text, write_text

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://api.insee.fr/metadonnees/V2"
_RETRY_STATUSES = {429, 500, 502, 503, 504}
_MAX_ATTEMPTS = 4


@dataclass(frozen=True)
class DdiRef:
    """Référence DDI versionnée (agence, identifiant, version)."""
    agency: str
    id: str
    version: str

    @property
    def urn(self) -> str:
        return f"urn:ddi:{self.agency}:{self.id}:{self.version}"


def label_fr(labels: Any) -> str:
    """Libellé français d'une liste [{"langue", "contenu"}] de l'API (sinon le premier)."""
    if not isinstance(labels, list) or not labels:
        return ""
    for item in labels:
        if item.get("langue") == "fr" and item.get("contenu"):
            return item["contenu"]
    return labels[0].get("contenu", "")


def references(raw: bytes, name: str) -> list[DdiRef]:
    """Références `name` (ex. CodeListReference) d'un document DDI, sans doublon, dans l'ordre."""
    refs: dict[DdiRef, None] = {}
    for el in etree.fromstring(raw, SAFE_PARSER).iter():
        if isinstance(el.tag, str) and _local(el) == name:
            ref = DdiRef(_text_of(el, "Agency"), _text_of(el, "ID"), _text_of(el, "Version"))
            if ref.agency and ref.id and ref.version:
                refs[ref] = None
    return list(refs)


class InseeMetadataClient:
    """Client HTTP de l'API Métadonnées, avec cache des objets DDI versionnés.

    Args:
        base_url: racine de l'API (défaut : `INSEE_METADATA_API_URL` ou l'API de production).
        cache_dir: dossier de cache local ou S3 ; None = pas de cache.
        http: client httpx (injecté par les tests).
    """

    def __init__(
        self,
        base_url: str | None = None,
        cache_dir: str | None = None,
        http: httpx.Client | None = None,
        timeout: float = 60.0,
    ) -> None:
        self.base_url = (
            base_url or os.environ.get("INSEE_METADATA_API_URL") or DEFAULT_BASE_URL
        ).rstrip("/")
        self.cache_dir = cache_dir.rstrip("/") if cache_dir else None
        self.http = http or httpx.Client(
            timeout=timeout,
            follow_redirects=True,
            headers={"User-Agent": "metadata-scrubber"},
        )
        self.calls = 0        # appels réseau effectués
        self.cache_hits = 0   # objets lus dans le cache
        self.restricted: list[str] = []  # objets non publics (401/403), ignorés

    # ------------------------------------------------------------------
    # HTTP
    # ------------------------------------------------------------------

    def _get(self, path: str, accept: str) -> httpx.Response | None:
        """GET avec nouvelles tentatives (429, 5xx, erreurs réseau).

        404 → None. 401/403 → None aussi : certains fichiers d'une opération
        publiée ne sont pas diffusés ; ils sont ignorés et notés dans `restricted`.
        """
        url = f"{self.base_url}/{path.lstrip('/')}"
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            self.calls += 1
            try:
                resp = self.http.get(url, headers={"Accept": accept})
            except httpx.TransportError as exc:
                if attempt == _MAX_ATTEMPTS:
                    raise
                logger.warning("API Insee %s : %s — nouvel essai", url, exc)
            else:
                if resp.status_code == 404:
                    return None
                if resp.status_code in (401, 403):
                    logger.warning("API Insee %s : accès refusé (HTTP %s), ignoré", url, resp.status_code)
                    self.restricted.append(path)
                    return None
                if resp.status_code not in _RETRY_STATUSES or attempt == _MAX_ATTEMPTS:
                    resp.raise_for_status()
                    return resp
                logger.warning("API Insee %s : HTTP %s — nouvel essai", url, resp.status_code)
            time.sleep(min(2 ** attempt, 10) * 0.5)
        return None  # inatteignable

    def get_json(self, path: str) -> Any | None:
        resp = self._get(path, "application/json")
        return resp.json() if resp is not None else None

    def get_xml(self, path: str) -> bytes | None:
        resp = self._get(path, "application/xml")
        return resp.content if resp is not None else None

    def _versioned_xml(self, kind: str, ref: DdiRef) -> bytes | None:
        """Objet DDI versionné, lu dans le cache s'il y est."""
        cache_path = (
            f"{self.cache_dir}/{kind}/{ref.agency}/{ref.id}/{ref.version}.xml"
            if self.cache_dir else None
        )
        if cache_path:
            try:
                raw = read_text(cache_path).encode("utf-8")
                self.cache_hits += 1
                return raw
            except FileNotFoundError:
                pass
        raw = self.get_xml(f"structures/{kind}/{ref.agency}/{ref.id}/{ref.version}")
        if raw is not None and cache_path:
            try:
                write_text(raw.decode("utf-8"), cache_path)
            except Exception as exc:  # le cache est une optimisation
                logger.warning("Cache API non écrit (%s) : %s", cache_path, exc)
        return raw

    # ------------------------------------------------------------------
    # Opérations
    # ------------------------------------------------------------------

    def list_series(self) -> list[dict[str, str]]:
        """Toutes les séries : [{"id", "label"}]."""
        return [
            {"id": s.get("seriesId", ""), "label": label_fr(s.get("label"))}
            for s in self.get_json("operations/series") or []
            if s.get("seriesId")
        ]

    def serie(self, serie_id: str) -> dict[str, Any] | None:
        """Série : {"id", "label", "famille", "operations": [{"id", "label"}]} ; None si inconnue."""
        data = self.get_json(f"operations/serie/{serie_id}")
        if data is None:
            return None
        famille = data.get("famille") or {}
        return {
            "id": data.get("seriesId", serie_id),
            "label": label_fr(data.get("label")),
            "famille": {"id": famille.get("id", ""), "label": label_fr(famille.get("label"))},
            "operations": [
                {"id": o.get("id", ""), "label": label_fr(o.get("label"))}
                for o in data.get("operations") or []
                if o.get("id")
            ],
        }

    def operation(self, op_id: str) -> dict[str, Any] | None:
        """Opération : {"id", "label", "millesime", "serie": {"id", "label"}} ; None si inconnue."""
        data = self.get_json(f"operations/operation/{op_id}")
        if data is None:
            return None
        serie = data.get("serie") or {}
        return {
            "id": data.get("id", op_id),
            "label": label_fr(data.get("label")),
            "millesime": data.get("millesime", ""),
            "serie": {"id": serie.get("id", ""), "label": label_fr(serie.get("label"))},
        }

    # ------------------------------------------------------------------
    # Structures DDI
    # ------------------------------------------------------------------

    def operation_files(self, op_id: str) -> list[DdiRef] | None:
        """Fichiers (PhysicalInstance) décrits pour une opération.

        Returns:
            None si l'opération n'a pas de description DDI (404) ; liste vide si
            son StudyUnit ne référence aucun fichier.
        """
        raw = self.get_xml(f"structures/operation/{op_id}/fichiers")
        if raw is None:
            return None
        return references(raw, "PhysicalInstanceReference")

    def file_fragments(self, ref: DdiRef) -> list[etree._Element]:
        """Objets DDI (Variables…) d'un fichier ; vide s'il est introuvable."""
        raw = self._versioned_xml("fichier", ref)
        return parse_xml(raw) if raw is not None else []

    def codelist_fragments(self, ref: DdiRef) -> list[etree._Element]:
        """CodeList + Codes + Catégories ; vide si la liste est introuvable."""
        raw = self._versioned_xml("codelist", ref)
        return parse_xml(raw) if raw is not None else []

    def latest_codelist_fragments(self, agency: str, cl_id: str) -> list[etree._Element]:
        """Dernière version d'une CodeList (non mise en cache : la version peut changer)."""
        raw = self.get_xml(f"structures/codelist/{agency}/{cl_id}")
        return parse_xml(raw) if raw is not None else []

    # ------------------------------------------------------------------
    # Nomenclatures
    # ------------------------------------------------------------------

    def geo_list(self, kind: str, date: str | None = None) -> list[dict[str, str]]:
        """Territoires actifs (ex. kind="regions") : [{"code", "intitule"}]."""
        path = f"geo/{kind}" + (f"?date={date}" if date else "")
        return [
            {"code": t.get("code", ""), "intitule": t.get("intitule", "")}
            for t in self.get_json(path) or []
            if t.get("code")
        ]


def code_list_reference(variable: etree._Element) -> DdiRef | None:
    """CodeListReference d'une Variable (sous VariableRepresentation ou directement)."""
    holder = _child(variable, "VariableRepresentation")
    code_repr = _child(holder if holder is not None else variable, "CodeRepresentation")
    if code_repr is None:
        return None
    ref = _child(code_repr, "CodeListReference")
    if ref is None:
        return None
    agency, cl_id, version = (_text_of(ref, k) for k in ("Agency", "ID", "Version"))
    return DdiRef(agency, cl_id, version) if agency and cl_id and version else None
