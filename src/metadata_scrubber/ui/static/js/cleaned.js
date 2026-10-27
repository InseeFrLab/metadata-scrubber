/* cleaned.js — Référentiel des CodeLists nettoyées (étape 3)

   Le référentiel (registre nettoyé) est alimenté par la validation des
   doublons (sync côté serveur) et éditable ici : un tableau par niveau, et
   un panneau latéral pour modifier une entrée (nom, libellé, niveau, codes)
   ou la supprimer. Les `replaces` (identifiants des listes remplacées) sont
   en lecture seule — leur cycle de vie appartient au sync de validation.
   Les modifications restent locales jusqu'à « Enregistrer ».

   Réutilise escapeHtml, icon, levelTagHtml, copyId (data-copy-id), showToast
   et confirmDialog (main.js). */

let cleanedDoc = null;
let cleanedDirty = false;
let cleanedLevel = 'all';   // onglet de niveau affiché
let editingId = null;       // entrée ouverte dans le panneau latéral
const cleanedSelected = new Set();  // entrées cochées (classement par lot)
let lastCheckedId = null;           // ancre du Maj+clic
let shownIds = [];                  // ordre des lignes affichées

const CLEANED_LEVELS = [
    ['all', 'Toutes'],
    ['officielle', 'Officielles'],
    ['commune', 'Communes'],
    ['specifique', 'Spécifiques'],
    ['none', 'À classer'],
];

/* ------------------------------------------------------------------ */
/* Chargement / sauvegarde / téléchargement
/* ------------------------------------------------------------------ */

/* `quiet` : chargement automatique à l'ouverture de l'onglet (pas de toast) */
async function loadCleaned({ quiet = false } = {}) {
    const path = document.getElementById('cleanedPath').value.trim();
    if (!path) {
        if (!quiet) showToast('Indiquez le fichier du référentiel.', 'warning');
        return;
    }
    if (cleanedDirty && !quiet && !await confirmDialog(
        'Des modifications ne sont pas enregistrées. Recharger le référentiel et les abandonner ?',
        { confirmLabel: 'Recharger', danger: true }
    )) return;
    try {
        const resp = await fetch(`${API_BASE}api/cleaned?path=${encodeURIComponent(path)}`);
        if (resp.status === 404) {
            cleanedDoc = null;
            setCleanedDirty(false);
            renderCleaned();
            if (!quiet) showToast('Pas encore de référentiel à cet emplacement.', 'warning');
            return;
        }
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        cleanedDoc = await resp.json();
        cleanedDoc.codelists = cleanedDoc.codelists || {};
        cleanedSelected.clear();
        setCleanedDirty(false);
        renderCleaned();
        if (!quiet) {
            showToast(`Référentiel chargé · ${Object.keys(cleanedDoc.codelists).length} entrées`, 'success');
        }
    } catch (err) {
        console.error('Chargement référentiel:', err);
        showToast(`Référentiel non chargé : ${err.message}`, 'danger');
    }
}

async function saveCleaned() {
    if (!cleanedDoc) {
        showToast('Aucun référentiel chargé.', 'warning');
        return;
    }
    const path = document.getElementById('cleanedPath').value.trim();
    if (!path) {
        showToast('Aucun fichier de destination.', 'warning');
        return;
    }
    const btn = document.getElementById('saveCleanedBtn');
    btn.disabled = true;
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
            showToast(`Enregistrement refusé : ${details}`, 'danger');
            return;
        }
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        const data = await resp.json();
        setCleanedDirty(false);
        showToast(`Référentiel enregistré · ${data.count} entrées`, 'success');
    } catch (err) {
        showToast(`Erreur : ${err.message}`, 'danger');
    } finally {
        btn.disabled = false;
    }
}

function downloadCleanedFile() {
    if (!cleanedDoc) {
        showToast('Aucun référentiel chargé.', 'warning');
        return;
    }
    const blob = new Blob([JSON.stringify(cleanedDoc, null, 2)], { type: 'application/json' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = 'cleaned_codelists.json';
    a.click();
    URL.revokeObjectURL(url);
}

/* Le référentiel a changé côté serveur (validation) : rechargé à la prochaine ouverture */
function invalidateCleaned() {
    if (cleanedDirty) return;
    cleanedDoc = null;
    if (document.getElementById('cleaned').classList.contains('active')) loadCleaned({ quiet: true });
    else renderCleaned();
}

function setCleanedDirty(dirty) {
    cleanedDirty = dirty;
    document.getElementById('cleanedDirty').classList.toggle('d-none', !dirty);
}

/* ------------------------------------------------------------------ */
/* Tableau
/* ------------------------------------------------------------------ */

const levelKey = (entry) => entry.level || 'none';

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

    // Pré-charge les codes des listes remplacées depuis registryData
    _preloadReplaceCaches();

    const entries = Object.values(cleanedDoc.codelists || {});
    renderLevelTabs(entries);

    const search = document.getElementById('cleanedSearch').value.trim().toLowerCase();
    let shown = cleanedLevel === 'all' ? entries : entries.filter(e => levelKey(e) === cleanedLevel);
    if (search) {
        shown = shown.filter(e =>
            (e.name || '').toLowerCase().includes(search) ||
            (e.label || '').toLowerCase().includes(search) ||
            (e.id || '').toLowerCase().includes(search)
        );
    }
    shown = [...shown].sort((a, b) => (a.name || '').localeCompare(b.name || '', 'fr'));
    shownIds = shown.map(e => e.id);
    document.getElementById('cleanedCount').textContent =
        `${shown.length} entrée${shown.length > 1 ? 's' : ''} affichée${shown.length > 1 ? 's' : ''}`;

    container.innerHTML = shown.length
        ? shown.map(renderCleanedRow).join('')
        : '<tr><td colspan="8" class="text-muted">Aucune entrée pour ce filtre.</td></tr>';
    renderBulkBar();
}

function renderLevelTabs(entries) {
    const count = (key) => (key === 'all' ? entries.length : entries.filter(e => levelKey(e) === key).length);
    document.getElementById('cleanedLevelTabs').innerHTML = CLEANED_LEVELS.map(([key, label]) => {
        const dot = ['officielle', 'commune', 'specifique'].includes(key)
            ? `<span class="dot lvl-${key}"></span>` : '';
        return `<button type="button" class="tab" role="tab" data-level-tab="${key}" `
            + `aria-selected="${key === cleanedLevel}">${dot}${label}<span class="num">${count(key)}</span></button>`;
    }).join('');
}

const CLEANED_SOURCES = {
    validation: 'Validation',
    manuel: 'Ajout manuel',
    'api-geo': 'API · COG',
    'api-structures': 'API · Colectica',
};

/* Pastilles de niveau d'une ligne : pleine = validé, pointillés = suggéré */
const ROW_LEVELS = [['officielle', 'Off.'], ['commune', 'Com.'], ['specifique', 'Spé.']];

function rowLevelPickHtml(entry) {
    const name = escapeHtml(entry.name || entry.id);
    return `<div class="lvl-pick compact" role="group" aria-label="Niveau de ${name}">` + ROW_LEVELS.map(([value, short]) => {
        const on = entry.level === value;
        const suggested = !entry.level && entry.level_suggested === value;
        const label = `${on ? 'Retirer le niveau' : 'Classer en'} ${LEVEL_LABELS[value]}${suggested ? ' (suggéré)' : ''}`;
        return `<button type="button" data-row-level="${value}" aria-pressed="${on}"`
            + `${suggested ? ' class="suggested"' : ''} title="${label}" aria-label="${label} : ${name}">${short}</button>`;
    }).join('') + '</div>';
}

function renderCleanedRow(entry) {
    const id = escapeHtml(entry.id);
    const name = escapeHtml(entry.name || entry.id);
    const replaces = (entry.replaces || []).length;
    const checked = cleanedSelected.has(entry.id);
    return `<tr class="hoverable${checked ? ' selected' : ''}" data-cl-id="${id}">
        <td class="sel"><input type="checkbox" class="form-check-input" data-select ${checked ? 'checked' : ''}
            aria-label="Sélectionner ${name}"></td>
        <td><div class="ref-name">${name}</div>
            <div class="dup-id">${id}</div></td>
        <td>${rowLevelPickHtml(entry)}</td>
        <td>${escapeHtml(CLEANED_SOURCES[entry.source] || entry.source || '—')}</td>
        <td class="r">${(entry.codes || []).length}</td>
        <td class="r">${replaces ? `${replaces} liste${replaces > 1 ? 's' : ''}` : '—'}</td>
        <td class="mono">${escapeHtml((entry.series || []).join(', ') || '—')}</td>
        <td class="r text-nowrap">
            <button type="button" class="btn btn-ghost btn-sm" data-action="edit-entry"
                aria-label="Modifier ${name}">${icon('edit')}Modifier</button>
        </td>
    </tr>`;
}

/* ------------------------------------------------------------------ */
/* Classement : depuis la ligne ou par lot
/* ------------------------------------------------------------------ */

/* Fixe le niveau des entrées ; renvoie les niveaux précédents (pour Annuler) */
function setLevels(levels) {
    const previous = {};
    for (const [id, level] of Object.entries(levels)) {
        const entry = cleanedDoc?.codelists?.[id];
        if (!entry || (entry.level || '') === level) continue;
        previous[id] = entry.level || '';
        entry.level = level;
    }
    if (Object.keys(previous).length) {
        setCleanedDirty(true);
        if (editingId in previous) renderCleanedEditor();
        renderCleaned();
    }
    return previous;
}

function classifyRow(id, level, btn) {
    const entry = cleanedDoc?.codelists?.[id];
    if (!entry) return;
    // Un clic sur le niveau déjà validé le retire
    setLevels({ [id]: entry.level === level ? '' : level });
    const again = document.querySelector(
        `#cleanedContainer tr[data-cl-id="${CSS.escape(id)}"] [data-row-level="${level}"]`);
    // La ligne peut sortir du filtre (onglet « À classer ») : le focus va à la suivante
    (again || document.querySelector('#cleanedContainer [data-row-level]') || btn)?.focus();
}

/* Classement par lot ; `level` null = niveau suggéré de chaque liste */
function classifySelected(level) {
    const ids = [...cleanedSelected].filter(id => cleanedDoc?.codelists?.[id]);
    const levels = {};
    for (const id of ids) {
        const target = level === null ? cleanedDoc.codelists[id].level_suggested : level;
        if (target !== undefined && (level !== null || target)) levels[id] = target;
    }
    if (!Object.keys(levels).length) {
        showToast('Aucune suggestion de niveau pour la sélection.', 'warning');
        return;
    }
    cleanedSelected.clear();
    const previous = setLevels(levels);
    const n = Object.keys(previous).length;
    if (!n) {
        renderCleaned();
        showToast('Niveaux inchangés.', 'info');
        return;
    }
    const what = level === null ? 'au niveau suggéré'
        : level ? `en ${LEVEL_LABELS[level]}` : 'en « à classer »';
    showToast(`${n} liste${n > 1 ? 's' : ''} classée${n > 1 ? 's' : ''} ${what}`, 'success', {
        actionLabel: 'Annuler',
        onAction: () => setLevels(previous),
    });
}

function toggleSelected(id, on, range = false) {
    const ids = range && lastCheckedId && shownIds.includes(lastCheckedId)
        ? shownIds.slice(Math.min(shownIds.indexOf(lastCheckedId), shownIds.indexOf(id)),
            Math.max(shownIds.indexOf(lastCheckedId), shownIds.indexOf(id)) + 1)
        : [id];
    for (const i of ids) {
        if (on) cleanedSelected.add(i);
        else cleanedSelected.delete(i);
        const row = document.querySelector(`#cleanedContainer tr[data-cl-id="${CSS.escape(i)}"]`);
        row?.classList.toggle('selected', on);
        const box = row?.querySelector('[data-select]');
        if (box) box.checked = on;
    }
    lastCheckedId = id;
    renderBulkBar();
}

function renderBulkBar() {
    const n = cleanedSelected.size;
    document.getElementById('cleanedBulkBar').classList.toggle('d-none', !n);
    document.getElementById('cleanedSelCount').textContent =
        `${n} sélectionnée${n > 1 ? 's' : ''}`;
    const all = document.getElementById('cleanedSelectAll');
    const visible = shownIds.filter(id => cleanedSelected.has(id)).length;
    all.checked = shownIds.length > 0 && visible === shownIds.length;
    all.indeterminate = visible > 0 && visible < shownIds.length;
}

/* ------------------------------------------------------------------ */
/* Panneau latéral d'édition
/* ------------------------------------------------------------------ */

function openCleanedEditor(clId) {
    editingId = clId;
    renderCleanedEditor();
    bootstrap.Offcanvas.getOrCreateInstance(document.getElementById('cleanedEditor')).show();
}

function renderCleanedEditor() {
    const entry = cleanedDoc?.codelists?.[editingId];
    const body = document.getElementById('cleanedEditorBody');
    if (!entry) {
        body.innerHTML = '';
        return;
    }
    const id = escapeHtml(entry.id);
    document.getElementById('cleanedEditorTitle').textContent = entry.name || entry.id;

    let html = `<div class="editor" data-cl-id="${id}">`;
    html += `<div class="field"><label class="form-label" for="ed-name">Nom</label>`;
    html += `<input type="text" class="form-control" id="ed-name" data-field="name" value="${escapeHtml(entry.name || '')}"></div>`;
    html += `<div class="field"><label class="form-label" for="ed-label">Libellé</label>`;
    html += `<input type="text" class="form-control" id="ed-label" data-field="label" value="${escapeHtml(entry.label || '')}"></div>`;

    // Niveau validé ; la suggestion (provenance) est rappelée dessous
    html += `<div class="field"><span class="form-label">Niveau</span><div class="lvl-pick" role="group" aria-label="Niveau">`;
    for (const [value, label] of [['officielle', 'Officielle'], ['commune', 'Commune'], ['specifique', 'Spécifique'], ['', 'À classer']]) {
        html += `<button type="button" data-level="${value}" aria-pressed="${(entry.level || '') === value}">${label}</button>`;
    }
    html += `</div>`;
    if (entry.level_suggested) {
        html += `<div class="form-text">Suggestion d'après les séries : ${escapeHtml(LEVEL_LABELS[entry.level_suggested] || entry.level_suggested)}</div>`;
    }
    html += `</div>`;

    html += `<dl>`;
    html += `<dt>Identifiant</dt><dd><span class="mono">${id}</span> `;
    html += `<button type="button" class="copy-id-btn" data-copy-id="${id}" title="Copier l'ID" aria-label="Copier l'ID">${icon('copy', 'sm')}</button></dd>`;
    if (entry.urn) html += `<dt>URN</dt><dd class="mono">${escapeHtml(entry.urn)}</dd>`;
    if (entry.source) html += `<dt>Source</dt><dd>${escapeHtml(CLEANED_SOURCES[entry.source] || entry.source)}</dd>`;
    if ((entry.series || []).length) html += `<dt>Séries</dt><dd class="mono">${escapeHtml(entry.series.join(', '))}</dd>`;
    if ((entry.operations || []).length) html += `<dt>Opérations</dt><dd class="mono">${escapeHtml(entry.operations.join(', '))}</dd>`;
    if (entry.updated_at) html += `<dt>Mise à jour</dt><dd>${escapeHtml(entry.updated_at)}</dd>`;
    html += `</dl>`;

    html += renderReplacesHtml(entry.replaces || []);

    // Codes éditables
    html += `<div class="field"><span class="form-label">Codes (${(entry.codes || []).length})</span>`;
    html += `<table class="code-edit"><tbody>`;
    (entry.codes || []).forEach((pair, idx) => {
        html += `<tr><td><input type="text" class="form-control form-control-sm mono" aria-label="Valeur du code ${idx + 1}" `;
        html += `data-field="code-value" data-code-idx="${idx}" value="${escapeHtml(pair[0] ?? '')}"></td>`;
        html += `<td><input type="text" class="form-control form-control-sm" aria-label="Libellé du code ${idx + 1}" `;
        html += `data-field="code-label" data-code-idx="${idx}" value="${escapeHtml(pair[1] ?? '')}"></td>`;
        html += `<td><button type="button" class="btn btn-ghost btn-sm" data-action="delete-code" `;
        html += `data-code-idx="${idx}" aria-label="Supprimer le code ${idx + 1}">${icon('x')}</button></td></tr>`;
    });
    html += `</tbody></table>`;
    html += `<button type="button" class="btn btn-quiet btn-sm mt-1" data-action="add-code">${icon('plus')}Ajouter un code</button></div>`;

    html += `<div class="d-flex justify-content-between gap-2 flex-wrap">`;
    html += `<button type="button" class="btn btn-danger-quiet btn-sm" data-action="delete-entry">${icon('trash')}Supprimer l'entrée</button>`;
    html += `<button type="button" class="btn btn-primary btn-sm" data-bs-dismiss="offcanvas">Terminé</button>`;
    html += `</div></div>`;
    body.innerHTML = html;
}

/* Listes remplacées (lecture seule), codes repliés */
function renderReplacesHtml(replaces) {
    if (!replaces.length) return '';
    let html = `<div class="field"><span class="form-label">Remplace (${replaces.length})</span><div class="replace-list">`;
    for (const rep of replaces) {
        const codes = _replacedCache[rep.id];
        const tableHTML = _buildReplaceTableHTML(codes, rep.id);
        const conf = rep.confidence != null ? ` · ${Math.round(rep.confidence * 100)} %` : '';
        html += `<details class="replace-collapsible">`;
        html += `<summary class="replace-summary" title="${escapeHtml(rep.id || '')}">${icon('chevron', 'sm')}`;
        html += `<span>${escapeHtml(rep.name || rep.id || '')}${conf}</span>`;
        html += `<span class="replace-codes-count">${tableHTML ? `${codes.length} codes` : 'codes non chargés'}</span></summary>`;
        html += `<div class="replace-body">`;
        html += tableHTML || `<div class="replace-not-loaded">Ouvrez le run correspondant dans Valider pour voir ses codes.</div>`;
        html += `</div></details>`;
    }
    return html + `</div></div>`;
}

/* ------------------------------------------------------------------ */
/* Édition (listeners délégués)
/* ------------------------------------------------------------------ */

function _editedEntry() {
    return cleanedDoc?.codelists?.[editingId] || null;
}

function handleCleanedInput(ev) {
    const entry = _editedEntry();
    const field = ev.target.dataset.field;
    if (!entry || !field) return;

    if (field === 'name' || field === 'label') {
        entry[field] = ev.target.value;
        if (field === 'name') document.getElementById('cleanedEditorTitle').textContent = entry.name || entry.id;
    } else if (field === 'code-value' || field === 'code-label') {
        const idx = parseInt(ev.target.dataset.codeIdx, 10);
        if (!entry.codes || !entry.codes[idx]) return;
        entry.codes[idx][field === 'code-value' ? 0 : 1] = ev.target.value;
    } else {
        return;
    }
    setCleanedDirty(true);
}

function handleEditorClick(ev) {
    const entry = _editedEntry();
    if (!entry) return;

    const levelBtn = ev.target.closest('[data-level]');
    if (levelBtn) {
        entry.level = levelBtn.dataset.level;
        setCleanedDirty(true);
        renderCleanedEditor();
        renderCleaned();
        // Le panneau est redessiné : le focus y reste (Échap le ferme toujours)
        document.querySelector(`#cleanedEditorBody [data-level="${CSS.escape(entry.level)}"]`)?.focus();
        return;
    }
    const copyBtn = ev.target.closest('.copy-replace-codes');
    if (copyBtn) {
        copyReplaceCodes(copyBtn.dataset.replaceId);
        return;
    }
    const btn = ev.target.closest('[data-action]');
    if (!btn) return;
    const action = btn.dataset.action;
    if (action === 'add-code') {
        entry.codes = entry.codes || [];
        entry.codes.push(['', '']);
        setCleanedDirty(true);
        renderCleanedEditor();
        const inputs = document.querySelectorAll('#cleanedEditorBody [data-field="code-value"]');
        inputs[inputs.length - 1]?.focus();
        renderCleaned();
    } else if (action === 'delete-code') {
        entry.codes.splice(parseInt(btn.dataset.codeIdx, 10), 1);
        setCleanedDirty(true);
        renderCleanedEditor();
        renderCleaned();
        document.querySelector('#cleanedEditorBody [data-action="add-code"]')?.focus();
    } else if (action === 'delete-entry') {
        confirmDialog(`Supprimer l'entrée « ${entry.name || entry.id} » du référentiel ?`,
            { confirmLabel: 'Supprimer', danger: true }).then((ok) => {
            if (!ok) return;
            delete cleanedDoc.codelists[entry.id];
            cleanedSelected.delete(entry.id);
            setCleanedDirty(true);
            bootstrap.Offcanvas.getOrCreateInstance(document.getElementById('cleanedEditor')).hide();
            editingId = null;
            renderCleaned();
        });
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
        rows += `<tr><td>${escapeHtml(pair[0] ?? '')}</td><td>${escapeHtml(pair[1] ?? '')}</td></tr>`;
    }
    return (
        `<table class="codes"><thead><tr><th>Code</th><th>Libellé</th></tr></thead>` +
        `<tbody>${rows}</tbody></table>` +
        `<button type="button" class="btn btn-quiet btn-sm copy-replace-codes" ` +
        `data-replace-id="${escapeHtml(rid)}">${icon('copy')}Copier tous les codes</button>`
    );
}

function copyReplaceCodes(rid) {
    const codes = _replacedCache[rid];
    if (!codes || codes.length === 0) return;
    const text = codes.map(([val, label]) => `${val}: ${label || ''}`).join('\n');
    navigator.clipboard.writeText(text).then(() => {
        showToast('Codes copiés dans le presse-papier', 'success');
    }).catch(() => {
        showToast('Copie impossible.', 'danger');
    });
}

/* ------------------------------------------------------------------ */
/* DOM Ready
/* ------------------------------------------------------------------ */

document.addEventListener('DOMContentLoaded', () => {
    document.getElementById('loadCleanedBtn').addEventListener('click', () => loadCleaned());
    document.getElementById('saveCleanedBtn').addEventListener('click', saveCleaned);
    document.getElementById('downloadCleanedFileBtn').addEventListener('click', downloadCleanedFile);
    document.getElementById('cleanedSearch').addEventListener('input', renderCleaned);

    document.getElementById('cleanedLevelTabs').addEventListener('click', (e) => {
        const tab = e.target.closest('[data-level-tab]');
        if (!tab) return;
        cleanedLevel = tab.dataset.levelTab;
        renderCleaned();
    });
    document.getElementById('cleanedContainer').addEventListener('click', (e) => {
        const row = e.target.closest('[data-cl-id]');
        if (!row) return;
        const box = e.target.closest('[data-select]');
        const levelBtn = e.target.closest('[data-row-level]');
        if (box) toggleSelected(row.dataset.clId, box.checked, e.shiftKey);
        else if (levelBtn) classifyRow(row.dataset.clId, levelBtn.dataset.rowLevel, levelBtn);
        else if (e.target.closest('[data-action="edit-entry"]')) openCleanedEditor(row.dataset.clId);
    });
    document.getElementById('cleanedSelectAll').addEventListener('change', (e) => {
        for (const id of shownIds) {
            if (e.target.checked) cleanedSelected.add(id);
            else cleanedSelected.delete(id);
        }
        renderCleaned();
    });
    document.getElementById('cleanedBulkBar').addEventListener('click', (e) => {
        const btn = e.target.closest('button');
        if (!btn) return;
        if (btn.dataset.bulkLevel !== undefined) classifySelected(btn.dataset.bulkLevel);
        else if (btn.dataset.action === 'bulk-suggested') classifySelected(null);
        else if (btn.dataset.action === 'bulk-clear') {
            cleanedSelected.clear();
            renderCleaned();
        }
    });
    const editor = document.getElementById('cleanedEditorBody');
    editor.addEventListener('input', handleCleanedInput);
    editor.addEventListener('change', (e) => {
        // Le tableau suit le nom saisi, sans être redessiné à chaque frappe
        if (e.target.dataset.field) renderCleaned();
    });
    editor.addEventListener('click', handleEditorClick);

    // Ouverture de l'onglet : charge le référentiel du dossier de sortie
    document.getElementById('cleaned-tab').addEventListener('shown.bs.tab', () => {
        if (!cleanedDoc && !cleanedDirty) loadCleaned({ quiet: true });
    });
    // Nouveau dossier de sortie : le référentiel affiché n'est plus le bon
    document.addEventListener('outputrootchange', () => {
        if (cleanedDirty) return;
        cleanedDoc = null;
        renderCleaned();
        if (document.getElementById('cleaned').classList.contains('active')) loadCleaned({ quiet: true });
    });

    // Éditions non enregistrées : avertir avant de quitter la page
    window.addEventListener('beforeunload', (e) => {
        if (!cleanedDirty) return;
        e.preventDefault();
        e.returnValue = '';
    });
});
