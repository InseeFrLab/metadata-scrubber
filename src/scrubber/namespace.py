"""Utilities pour l'identification et le traitement des namespaces DDI.

Les métadonnées BTS peuvent contenir des CodeLists dans différents formats
DDI (3.3, 6.0) avec des XML namespaces variés.
"""

from __future__ import annotations

import re


# Regexes qui identifient un namespace DDI
_DDI3_RE = re.compile(r"(ddi:reusable:3_\d|ddi:code:3_\d|ddi:instance:3_\d|ddi:generic:3_\d)")
_DDI6_RE = re.compile(r"ddi:code:6")


def detect_ddi_namespace(xml_bytes: bytes) -> str | None:
    """Detecter le namespace DDI dominant dans un fragment XML.

    Returns
    -------
    str | None
        Version majeure mineure (ex. "3.3", "6.0") ou None si non détecté.
    """
    text = xml_bytes.decode("utf-8", errors="replace")

    # Essai DDI-6 d'abord (plus spécifique)
    if _DDI6_RE.search(text):
        return "6.0"

    # Essai DDI-3
    m = _DDI3_RE.search(text)
    if m:
        # Extraire version depuis "ddi:reusable:3_3" ou "ddi:code:3_3"
        return m.group().split(":")[-1].replace("_", ".")

    return None


def strip_namespace(tag: str) -> str:
    """Retirer le prefixe namespace d'un tag XML ('r:ID' -> 'ID')."""
    if ":" in tag:
        return tag.split(":", 1)[1]
    return tag
