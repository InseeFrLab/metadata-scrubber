/* cleaned.js — Édition du registre des CodeLists nettoyées (onglet 3)

   Le registre nettoyé est alimenté par la validation des doublons (sync
   côté serveur) et éditable ici : renommer, modifier/ajouter/supprimer des
   codes (valeur + libellé/catégorie), supprimer des entrées. Les `replaces`
   (identifiants des listes remplacées) sont en lecture seule — leur cycle
   de vie appartient au sync de validation.

   Réutilise escapeHtml/copyId (registry.js) et showToast (main.js). */

let cleanedDoc = null;
let cleanedDirty = false;

/* ------------------------------------------------------------------ */
/* Chargement / sauvegarde / téléchargement
/* ------------------------------------------------------------------ */

async function loadCleaned() {
    const path = document.getElementById('cleanedPath').value.trim();
    if (!path) {
        showToast('Veuillez saisir le chemin du registre nettoyé.', 'warning');
        return;
    }
    try {
        const resp = await fetch(`${API_BASE}api/cleaned?path=${encodeURIComponent(path)}`);
        if (resp.status === 404) {
            showToast('Fichier introuvable — validez des doublons puis sauvegardez d\'abord.', 'warning');
            return;
        }
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        cleanedDoc = await resp.json();
        setCleanedDirty(false);
        renderCleaned();
        showToast(`✅ Registre nettoyé chargé · ${Object.keys(cleanedDoc.codelists || {}).length} entrées`, 'success');
    } catch (err) {
        console.error('Chargement registre nettoyé:', err);
        showToast(`Erreur: ${err.message}`, 'danger');
    }
}

async function saveCleaned() {
    if (!cleanedDoc) {
        showToast('Aucun registre nettoyé chargé.', 'warning');
        return;
    }
    const path = document.getElementById('cleanedPath').value.trim();
    if (!path) {
        showToast('Aucun chemin de destination.', 'warning');
        return;
    }
    try {
        const resp = await fetch(
            `${API_BASE}api/cleaned/save?path=${encodeURIComponent(path)}`,
            {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(cleanedDoc),
            }
        );
        if (resp.status === 422) {
            const data = await resp.json();
            const details = Array.isArray(data.detail) ? data.detail.join(' · ') : data.detail;
            showToast(`Validation échouée: ${details}`, 'danger');
            return;
        }
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        const data = await resp.json();
        setCleanedDirty(false);
        showToast(`💾 Registre nettoyé sauvegardé · ${data.count} entrées`, 'success');
    } catch (err) {
        showToast(`Erreur: ${err.message}`, 'danger');
    }
}

function downloadCleanedFile() {
    if (!cleanedDoc) {
        showToast('Aucun registre nettoyé chargé.', 'warning');
        return;
    }
    const blob = new Blob([JSON.stringify(cleanedDoc, null, 2)], { type: 'application/json' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = 'cleaned_codelists.json';
    a.click();
    URL.revokeObjectURL(url);
    showToast('⬇️ Téléchargement lancé', 'info');
}

function setCleanedDirty(dirty) {
    cleanedDirty = dirty;
    document.getElementById('cleanedDirty').classList.toggle('d-none', !dirty);
}

/* ------------------------------------------------------------------ */
/* Rendu
/* ------------------------------------------------------------------ */

function renderCleaned() {
    const container = document.getElementById('cleanedContainer');
    const toolbar = document.getElementById('cleanedToolbar');
    const emptyState = document.getElementById('cleanedEmptyState');

    if (!cleanedDoc) {
        toolbar.classList.add('d-none');
        emptyState.classList.remove('d-none');
        container.innerHTML = '';
        return;
    }
    toolbar.classList.remove('d-none');
    emptyState.classList.add('d-none');

    // Pré-charge les codes de replace depuis registryData
    _preloadReplaceCaches();

    const entries = Object.values(cleanedDoc.codelists || {});
    document.getElementById('cleanedCount').textContent =
        `${entries.length} entrée${entries.length > 1 ? 's' : ''}`;

    const search = document.getElementById('cleanedSearch').value.trim().toLowerCase();
    let shown = entries;
    if (search) {
        shown = entries.filter(e =>
            (e.name || '').toLowerCase().includes(search) ||
            (e.label || '').toLowerCase().includes(search) ||
            (e.id || '').toLowerCase().includes(search)
        );
    }
    shown = [...shown].sort((a, b) => (a.name || '').localeCompare(b.name || ''));

    if (!shown.length) {
        container.innerHTML = '<div class="text-center py-4 text-muted">Aucune entrée.</div>';
        return;
    }
    container.innerHTML = shown.map(renderCleanedEntry).join('');
}

function _confidenceTag(confidence) {
    if (confidence == null) return '';
    const pct = Math.round(confidence * 100);
    const cls = confidence >= 0.8 ? 'confidence-high'
              : confidence >= 0.5 ? 'confidence-medium'
              : 'confidence-low';
    return `<span class="confidence-tag badge ${cls}">${pct}%</span>`;
}

function renderCleanedEntry(entry) {
    const id = escapeHtml(entry.id);
    const replaces = entry.replaces || [];

    let html = `<div class="card mb-3" data-cl-id="${id}">`;

    // Header : nom + label éditables, id copiable, suppression
    html += `<div class="card-header">`;
    html += `<div class="row g-2 align-items-center">`;
    html += `<div class="col-md-4"><input type="text" class="form-control form-control-sm" `;
    html += `data-field="name" value="${escapeHtml(entry.name || '')}" placeholder="Nom"></div>`;
    html += `<div class="col-md-4"><input type="text" class="form-control form-control-sm" `;
    html += `data-field="label" value="${escapeHtml(entry.label || '')}" placeholder="Libellé"></div>`;
    html += `<div class="col-md-4 text-end">`;
    html += `<a class="copy-id-btn" onclick="copyId('${id}')" title="Copier l'id">${id.substring(0, 12)}…</a> `;
    html += `<span class="badge bg-secondary ms-1">${(entry.codes || []).length} codes</span> `;
    html += `<button class="btn btn-sm btn-outline-danger ms-1" data-action="delete-entry" title="Supprimer l'entrée">🗑️</button>`;
    html += `</div></div></div>`;

    // ID complet (copiable) — directement sous le header
    html += `<div class="px-3 pb-2 small"><span class="text-muted">ID : </span>`;
    html += `<code class="font-monospace small">${id}</code>`;
    html += ` <a class="copy-id-btn" onclick="copyId('${id}')" title="Copier l'ID">📋</a></div>`;

    html += `<div class="card-body py-2">`;

    // Replaces pliables (lecture seule, contenu injecté au render)
    if (replaces.length) {
        html += '<div class="mb-2">';
        for (let r = 0; r < replaces.length; r++) {
            const rep = replaces[r];
            const rid = (rep.id || '').replace(/'/g, "\\'");
            const ridEsc = escapeHtml(rep.id || '');
            const rname = escapeHtml(rep.name || '');
            const rnameShort = rname.length > 20
                ? rname.slice(0, 20) + '…'
                : rname;
            const collapsibleId = `replace-detail-cl-${rep.id}`;
            const codes = _replacedCache[rep.id];
            const tableHTML = _buildReplaceTableHTML(codes, rep.id);
            const summaryLabel = tableHTML
                ? `${rnameShort} — <span class="replace-codes-count">${(codes || []).length} codes</span>`
                : `${rnameShort} — <span class="replace-codes-count">non disponible</span>`;

            html += `<div class="replace-badge badge bg-primary text-white me-1 mb-1" `;
            html += `data-replace-id="${ridEsc}" role="button" tabindex="0" `;
            html += `title="${ridEsc}" `;
            html += `onclick="toggleReplaceCodes('${rid}', '${collapsibleId}')">`;
            html += `📘 ${rnameShort}`;
            html += _confidenceTag(rep.confidence);
            html += '</div>';

            // Collapsible : fermé par défaut, contenu déjà injecté
            html += `<details class="replace-collapsible" id="${collapsibleId}" `;
            html += `data-replace-id="${ridEsc}">`;
            html += `<summary class="replace-summary" data-replace-id="${ridEsc}">`;
            html += `<span>${summaryLabel}</span></summary>`;
            html += '<div class="replace-body">';
            if (tableHTML) {
                html += `<div class="replace-content">${tableHTML}</div>`;
            } else {
                html += '<div class="replace-not-loaded">';
                html += '<p>Ce registre n\'a pas été chargé depuis l\'onglet Validation.</p>';
                html += '<button class="btn btn-sm btn-warning js-switch-to-validation" ';
                html += 'title="Aller à l\'onglet Validation">➡️ Aller à Validation</button>';
                html += '</div>';
            }
            html += '</div></details>';
        }
        html += '</div>';
    }

    // Table de codes éditable
    html += `<table class="table table-sm table-bordered code-table mb-1"><thead><tr>`;
    html += `<th style="width:25%">Valeur</th><th>Libellé / Catégorie</th><th style="width:40px"></th>`;
    html += `</tr></thead><tbody>`;
    (entry.codes || []).forEach((pair, idx) => {
        html += `<tr>`;
        html += `<td><input type="text" class="form-control form-control-sm" `;
        html += `data-field="code-value" data-code-idx="${idx}" value="${escapeHtml(pair[0] ?? '')}"></td>`;
        html += `<td><input type="text" class="form-control form-control-sm" `;
        html += `data-field="code-label" data-code-idx="${idx}" value="${escapeHtml(pair[1] ?? '')}"></td>`;
        html += `<td class="text-center"><button class="btn btn-sm btn-outline-danger" `;
        html += `data-action="delete-code" data-code-idx="${idx}" title="Supprimer">✖</button></td>`;
        html += `</tr>`;
    });
    html += `</tbody></table>`;
    html += `<button class="btn btn-sm btn-outline-success" data-action="add-code">➕ Ajouter un code</button>`;

    if (entry.updated_at) {
        html += `<div class="small text-muted mt-2">Mis à jour : ${escapeHtml(entry.updated_at)}</div>`;
    }
    html += `</div></div>`;
    return html;
}

/* ------------------------------------------------------------------ */
/* Édition (listeners délégués)
/* ------------------------------------------------------------------ */

function _cleanedEntryFromEvent(ev) {
    const card = ev.target.closest('[data-cl-id]');
    if (!card || !cleanedDoc) return [null, null];
    return [card.dataset.clId, cleanedDoc.codelists[card.dataset.clId]];
}

function handleCleanedInput(ev) {
    const [, entry] = _cleanedEntryFromEvent(ev);
    if (!entry) return;
    const field = ev.target.dataset.field;
    if (!field) return;

    if (field === 'name' || field === 'label') {
        entry[field] = ev.target.value;
    } else if (field === 'code-value' || field === 'code-label') {
        const idx = parseInt(ev.target.dataset.codeIdx, 10);
        if (!entry.codes || !entry.codes[idx]) return;
        entry.codes[idx][field === 'code-value' ? 0 : 1] = ev.target.value;
    } else {
        return;
    }
    setCleanedDirty(true);
}

function handleCleanedClick(ev) {
    const btn = ev.target.closest('[data-action]');
    if (btn) {
        const [clId, entry] = _cleanedEntryFromEvent(ev);
        if (!entry) return;
        return _handleCleanedAction(btn, clId, entry);
    }
    // Boutons copy-replace-codes (dans les collapsibles)
    const copyBtn = ev.target.closest('.copy-replace-codes');
    if (copyBtn) {
        ev.preventDefault();
        const rid = copyBtn.dataset.replaceId;
        if (rid) copyReplaceCodes(rid);
        return;
    }
    // Bouton switch-to-validation (fallback)
    const switchBtn = ev.target.closest('.js-switch-to-validation');
    if (switchBtn) {
        ev.preventDefault();
        document.getElementById('validationTab').click();
        return;
    }
}

function _handleCleanedAction(btn, clId, entry) {
    const action = btn.dataset.action;
    if (action === 'delete-entry') {
        if (!confirm(`Supprimer l'entrée « ${entry.name || clId} » du registre nettoyé ?`)) return;
        delete cleanedDoc.codelists[clId];
        setCleanedDirty(true);
        renderCleaned();
    } else if (action === 'add-code') {
        entry.codes = entry.codes || [];
        entry.codes.push(['', '']);
        setCleanedDirty(true);
        renderCleaned();
    } else if (action === 'delete-code') {
        const idx = parseInt(btn.dataset.codeIdx, 10);
        entry.codes.splice(idx, 1);
        setCleanedDirty(true);
        renderCleaned();
    }
}

/* ------------------------------------------------------------------ */
/* Cache & index de lookup des remplacements
/* ------------------------------------------------------------------ */

/**
 * Cache { [rid]: Array<[value, label]> } construit au chargement du registre.
 */
let _replacedCache = {};

/** Index O(1) ID → objet duplicate depuis registryData. */
let _dupIndex = null;

function _resetCache() { _replacedCache = {}; _dupIndex = null; }

/** Construit (si nécessaire) un index ID → duplicate complet depuis registryData. */
function _ensureDupIndex() {
    if (_dupIndex) return _dupIndex;
    _dupIndex = new Map();
    if (registryData) {
        for (const _pid in registryData) {
            const parent = registryData[_pid];
            const dups = parent && parent.duplicates ? parent.duplicates : [];
            for (let i = 0; i < dups.length; i++) {
                const dup = dups[i];
                if (dup && dup.id) _dupIndex.set(dup.id, dup);
            }
        }
    }
    return _dupIndex;
}

/** Cherche un duplicate depuis registryData par son ID. */
function _findInRegistry(rid) {
    if (!registryData || typeof registryData !== 'object') return null;
    const idx = _ensureDupIndex();
    return idx.get(rid) || null;
}

/** Pré-charge TOUS les codes de replace depuis registryData. */
function _preloadReplaceCaches() {
    _resetCache();
    if (!cleanedDoc || !cleanedDoc.codelists) return;
    for (const _key in cleanedDoc.codelists) {
        const entry = cleanedDoc.codelists[_key];
        const replaces = entry.replaces || [];
        for (let r = 0; r < replaces.length; r++) {
            const rid = replaces[r].id;
            if (!rid || _replacedCache[rid]) continue;
            const dup = _findInRegistry(rid);
            if (dup && dup.codes) _replacedCache[rid] = dup.codes;
        }
    }
}

/** Génère le HTML d'une table de codes readonly. Retourne null si aucun code. */
function _buildReplaceTableHTML(codes, rid) {
    if (!codes || codes.length === 0) return null;
    let rows = '';
    for (let i = 0; i < codes.length; i++) {
        const pair = codes[i];
        rows += `<tr>`;
        rows += `<td title="${escapeHtml(pair[0] ?? '')}">${escapeHtml(pair[0] ?? '')}</td>`;
        rows += `<td title="${escapeHtml(pair[1] ?? '')}">${escapeHtml(pair[1] ?? '')}</td>`;
        rows += `</tr>`;
    }
    return (
        `<table class="table table-sm table-bordered replace-code-table mb-2">` +
        `<thead><tr><th style="width:30%">Valeur</th><th>Libellé</th></tr></thead>` +
        `<tbody>${rows}</tbody></table>` +
        `<button class="btn btn-sm btn-outline-secondary copy-replace-codes" ` +
        `data-replace-id="${escapeHtml(rid)}" title="Copier tous les codes">` +
        `📋 Copier tous les codes</button>`
    );
}

function copyReplaceCodes(rid) {
    const codes = _replacedCache[rid];
    if (!codes || codes.length === 0) return;
    const text = codes.map(([val, label]) => {
        return `${val}: ${label || ''}`;
    }).join('\n');
    navigator.clipboard.writeText(text).then(() => {
        showToast('✅ Codes copiés dans le presse-papier', 'success');
    }).catch(() => {
        showToast('Erreur lors de la copie.', 'danger');
    });
}

/* ------------------------------------------------------------------ */
/* Toggle des collapsibles (simple — contenu déjà en DOM au render)
/* ------------------------------------------------------------------ */

function toggleReplaceCodes(rid, collapsibleId) {
    const el = document.getElementById(collapsibleId);
    if (!el) return;
    if (el.hasAttribute('open')) {
        el.removeAttribute('open');
    } else {
        el.setAttribute('open', '');
    }
}

/* ------------------------------------------------------------------ */
/* DOM Ready
/* ------------------------------------------------------------------ */

document.addEventListener('DOMContentLoaded', () => {
    document.getElementById('loadCleanedBtn').addEventListener('click', loadCleaned);
    document.getElementById('saveCleanedBtn').addEventListener('click', saveCleaned);
    document.getElementById('downloadCleanedFileBtn').addEventListener('click', downloadCleanedFile);
    document.getElementById('cleanedSearch').addEventListener('input', renderCleaned);

    const container = document.getElementById('cleanedContainer');
    container.addEventListener('input', handleCleanedInput);
    container.addEventListener('click', handleCleanedClick);
});
