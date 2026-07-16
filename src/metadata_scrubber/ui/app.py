"""
Metadata Scrubber — Interface de validation des doublons de CodeLists.

Charge codelist_duplicates.json, permet de valider chaque duplicate
(decision = approve | reject | pending), avec auto-save et actions
globales (approuver les exactes, approuver confiance ≥ 0.95…).
Peut aussi lancer un pipeline complet depuis l'onglet Pipeline.

Lancement :
    uv run scrubber-web           # FastAPI
    uv run -A streamlit run src/ui/app.py  # Streamlit
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import queue
import streamlit as st  # noqa: E402
import pandas as pd  # noqa: E402

from metadata_scrubber.ui.pipeline_runner import PipelineResult, run_pipeline_async


# ---------------------------------------------------------------------------
# S3 support
# ---------------------------------------------------------------------------


def _try_s3_path(path: str) -> bool:
    """Retourne True si la chaîne ressemble à un chemin S3."""
    return path.startswith("s3://")


def read_json(path: str) -> dict | list:
    """Lit un fichier JSON depuis le filesystem local ou S3."""
    if _try_s3_path(path):
        import s3fs
        from botocore.exceptions import ClientError

        s3 = s3fs.S3FileSystem()
        url = path  # e.g. s3://bucket/path/file.json
        try:
            with s3.open(url, "r", encoding="utf-8") as f:
                return json.load(f)
        except ClientError as e:
            st.error(f"❌ Erreur S3 pour {path} : {e}")
            raise
    else:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)


def write_json(obj: Any, path: str) -> None:
    """Écrit un objet JSON vers le filesystem local ou S3."""
    formatted = json.dumps(obj, indent=2, ensure_ascii=False)
    if _try_s3_path(path):
        import s3fs

        s3 = s3fs.S3FileSystem()
        url = path
        parent = Path(url.replace("s3://", "")).parent
        try:
            s3.makedirs(parent, exist_ok=True)
        except Exception:
            pass  # bucket existe probablement déjà
        with s3.open(url, "w", encoding="utf-8") as f:
            f.write(formatted)
    else:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(formatted)
    st.toast(f"💾 Sauvegardé : {path}", icon="💾")


# ---------------------------------------------------------------------------
# UI helpers
# ---------------------------------------------------------------------------

_BADGE_COLORS = {
    "exact": "🟢",
    "fuzzy": "🟡",
    "semantic_list": "🟣",
    "semantic_var": "🟣",
    "usage": "🔵",
}


def _badge_decision(action: str | None) -> str:
    """Retourne un emoji/badge pour le statut de décision."""
    if action == "approve":
        return "✅"
    elif action == "reject":
        return "❌"
    elif action == "pending":
        return "⏳"
    return "⏳"


def render_duplicates_tab() -> None:
    """Interface de validation manuelle des doublons de CodeLists.

    Charge codelist_duplicates.json, permet de valider chaque duplicate
    (decision = approve | reject | pending), avec auto-save.
    """

    # --- Chargement automatique depuis un pipeline récent ---
    auto_registry_url: str = st.session_state.pop("_auto_registry", "") or ""

    auto_path: str | None = None
    if auto_registry_url:
        auto_path = auto_registry_url
        st.toast("🔄 Registre des doublons pré-chargé depuis le pipeline récent", icon="🔄")

    # --- Sidebar : Configuration ---
    with st.sidebar:
        st.title("⚙️ Configuration")

        source = st.radio(
            "Source des fichiers",
            ["Local (chemin)", "S3 (bucket)"],
            help="Local pour tester en développement, S3 pour lire/écrire sur le bucket Onyxia.",
            key="dup_source",
        )

        default_path = (
            auto_path or ("audit/codelist_duplicates.json" if source == "Local (chemin)" else "s3://")
        )

        registry_path = st.text_input(
            "Fichier codelist_duplicates.json",
            value=default_path,
            key="registry_path",
        )

        st.divider()

        col1, col2 = st.columns(2)
        with col1:
            btn_load = st.button("📂 Charger", use_container_width=True)
        with col2:
            if st.session_state.get("dup_dirty", False):
                st.warning("💡 Données modifiées — non encore sauvegardées.")

        st.divider()
        st.caption("Metadata Scrubber — Validation des doublons CodeLists")

    # --- Chargement du registre ---
    load_requested = "dup_load_requested" in st.session_state and st.session_state["dup_load_requested"]

    if btn_load or load_requested or auto_path:
        st.session_state["dup_load_requested"] = True
        st.session_state["dup_dirty"] = False

        with st.spinner("Chargement du registre des doublons…"):
            try:
                registry = read_json(registry_path)
                # Enregistrer
                st.session_state["dup_registry"] = registry
                st.session_state["dup_registry_path"] = registry_path
                st.session_state["dup_dirty"] = True
                st.toast(f"✅ {len(registry)} CodeLists chargées", icon="🎉")
            except Exception as e:
                st.error(f"❌ Erreur lors du chargement : {e}")
                if "dup_load_requested" in st.session_state:
                    del st.session_state["dup_load_requested"]
                return

    # Si pas de registre chargé : message d'attente
    if "dup_registry" not in st.session_state:
        st.info("👈 Utilisez le bouton **Charger** de la barre latérale ou lancez un pipeline depuis l'onglet Pipeline.")
        return

    registry: dict = st.session_state["dup_registry"]
    registry_path = st.session_state.get("dup_registry_path", "")

    # ========================================================================
    # Statistiques globales
    # ========================================================================
    st.subheader("📊 Statistiques globales")

    total_cls = len(registry)
    total_dups = sum(len(cl.get("duplicates", [])) for cl in registry.values())
    decisions_list = [
        d["decision"]
        for cl in registry.values()
        for d in cl.get("duplicates", [])
    ]
    n_approved = sum(1 for d in decisions_list if d == "approve")
    n_rejected = sum(1 for d in decisions_list if d == "reject")
    n_pending_val = len(decisions_list) - n_approved - n_rejected

    stats_col1, stats_col2, stats_col3, stats_col4 = st.columns(4)
    stats_col1.metric("🗂️ CodeLists", total_cls)
    stats_col2.metric("🔁 Doublons détectés", total_dups)
    stats_col3.metric("✅ Approuvés", n_approved)
    stats_col4.metric("⏳ En attente", n_pending_val)

    if n_rejected > 0:
        stats_col3.caption(f"❌ {n_rejected} rejetés")

    # Compteurs par type de détection parmi les duplicates
    type_counts: dict[str, int] = {}
    for cl in registry.values():
        for dup in cl.get("duplicates", []):
            for t in dup.get("detection_types", []):
                type_counts[t] = type_counts.get(t, 0) + 1
    if type_counts:
        type_str = ", ".join(f"🟢 {k}: {v}" for k, v in sorted(type_counts.items()))
        st.caption(f"Par méthode de détection : {type_str}")

    st.divider()

    # ========================================================================
    # Actions globales
    # ========================================================================
    st.subheader("⚡ Actions globales")

    bulk_col1, bulk_col2, bulk_col3, bulk_col4 = st.columns(4)

    with bulk_col1:
        if st.button(
            "✅ Approuver toutes les exactes",
            help="Mets decision=approve sur tous les duplicates détectés par la méthode 'exact'.",
            use_container_width=True,
        ):
            _bulk_set_decisions(registry, "exact", "approve")
            st.session_state["dup_dirty"] = True
            st.toast("✅ Toutes les exactes approuvées", icon="🎉")

    with bulk_col2:
        if st.button(
            "✅ Approuver confidentielles ≥ 0.95",
            help="Mets decision=approve sur les duplicates avec confiance ≥ 0.95.",
            use_container_width=True,
        ):
            count = _bulk_set_decisions(registry, "≥ 0.95", "approve")
            st.session_state["dup_dirty"] = True
            st.toast(f"✅ {count} approuvées (confiance ≥ 0.95)", icon="🎉")

    with bulk_col3:
        if st.button(
            "❌ Tout rejeter par défaut",
            help="Mets decision=reject sur tous les duplicates.",
            use_container_width=True,
        ):
            _bulk_set_decisions(registry, "*", "reject")
            st.session_state["dup_dirty"] = True
            st.toast("❌ Tous rejetés par défaut", icon="⚠️")

    with bulk_col4:
        if st.button(
            "🔄 Tout remettre en attente",
            help="Réinitialise toutes les décisions à pending.",
            use_container_width=True,
        ):
            _bulk_set_decisions(registry, "*", "pending")
            st.session_state["dup_dirty"] = True
            st.toast("🔄 Tout remis en attente", icon="🔄")

    st.divider()

    # ========================================================================
    # Vue principale : cartes par CodeList
    # ========================================================================
    st.subheader("🗂️ Doublons par CodeList")
    st.caption("Cliquez sur une CodeList pour voir les détails et valider chaque duplicate.")

    # --- Filtres ---
    filter_col1, filter_col2, filter_col3 = st.columns(3)
    with filter_col1:
        decision_filter = st.multiselect(
            "Décision",
            options=["approve", "reject", "pending"],
            default=["approve", "reject", "pending"],
            format_func=lambda x: {
                "approve": "✅ Approuvé",
                "reject": "❌ Rejeté",
                "pending": "⏳ En attente",
            }[x],
        )
    with filter_col2:
        cl_name_search = st.text_input("🔎 Rechercher dans le nom…", placeholder="Nom de la CodeList")
    with filter_col3:
        n_per_page = st.select_slider(
            "CodeLists affichées",
            options=[10, 20, 50, 100, len(registry)],
            value=min(50, len(registry)),
        )

    # Filtrer l'ordre de tri par nombre de duplicates
    cl_list: list[tuple[str, dict]] = sorted(
        registry.items(),
        key=lambda kv: len(kv[1].get("duplicates", [])),
        reverse=True,
    )

    # Appliquer les filtres
    filtered: list[tuple[str, dict]] = []
    for cl_id, cl_data in cl_list:
        # Filtre par recherche nom
        cl_name = (cl_data.get("name") or cl_id[:10]).lower()
        if cl_name_search and cl_name_search.lower() not in cl_name:
            # Chercher aussi dans les duplicates
            dup_names = [
                (d.get("name") or d.get("id", ""))
                for d in cl_data.get("duplicates", [])
            ]
            if not any(cl_name_search.lower() in n.lower() for n in dup_names):
                continue

        # Vérifier au moins un duplicate valide pour le filtre decision
        has_matching = any(
            d.get("decision") in decision_filter
            for d in cl_data.get("duplicates", [])
        )
        if not has_matching:
            continue

        filtered.append((cl_id, cl_data))

    # Appliquer le pagination après filtrage
    filtered = filtered[:n_per_page]

    if not filtered:
        st.info("Aucun duplicate ne correspond aux filtres sélectionnés.")
    else:
        for cl_id, cl_data in filtered:
            with st.expander(
                f"🗂️ {cl_data.get('name', cl_id)} — {len(cl_data.get('duplicates', []))} duplicate(s)",
                expanded=True,
            ):
                # Header de la CL
                header_cols = st.columns([2, 1.5, 1.5, 1])
                with header_cols[0]:
                    st.write(f"**{cl_data.get('name', cl_id)}**")
                    if cl_data.get("label"):
                        st.caption(f"`{cl_data['label']}`")
                with header_cols[1]:
                    st.metric("Codes", cl_data.get("codes_count", len(cl_data.get("codes", []))))
                with header_cols[2]:
                    vars_list = cl_data.get("vars", [])
                    st.write(f"**{len(vars_list)}** variable(s)")
                    if vars_list:
                        st.caption(f"{', '.join(vars_list[:3])}{('…' if len(vars_list) > 3 else '')}")
                with header_cols[3]:
                    cat_ids = cl_data.get("cat_ids", [])
                    st.write(f"**{len(cat_ids)}** catégorie(s)")

                st.divider()

                # --- Duplicates de cette CL ---
                duplicates = cl_data.get("duplicates", [])

                if not duplicates:
                    st.caption("*Aucun duplicate détecté pour cette CodeList.*")
                    continue

                for dup in duplicates:
                    _render_duplicate_card(cl_id, dup, registry_path, registry)

    st.divider()

    # --- Sauvegarde ---
    st.subheader("💾 Sauvegarder")

    save_col1, save_col2 = st.columns(2)

    with save_col1:
        # Export JSON brut pour téléchargement
        export_data = json.dumps(
            registry, indent=2, ensure_ascii=False
        )
        st.download_button(
            label="⬇️ Télécharger codelist_duplicates.json",
            data=export_data,
            file_name="codelist_duplicates.json",
            mime="application/json",
            use_container_width=True,
        )

    with save_col2:
        if st.session_state.get("dup_dirty", False):
            btn_remote_save = st.button(
                "💾 Écrire sur le fichier (S3 / Local)",
                help=f"Écrira le registre sur : {registry_path}",
                type="primary",
                use_container_width=True,
            )
            if btn_remote_save:
                try:
                    write_json(registry, registry_path)
                    st.session_state["dup_dirty"] = False
                except Exception as e:
                    st.error(f"❌ Erreur de sauvegarde : {e}")
        else:
            st.caption(f"Non modifié — rien à sauvegarder (dernière écriture : {registry_path})")

    st.divider()
    st.caption(
        f"Source : `codelist_duplicates.json` — {registry_path} | "
        f"{total_cls} CodeLists | {total_dups} doublons détectés | "
        f"{n_approved} approuvés | {n_rejected} rejetés | {n_pending_val} en attente\n"
        f"💡 Les modifications sont sauvegardées automatiquement à chaque changement."
    )


# ---------------------------------------------------------------------------
# Helpers pour la validation des doublons
# ---------------------------------------------------------------------------

_DECISION_LABELS = {
    "approve": "✅ Approuvé",
    "reject": "❌ Rejeté",
    "pending": "⏳ En attente",
}


def _on_dup_decision_change(**kwargs) -> None:
    """CB on_change du selectbox decision — écrit dans le registre + session_state."""
    key = kwargs["key"]
    cl_id = kwargs["cl_id"]
    dup_id = kwargs["dup_id"]
    registry: dict = kwargs["registry"]
    val = st.session_state.get(f"decision_{key}")
    if val:
        for cl in registry.values():
            if cl.get("id") == cl_id:
                for dup in cl.get("duplicates", []):
                    if dup.get("id") == dup_id:
                        dup["decision"] = val
                        break

        st.session_state["dup_dirty"] = True


def _render_code_comparison(
    src_codes: list[list[str]],
    dst_codes: list[list[str]],
    src_info: dict,
    dst_info: dict,
) -> None:
    """Tableau comparatif de codes : Valeur P | Étiquette P | Valeur D | Étiquette D.

    3 sections : matching (clefs communes), uniquement parent, uniquement duplicate.
    Normalise les clefs en lowercase pour le matching, mais affiche les originaux.
    """

    # ── 1. Maps value_orig → label ET key_lower → value_orig pour matching case-insensitive ──
    src_map: dict[str, str] = {}        # "CF" → "Voir dictionnaire..."
    src_map_lower: dict[str, str] = {}  # "cf" → "CF" (clef d'origine)
    for pair in src_codes:
        raw = str(pair[0])
        lbl = str(pair[1]) if len(pair) > 1 else ""
        src_map[raw] = lbl
        src_map_lower[raw.lower()] = raw

    dst_map: dict[str, str] = {}
    dst_map_lower: dict[str, str] = {}
    for pair in dst_codes:
        raw = str(pair[0])
        lbl = str(pair[1]) if len(pair) > 1 else ""
        dst_map[raw] = lbl
        dst_map_lower[raw.lower()] = raw

    # ── 2. Matching case-insensitive ──
    common_lower: set[str] = set(src_map_lower) & set(dst_map_lower)
    only_src_lower: set[str] = set(src_map_lower) - set(dst_map_lower)
    only_dst_lower: set[str] = set(dst_map_lower) - set(src_map_lower)

    rows: list[dict] = []

    for k_lower in sorted(common_lower):
        k_src = src_map_lower[k_lower]
        k_dst = dst_map_lower[k_lower]
        rows.append(
            {
                "Valeur P": k_src,
                "Étiquette P": src_map[k_src],
                "Valeur D": k_dst,
                "Étiquette D": dst_map[k_dst],
                "section": 0,
            },
        )

    for k_lower in sorted(only_src_lower):
        k_src = src_map_lower[k_lower]
        rows.append(
            {
                "Valeur P": k_src,
                "Étiquette P": src_map[k_src],
                "Valeur D": "",
                "Étiquette D": "",
                "section": 1,
            },
        )

    for k_lower in sorted(only_dst_lower):
        k_dst = dst_map_lower[k_lower]
        rows.append(
            {
                "Valeur P": "",
                "Étiquette P": "",
                "Valeur D": k_dst,
                "Étiquette D": dst_map[k_dst],
                "section": 2,
            },
        )

    # ── 3. Trier par section puis par valeur ──
    rows_sorted = sorted(rows, key=lambda r: (
        r.get("section", 0),
        r.get("Valeur P") or r.get("Valeur D") or "",
    ))

    # ── 4. DataFrame avec colonnes tronquées ──
    df_codes = pd.DataFrame(
        [
            {
                "Valeur P": r.get("Valeur P", ""),
                "Étiquette P": (r.get("Étiquette P", "") or "")[:60],
                "Valeur D": r.get("Valeur D", ""),
                "Étiquette D": (r.get("Étiquette D", "") or "")[:60],
            }
            for r in rows_sorted
        ]
    )

    # Fix height=0 si aucun row → éviter division par zéro
    height = max(100, min(400, len(rows_sorted) * 28)) if rows_sorted else 100

    st.dataframe(
        df_codes,
        use_container_width=True,
        hide_index=True,
        height=height,
    )

    # ── 5. Résumé ──
    n_common = len(common_lower)
    n_only_src = len(only_src_lower)
    n_only_dst = len(only_dst_lower)
    summary_parts = [f"📥 {n_common} matching"]
    if n_only_src:
        summary_parts.append(f"{n_only_src} uniquement {src_info.get('name', src_info.get('id', '?'))}")
    if n_only_dst:
        summary_parts.append(f"{n_only_dst} uniquement {dst_info.get('name', dst_info.get('id', '?'))}")
    st.caption(" | ".join(summary_parts))


def _render_duplicate_card(
    cl_id: str,
    dup: dict,
    registry_path: str,
    registry: dict,
) -> None:
    """Rend une carte expandable pour un duplicate."""
    dup_decision = dup.get("decision", "pending")
    dup_id = dup.get("id", "")
    dup_name = dup.get("name", "Inconnu")
    detection_types = dup.get("detection_types", [])
    confidence = dup.get("confidence", 0.0)
    codes_count = dup.get("codes_count", 0)
    codes = dup.get("codes", [])
    vars_list = dup.get("vars", [])
    cat_ids = dup.get("cat_ids", [])

    # Badge type
    types_str = " | ".join(
        f"{_BADGE_COLORS.get(t, '⚪')} {t}" for t in sorted(detection_types)
    )

    st.divider()

    with st.expander(
        f"🆚 **{dup_name}** — {_badge_decision(dup_decision)} {dup_decision}  "
        f"({types_str}, conf={confidence:.3f}, {codes_count} codes)",
        expanded=True,
    ):
        # Header infos
        info_cols = st.columns([2, 1.5, 1.5, 1])
        with info_cols[0]:
            st.write(f"`{dup.get('id', dup_id)}`")
            if dup.get("label"):
                st.caption(f"`{dup['label']}`")
        with info_cols[1]:
            st.metric("Codes", codes_count)
        with info_cols[2]:
            st.write(f"**{len(vars_list)}** variable(s)")
            if vars_list:
                st.caption(f"{', '.join(vars_list[:3])}{('…' if len(vars_list) > 3 else '')}")
        with info_cols[3]:
            st.write(f"**{len(cat_ids)}** catégorie(s)")

        # Sélecteur decision (auto-save via on_change)
        key = f"dec_{cl_id}_{dup_id}"
        decision_index = 2  # default pending
        if dup_decision == "approve":
            decision_index = 0
        elif dup_decision == "reject":
            decision_index = 1
        st.selectbox(
            "Sélectionner une décision :",
            options=["approve", "reject", "pending"],
            index=decision_index,
            key=f"decision_{key}",
            on_change=_on_dup_decision_change,
            kwargs={
                "key": key, "cl_id": cl_id, "dup_id": dup_id,
                "path": registry_path, "registry": registry,
            },
            format_func=lambda x: _DECISION_LABELS.get(x, x),
            label_visibility="collapsed",
            help="Approuver / Rejeter / Laisser en attente ce duplicate.",
        )

        # --- Section codes comparés ---
        src_codes = registry.get(cl_id, {}).get("codes", [])
        if src_codes and codes:
            with st.expander("🆚 Comparaison des codes", expanded=False):
                _render_code_comparison(src_codes, codes, registry.get(cl_id, {}), dup)

        # --- Variables ---
        if vars_list:
            with st.expander(f"🔗 Variables associées ({len(vars_list)})", expanded=False):
                for v in vars_list[:20]:
                    st.code(v)
                if len(vars_list) > 20:
                    st.caption(f"... et {len(vars_list) - 20} autres")


def _bulk_set_decisions(
    registry: dict,
    criteria: str,
    action: str,
) -> int:
    """Modifie massivement les decisions dans le registre.

    Args:
        registry: Le registre chargé en session_state.
        criteria: "exact", "≥ 0.95", "*" (tout).
        action: "approve" | "reject" | "pending"

    Returns:
        Nombre de duplicates modifiés.
    """
    count = 0
    for cl_id, cl_data in registry.items():
        for dup in cl_data.get("duplicates", []):
            should = False
            if criteria == "*":
                should = True
            elif criteria == "exact":
                should = "exact" in dup.get("detection_types", [])
            elif "≥ 0.95" in criteria:
                should = dup.get("confidence", 0) >= 0.95

            if should:
                dup["decision"] = action
                count += 1
    return count



def render_pipeline_tab() -> None:
    """Interface de lancement du pipeline et affichage des résultats."""

    st.header("🚀 Lancer le pipeline")
    st.caption(
        "Saisissez le chemin du fichier DDI (XML) et le dossier de sortie. "
        "Le pipeline s'exécute de façon synchrone — l'interface attend la fin."
    )

    with st.form("pipeline_form"):
        # --- Formulaire : paramètres du pipeline ---
        xml_source = st.text_input(
            "Chemin DDI (XML) — local ou S3",
            value="",
            placeholder="s3://<bucket>/data/input/dde-metadonnees.xml",
            help="URL S3 ou chemin local vers le fichier DDI à analyser.",
            key="pipeline_xml_source",
        )
        output_base = st.text_input(
            "Dossier de sortie — local ou S3",
            value="s3://",
            placeholder="s3://<bucket>/output/scrubber",
            help="S3 : écrit les outputs sur le bucket. Local : dossier temporaire.",
            key="pipeline_output_base",
        )
        run_llm = st.checkbox(
            "Exécuter les phases LLM / embeddings",
            value=True,
            help="Inclure la détection sémantique (embeddings + juge LLM).",
            key="pipeline_run_llm",
        )
        verbose = st.checkbox(
            "Mode verbeux",
            value=False,
            help="Afficher les détails des embeddings et des appels LLM.",
            key="pipeline_verbose",
        )

        submitted = st.form_submit_button(
            "🚀 Lancer le pipeline",
            type="primary",
            use_container_width=True,
        )

    # ------------------------------------------------------------------
    # Si l'utilisateur ré-entre sur la page (après un st.rerun() interne),
    # on ne doit plus repasser par le formulaire.
    # ------------------------------------------------------------------
    if "pipeline_data" not in st.session_state:
        if not submitted:
            return

        # --- Validation des paramètres ---
        if not xml_source.strip():
            st.error("❌ Veuillez saisir un chemin DDI (local ou S3).")
            return
        if not output_base.strip():
            st.error("❌ Veuillez saisir un chemin de sortie (local ou S3).")
            return

        _render_execute_pipeline(
            xml_source, output_base, run_llm, verbose,
        )
    else:
        # Le pipeline a déjà été lancé — on affiche simplement son état
        _render_execute_pipeline(
            st.session_state["pipeline_data"]["xml_source"],
            st.session_state["pipeline_data"]["output_base"],
            st.session_state["pipeline_data"]["run_llm"],
            st.session_state["pipeline_data"]["verbose"],
        )


# ---------------------------------------------------------------------------
# Exécution asynchrone du pipeline (thread + queue + polling main thread)
# ---------------------------------------------------------------------------


def _poll_queue(queue_obj: queue.Queue, data: dict) -> None:
    """Poll toutes les messages d'une ``queue.Queue`` et met à jour *data*.

    Cette fonction s'exécute **toujours dans le thread principal** (celui de
    Streamlit).  Elle est appelée à chaque *rerun* pour transformer la queue
    brute en state facile à consommer par l'UI.
    """
    while True:
        try:
            type_, msg, prog, phase = queue_obj.get_nowait()
        except queue.Empty:
            break  # plus rien dans la queue

        if type_ == "phase":
            data["status"] = "running"
            data["phase"] = phase
            data["progress"] = prog if prog is not None else 0.0
            data["current_log"] = msg
            data["logs_accum"].append(msg)
        elif type_ == "log":
            data["logs_accum"].append(str(msg))
        elif type_ == "done":
            data["status"] = "success"
            data["progress"] = 1.0
            data["current_log"] = str(msg)
        elif type_ == "error":
            data["status"] = "error"
            data["error_message"] = str(msg)
        elif type_ == "result":
            if msg is not None:
                data["result"] = msg


def _render_execute_pipeline(
    xml_source: str,
    output_base: str,
    run_llm: bool,
    verbose: bool,
) -> None:
    """Affiche le statut du pipeline et le met à jour en mode non-bloquant.

    Utilise ``session_state`` + ``st.rerun()`` pour garder l'UI vivante
    pendant l'exécution.  La *queue* retournée par ``run_pipeline_async``
    est pollée à chaque rerun dans le thread principal Streamlit
    (le seul endroit où on a le droit de modifier ``session_state``).
    """
    state_key = "pipeline_data"

    # ------------------------------------------------------------------
    # État initial : aucun pipeline en cours et utilisateur a soumit
    # ------------------------------------------------------------------
    if state_key not in st.session_state or st.session_state[state_key].get("running_thread") is None:
        st.session_state[state_key] = {
            "xml_source": xml_source,
            "output_base": output_base,
            "run_llm": run_llm,
            "verbose": verbose,
            "status": "init",
            "progress": 0.0,
            "phase": None,
            "current_log": "Initialisation…",
            "logs_accum": [],
            "result": None,
            "error_message": None,
        }

    data = st.session_state[state_key]

    # ------------------------------------------------------------------
    # Lancer le thread SI aucun n'est en cours ET qu'on vient de cliquer
    # (on le détecte en vérifiant que status == "init" et pas de thread).
    # ------------------------------------------------------------------
    if data["status"] == "init":
        try:
            thread, q = run_pipeline_async(
                xml_source=xml_source,
                output_base=output_base,
                run_llm=run_llm,
                verbose=verbose,
            )
            data["running_thread"] = thread
            data["msg_queue"] = q  # reference nécessaire pour le polling
        except Exception as exc:
            data["status"] = "error"
            data["error_message"] = str(exc)
            st.error(f"❌ Erreur au lancement : {exc}")
            return

    # ------------------------------------------------------------------
    # Polling de la queue (thread principal uniquement)
    # ------------------------------------------------------------------
    q = data.get("msg_queue")
    if q is not None:
        _poll_queue(q, data)

    status = data.get("status", "init")

    # =========================================================================
    # Rendu selon le statut
    # =========================================================================

    status_box = st.empty()
    progress_bar = st.progress(
        data.get("progress", 0.0),
        text=data.get("current_log", "Initialisation…"),
    )

    current_phase = data.get("phase", None)
    phase_names = {
        0: "Lancement …",
        1: "Lecture & parsing XML …",
        2: "Extraction CodeLists …",
        3: "Extraction variables …",
        4: "Signature de contenu …",
        5: "Détection exacte …",
        6: "Détection floue …",
        7: "Signaux d'usage …",
        8: "Détection sémantique …",
        9: "Génération du registre …",
    }
    phase_label = phase_names.get(current_phase, f"Phase {current_phase}") if current_phase else ""

    with status_box:
        if status == "running":
            st.info(f"🔄 Pipeline en cours — **{phase_label}**")
        elif status == "success":
            st.success(f"✅ Pipeline terminé — {progress_bar.info_text}")
        elif status == "error":
            st.error(f"❌ Erreur : {data.get('error_message', 'inconnue')}")

    # --- Logs en direct (pendant execution) ---
    if status == "running" and data.get("logs_accum"):
        with st.expander("📋 Logs en direct", expanded=True):
            st.code("\n".join(data["logs_accum"]), language="text")
    elif status in ("success", "error") and data.get("logs_accum"):
        with st.expander("📋 Logs d'exécution"):
            st.code("\n".join(data["logs_accum"]), language="text")

    # --- Boucle d'attente si pipeline en cours ---
    if status == "running":
        thread = data.get("running_thread")
        if thread and thread.is_alive():
            st.rerun()
        else:
            # Thread fini mais callback pas encore passé par "result"
            data["progress"] = 1.0
            if "current_log" not in data:
                data["current_log"] = "Terminé"
            st.rerun()

    # --- Affichage résultat : success ---
    if status == "success" and "result" in data:
        result = data["result"]  # PipelineResult
        st.divider()
        _render_pipeline_success(result, output_base)
        data["running_thread"] = None

    # --- Affichage résultat : error ---
    if status == "error" and data.get("error_message"):
        st.divider()
        st.error(f"❌ Erreur pipeline : {data['error_message']}")
        if data.get("logs_accum"):
            with st.expander("📋 Logs (échec)"):
                st.code("\n".join(data["logs_accum"]), language="text")

    # --- Reset bouton ---
    if status in ("success", "error"):
        st.divider()
        col_reset, _ = st.columns([1, 3])
        if col_reset.button("🗑️ Réinitialiser le pipeline", use_container_width=True):
            st.session_state["pipeline_data"] = {}
            st.rerun()


def _render_pipeline_success(
    result: PipelineResult,
    output_base: str,
) -> None:
    """Affiche les résultats d'un pipeline réussi."""

    # --- Stats ---
    st.subheader("📊 Résultats")
    stats_col1, stats_col2, stats_col3 = st.columns(3)
    with stats_col1:
        st.metric("Durée", f"{result.duration_seconds:.1f}s")
    with stats_col2:
        has_outputs = result.output_files and len(result.output_files) > 0
        st.metric("Fichiers produits", len(result.output_files) if has_outputs else 0)
    with stats_col3:
        st.metric("Sortie principale", "codelist_duplicates.json")

    # --- Pré-remplissage onglet Validation ---
    if result.output_files:
        st.divider()

        dup_url = result.output_files.get("codelist_duplicates.json") or ""
        if dup_url:
            st.session_state["_auto_registry"] = dup_url

        st.info(
            "👉 Vous pouvez maintenant aller dans l'onglet **"
            "🔍 Validation des doublons** "
            "pour consulter et valider les résultats. Le registre a été pré-chargé."
        )

        with st.form("go_to_validation"):
            if st.form_submit_button(
                "🔍 Aller à l'onglet Validation",
                type="primary",
                use_container_width=True,
            ):
                st.rerun()

    # --- Fichiers de sortie ---
    if result.output_files:
        st.divider()
        st.subheader("📁 Fichiers de sortie")

        for fname, fpath in result.output_files.items():
            with st.expander(f"📄 {fname} — {fpath}", expanded=False):
                st.code(fpath, language="text")

    # --- Logs ---
    st.divider()
    st.subheader("📋 Logs d'exécution")

    # --- Logs ---
    with st.expander("Afficher les logs", expanded=False):
        st.code(result.logs, language="text")


# ---------------------------------------------------------------------------
# Entry point — tabbed main
# ---------------------------------------------------------------------------


def main() -> None:
    st.set_page_config(page_title="DDI Scrubber", layout="wide", page_icon="🔍")

    tabs = st.tabs(["🚀 Pipeline", "🔍 Validation des doublons"])

    with tabs[0]:
        render_pipeline_tab()

    with tabs[1]:
        render_duplicates_tab()


if __name__ == "__main__":
    main()
