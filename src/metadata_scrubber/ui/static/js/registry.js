/* registry.js — Gestion de la validation des doublons

   Les décisions sont enregistrées automatiquement par le serveur dans
   decisions.json à chaque changement (indicateur « Enregistré à … » dans
   l'en-tête). Le run ouvert, les filtres et la CodeList affichée sont
   conservés dans l'URL (#run=…&f=…&cl=…). */

// API_BASE is defined in config.js (loaded first) ; escapeHtml, copyId,
// showToast, confirmDialog, getOutputRoot, fetchRuns… dans main.js

let registryData = null;
let registryPath = null;   // registre ouvert (paramètre `path` de toutes les routes)
let currentPage = 0;
let TOTAL_PAGES = 0;
let selectedClId = null;   // CodeList affichée dans la colonne de droite
let activeDupId = null;    // doublon actif (raccourcis clavier)
let lastNavItems = [];     // CodeLists de la page affichée à gauche
let filteredItems = [];    // toutes les CodeLists du filtre courant (toutes pages)
const openComparisons = new Set();  // doublons dont le tableau comparatif est ouvert
const batchSelected = new Set();  // ids cochés pour l'ajout en lot (survit à la pagination)
const inCleaned = new Set();      // ids présents dans le registre nettoyé

/* URL d'une route du registre ouvert */
function regUrl(route, params = {}) {
    const query = new URLSearchParams({ ...params });
    if (registryPath) query.set('path', registryPath);
    return `${API_BASE}${route}?${query}`;
}

async function postJson(url, body, method = 'POST') {
    const resp = await fetch(url, {
        method,
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
    });
    if (!resp.ok) {
        const detail = (await resp.json().catch(() => ({}))).detail;
        throw new Error(typeof detail === 'string' ? detail : `HTTP ${resp.status}`);
    }
    return resp.json();
}

/* ------------------------------------------------------------------ */
/* Ouverture d'un run / d'un registre
/* ------------------------------------------------------------------ */

/* Remplit la liste des runs de la racine de sortie */
async function loadRunOptions() {
    const select = document.getElementById('runSelect');
    const root = getOutputRoot();
    document.getElementById('outputRoot').value = root;
    select.innerHTML = '<option value="">— chargement des runs… —</option>';
    try {
        const runs = await fetchRuns(root);
        if (!runs.length) {
            select.innerHTML = '<option value="">— aucun run dans ce dossier —</option>';
            return;
        }
        select.innerHTML = runs.map(run => {
            const source = (run.xml_source || '').startsWith('api:')
                ? `API ${run.xml_source.slice(4)}`
                : (run.xml_source || '').split('/').pop();
            const status = run.status === 'error' ? ' · en erreur' : '';
            const n = run.counts?.candidates != null ? ` · ${run.counts.candidates} candidats` : '';
            const label = `${formatRunId(run.run_id)}${source ? ` · ${source}` : ''}${n}${status}`;
            return `<option value="${escapeHtml(run.registry_path)}"${run.status === 'error' ? ' disabled' : ''}>${escapeHtml(label)}</option>`;
        }).join('');
        if (registryPath && runs.some(r => r.registry_path === registryPath)) {
            select.value = registryPath;
        }
    } catch (err) {
        select.innerHTML = '<option value="">— runs indisponibles —</option>';
        showToast(`Liste des runs indisponible : ${err.message}`, 'danger');
    }
}

/* Ouvre un registre des doublons ; `restore` = état (filtres, CodeList) issu de l'URL */
async function openRegistry(path, restore = null) {
    path = (path || '').trim();
    if (!path) {
        showToast('Choisissez un run ou saisissez le chemin d\'un registre.', 'warning');
        return;
    }
    switchTab('validation');
    const btn = document.getElementById('openRunBtn');
    btn.disabled = true;
    try {
        const resp = await fetch(`${API_BASE}api/registry?path=${encodeURIComponent(path)}`);
        if (!resp.ok) {
            const detail = (await resp.json().catch(() => ({}))).detail;
            throw new Error(detail || `HTTP ${resp.status}`);
        }
        registryData = await resp.json();
        registryPath = path;
        document.getElementById('registryPath').value = path;
        document.getElementById('runSelect').value = path;
        document.getElementById('emptyState').classList.add('d-none');
        document.getElementById('statsSection').classList.remove('d-none');
        document.getElementById('saveStatus').textContent = '';

        currentPage = restore?.page || 0;
        selectedClId = restore?.cl || null;
        activeDupId = null;
        openComparisons.clear();
        batchSelected.clear();
        inCleaned.clear();
        updateStats();
        renderCodelistsFiltered();
        loadRunSummary();
        loadCleanedIds();

        if (!restore) {
            showToast(`Registre chargé · ${Object.keys(registryData).length} CodeLists`, 'success');
        }
    } catch (err) {
        console.error('Chargement registre:', err);
        showToast(`Registre non chargé : ${err.message}`, 'danger');
    } finally {
        btn.disabled = false;
    }
}

function loadRegistry() {
    return openRegistry(document.getElementById('registryPath').value);
}

/* Avertissements du run (run_summary.json à côté du registre) : étape sémantique sautée, etc. */
async function loadRunSummary() {
    const box = document.getElementById('runWarnings');
    renderRunWarnings(box, []);
    try {
        const resp = await fetch(regUrl('api/registry/run-summary'));
        if (!resp.ok) return;   // ancien registre sans résumé : rien à afficher
        const summary = await resp.json();
        renderRunWarnings(box, summary.warnings);
    } catch (err) {
        console.warn('run-summary:', err);
    }
}

/* Ids déjà présents dans le registre nettoyé : le ✅ reste fiable d'une session à l'autre */
async function loadCleanedIds() {
    try {
        const resp = await fetch(regUrl('api/registry/cleaned'));
        if (!resp.ok) return;   // 404 : pas encore de registre nettoyé
        const cleaned = await resp.json();
        for (const id of Object.keys(cleaned.codelists || {})) inCleaned.add(id);
        renderCodelistsFiltered();
    } catch (err) {
        console.warn('cleaned ids:', err);
    }
}

/* Reporte les doublons approuvés dans le registre nettoyé (les décisions, elles,
   sont déjà enregistrées) */
async function saveRegistry() {
    if (!registryData) {
        showToast('Aucun registre chargé.', 'warning');
        return;
    }
    const btn = document.getElementById('saveBtn');
    const original = btn.textContent;
    btn.disabled = true;
    btn.textContent = '⏳ Mise à jour…';

    try {
        const resp = await fetch(regUrl('api/registry/save'), { method: 'POST' });
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        const data = await resp.json();
        showToast(
            `Registre nettoyé mis à jour (${data.cleaned_count ?? 0} entrées)`
            + ` · ${data.decisions_count ?? 0} décision(s) conservée(s) pour les prochains runs`,
            'success'
        );
        if (data.cleaned_path) document.getElementById('cleanedPath').value = data.cleaned_path;
        setSaveStatus({ saved: true });
    } catch (err) {
        showToast(`Erreur: ${err.message}`, 'danger');
    } finally {
        btn.disabled = false;
        btn.textContent = original;
    }
}

function downloadJson(doc, filename) {
    const blob = new Blob([JSON.stringify(doc, null, 2)], { type: 'application/json' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = filename;
    a.click();
    URL.revokeObjectURL(url);
}

async function downloadRegistry() {
    if (!registryData) {
        showToast('Aucun registre chargé.', 'warning');
        return;
    }
    downloadJson(registryData, 'codelist_duplicates.json');
}

async function downloadCleanedRegistry() {
    if (!registryData) {
        showToast('Aucun registre chargé.', 'warning');
        return;
    }
    try {
        const resp = await fetch(regUrl('api/registry/cleaned'));
        if (resp.status === 404) {
            showToast('Pas encore de registre nettoyé : utilisez « Mettre à jour le registre nettoyé ».', 'warning');
            return;
        }
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        const cleaned = await resp.json();
        downloadJson(cleaned, 'cleaned_codelists.json');
    } catch (err) {
        showToast(`Erreur: ${err.message}`, 'danger');
    }
}

/* ------------------------------------------------------------------ */
/* Indicateur d'enregistrement automatique
/* ------------------------------------------------------------------ */

function setSaveStatus(result) {
    const el = document.getElementById('saveStatus');
    if (result.saved === false) {
        el.innerHTML = '<span class="save-error">⚠ Échec de l\'enregistrement</span> '
            + '<button type="button" class="btn btn-sm btn-light py-0" id="retrySaveBtn">Réessayer</button>';
        el.title = result.save_error || '';
        document.getElementById('retrySaveBtn').addEventListener('click', retrySave);
        return;
    }
    const at = result.saved_at ? new Date(result.saved_at) : new Date();
    el.textContent = `✓ Enregistré à ${at.toLocaleTimeString('fr-FR', { hour: '2-digit', minute: '2-digit' })}`;
    el.title = 'Décisions enregistrées automatiquement dans decisions.json';
}

async function retrySave() {
    try {
        const resp = await fetch(regUrl('api/registry/persist'), { method: 'POST' });
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        setSaveStatus(await resp.json());
    } catch (err) {
        setSaveStatus({ saved: false, save_error: err.message });
    }
}

/* ------------------------------------------------------------------ */
/* Stats
/* ------------------------------------------------------------------ */

function updateStats() {
    if (!registryData) return;
    const stats = computeStats(registryData);
    document.getElementById('statTotalCL').textContent = stats.total_code_lists;
    document.getElementById('statTotalDups').textContent = stats.total_duplicates;
    document.getElementById('statApproved').textContent = stats.approved;
    document.getElementById('statRejected').textContent = stats.rejected;
    document.getElementById('statPending').textContent = stats.pending;

    const decided = stats.approved + stats.rejected;
    const pct = stats.total_duplicates ? Math.round(100 * decided / stats.total_duplicates) : 0;
    document.getElementById('decisionProgressBar').style.width = `${pct}%`;
    document.getElementById('decisionProgress').setAttribute('aria-valuenow', pct);
    document.getElementById('decisionProgressLabel').textContent =
        `${decided} / ${stats.total_duplicates} décisions (${pct} %)`;
}

function computeStats(registry) {
    let totalDuplicates = 0, approved = 0, pending = 0;
    for (const cl of Object.values(registry)) {
        for (const dup of cl.duplicates || []) {
            totalDuplicates++;
            if (dup.decision === 'approve') approved++;
            else if (dup.decision !== 'reject') pending++;
        }
    }
    return {
        total_code_lists: Object.keys(registry).length,
        total_duplicates: totalDuplicates,
        approved,
        rejected: totalDuplicates - approved - pending,
        pending,
    };
}

const decisionOf = (dup) => dup.decision || 'pending';
const pendingCount = (dups) => dups.filter(d => decisionOf(d) === 'pending').length;

/* ------------------------------------------------------------------ */
/* Filtering & rendering
/* ------------------------------------------------------------------ */

function getFilters() {
    return {
        decisionFilter: document.getElementById('decisionFilter').value,
        originFilter: document.getElementById('originFilter').value,
        search: document.getElementById('searchFilter').value.trim(),
        sortBy: document.getElementById('sortBy').value,
        pageSize: parseInt(document.getElementById('pageSize').value, 10),
    };
}

/* Doublons d'une CodeList affichés à droite (le filtre de décision s'applique aussi à eux) */
function visibleDuplicates(clData) {
    const all = clData?.duplicates || [];
    const filter = document.getElementById('decisionFilter').value;
    if (['approve', 'reject', 'pending'].includes(filter)) {
        return all.filter(d => decisionOf(d) === filter);
    }
    return all;
}

function matchesDecisionFilter(filter, clId, clData) {
    const dups = clData.duplicates || [];
    const dismissed = !!clData.dismissed;
    switch (filter) {
        case 'todo':   // doublons à décider, ou liste sans doublon encore à classer
            return pendingCount(dups) > 0
                || (!dups.length && !dismissed && !inCleaned.has(clId));
        case 'none':
            return !dups.length && !dismissed;
        case 'dismissed':
            return dismissed;
        case 'approve':
        case 'reject':
        case 'pending':
            return dups.some(d => decisionOf(d) === filter);
        default:
            return true;
    }
}

const SORTERS = {
    pending: (a, b) => pendingCount(b.duplicates) - pendingCount(a.duplicates)
        || b.duplicates.length - a.duplicates.length,
    confidence: (a, b) => maxConfidence(b) - maxConfidence(a),
    name: (a, b) => a.name.localeCompare(b.name, 'fr'),
    vars: (a, b) => b.varsCount - a.varsCount,
    series: (a, b) => b.seriesCount - a.seriesCount || b.varsCount - a.varsCount,
};

function maxConfidence(cl) {
    return Math.max(0, ...cl.duplicates.map(d => d.confidence || 0));
}

function renderCodelistsFiltered() {
    if (!registryData) return;
    const { decisionFilter, originFilter, search, sortBy, pageSize } = getFilters();
    const searchLower = search.toLowerCase();

    const items = [];
    for (const [clId, clData] of Object.entries(registryData)) {
        if (!matchesDecisionFilter(decisionFilter, clId, clData)) continue;

        // Origin filter (listes issues du registre nettoyé, ou listes suspectes)
        if (originFilter === 'registry' && clData.origin !== 'registry') continue;
        if (originFilter === 'suspect' && !(clData.quality_flags || []).length) continue;
        if (originFilter.startsWith('level:')
            && (clData.level || clData.level_suggested) !== originFilter.slice(6)) continue;

        const duplicates = clData.duplicates || [];
        // Search filter (nom ou ID de la liste, ou d'un de ses doublons)
        if (search) {
            const haystack = [clData.name || '', clId]
                .concat(duplicates.flatMap(d => [d.name || '', d.id]))
                .join(' ').toLowerCase();
            if (!haystack.includes(searchLower)) continue;
        }

        items.push({
            id: clId,
            name: clData.name || clId,
            duplicates,
            varsCount: (clData.vars || []).length,
            seriesCount: (clData.series || []).length,
        });
    }
    items.sort(SORTERS[sortBy] || SORTERS.pending);
    filteredItems = items;

    TOTAL_PAGES = Math.ceil(items.length / pageSize) || 1;
    if (currentPage >= TOTAL_PAGES) currentPage = TOTAL_PAGES - 1;
    // La CodeList demandée (URL, raccourci « n ») est affichée sur sa page
    const wanted = items.findIndex(cl => cl.id === selectedClId);
    if (wanted >= 0) currentPage = Math.floor(wanted / pageSize);

    const start = currentPage * pageSize;
    const paged = items.slice(start, start + pageSize);
    if (!paged.some(cl => cl.id === selectedClId)) selectedClId = paged[0]?.id || null;

    renderCodelistNav(paged);
    renderCodelistDetail(selectedClId);
    renderPagination(items.length, currentPage, pageSize);
    updateBulkCounts();
    writeHash();
}

/* Liste de gauche : une ligne compacte par CodeList.
   Les compteurs sont lus dans registryData pour rester à jour après un changement de décision. */
function renderCodelistNav(items) {
    if (items) lastNavItems = items;
    const nav = document.getElementById('codelistNav');

    if (!lastNavItems.length) {
        nav.innerHTML = '<div class="text-center py-4 text-muted">Aucune CodeList pour ce filtre.</div>';
        updateBatchBar();
        return;
    }

    let html = '';
    for (const cl of lastNavItems) {
        const data = registryData[cl.id] || {};
        const dups = data.duplicates || [];
        const added = inCleaned.has(cl.id);
        const dismissed = !!data.dismissed;
        html += `<a href="#" class="list-group-item list-group-item-action`;
        html += `${cl.id === selectedClId ? ' active' : ''}${added ? ' in-cleaned' : ''}" `;
        html += `data-id="${escapeHtml(cl.id)}"${cl.id === selectedClId ? ' aria-current="true"' : ''}>`;
        html += `<div class="d-flex justify-content-between align-items-start gap-1">`;
        html += `<div class="d-flex gap-2">`;
        // Case d'ajout en lot : uniquement pour les listes sans doublon pas encore ajoutées
        if (!dups.length && !added && !dismissed) {
            html += `<input type="checkbox" class="form-check-input batch-check" `;
            html += `aria-label="Sélectionner ${escapeHtml(cl.name)}" `;
            html += `data-id="${escapeHtml(cl.id)}"${batchSelected.has(cl.id) ? ' checked' : ''}>`;
        }
        html += `<div class="text-break">${escapeHtml(cl.name)}`;
        if (data.origin === 'registry') html += ` <span class="badge bg-primary" title="Registre nettoyé">📘</span>`;
        if (added) html += ` <span class="badge bg-success" title="Dans le registre nettoyé">✅</span>`;
        if (dismissed) html += ` <span class="badge bg-secondary" title="Écartée : ne pas ajouter au registre nettoyé">🚫</span>`;
        html += qualityBadgeHtml(data.quality_flags);
        html += `<br><code class="dup-id">${escapeHtml(cl.id)}</code>`;
        html += ` <small class="text-muted" title="Variables qui référencent cette CodeList">· ${cl.varsCount} var.</small>`;
        if (cl.seriesCount) {
            html += ` <small class="text-muted" title="Séries d'opérations qui utilisent cette CodeList">· ${cl.seriesCount} série(s)</small>`;
        }
        html += `</div></div>`;
        html += `<div class="flex-shrink-0">${navStatusBadgeHtml(dups)}</div></div></a>`;
    }
    nav.innerHTML = html;
    updateBatchBar();
    nav.querySelector('.list-group-item.active')?.scrollIntoView({ block: 'nearest' });
}

/* Une seule pastille : doublons en attente, ou ✓ quand tout est décidé */
function navStatusBadgeHtml(dups) {
    if (!dups.length) return '';
    const approved = dups.filter(d => decisionOf(d) === 'approve').length;
    const rejected = dups.filter(d => decisionOf(d) === 'reject').length;
    const pending = dups.length - approved - rejected;
    const title = `${dups.length} doublon(s) : ${approved} approuvé(s), ${rejected} rejeté(s), ${pending} en attente`;
    return pending
        ? `<span class="badge bg-warning text-dark" title="${title}">${pending}</span>`
        : `<span class="badge bg-success" title="${title}">✓ ${dups.length}</span>`;
}

/* Pastilles détaillées (en-tête de la CodeList affichée) */
function decisionBadgesHtml(dups) {
    const count = (dec) => dups.filter(d => decisionOf(d) === dec).length;
    const approved = count('approve'), rejected = count('reject');
    const pending = dups.length - approved - rejected;
    return `<span class="badge bg-success" title="Doublons approuvés">✓ ${approved}</span>`
        + `<span class="badge bg-danger" title="Doublons rejetés">✗ ${rejected}</span>`
        + `<span class="badge bg-warning text-dark" title="Doublons en attente">⏳ ${pending}</span>`;
}

function selectCodelist(id, { keepActive = false } = {}) {
    if (id !== selectedClId) openComparisons.clear();
    selectedClId = id;
    if (!keepActive) activeDupId = null;
    document.querySelectorAll('#codelistNav .list-group-item').forEach(el => {
        const on = el.dataset.id === id;
        el.classList.toggle('active', on);
        if (on) {
            el.setAttribute('aria-current', 'true');
            el.scrollIntoView({ block: 'nearest' });
        } else {
            el.removeAttribute('aria-current');
        }
    });
    renderCodelistDetail(id);
    writeHash();
}

/* En-tête de la CodeList affichée à droite (nom, ID, label, badges, volumétrie) */
function renderDetailHeader(id) {
    const cl = registryData[id];
    if (!cl) return '';

    let html = `<div class="card-header d-flex justify-content-between align-items-center gap-2">`;
    html += `<div><h6 class="mb-0">${escapeHtml(cl.name || id)} `;
    html += `<button type="button" class="btn btn-sm btn-link p-0 copy-id-btn" data-copy-id="${escapeHtml(id)}" title="Copier l'ID">${escapeHtml(id)}</button></h6>`;
    if (cl.label) html += `<small class="text-muted">${escapeHtml(cl.label)}</small>`;
    html += `</div><div class="d-flex flex-wrap gap-1 justify-content-end">`;
    if (cl.origin === 'registry') html += `<span class="badge bg-primary">📘 registre</span>`;
    if (cl.master_chosen) html += `<span class="badge bg-info text-dark" title="Maître choisie par l'utilisateur, conservée aux runs suivants">⇅ maître choisie</span>`;
    html += levelBadgeHtml(cl);
    for (const flag of cl.quality_flags || []) {
        const [label, help] = QUALITY_FLAGS[flag] || [flag, flag];
        html += `<span class="badge bg-warning text-dark" title="${escapeHtml(help)}">⚠ ${escapeHtml(label)}</span>`;
    }
    if ((cl.duplicates || []).length) html += decisionBadgesHtml(cl.duplicates);
    html += `</div></div>`;
    html += `<div class="card-body py-1 small">`;
    if (cl.version) {
        html += `<code title="Référence DDI agence:id:version">${escapeHtml(`${cl.agency || '?'}:${id}:${cl.version}`)}</code> · `;
    }
    html += `${cl.codes_count || 0} codes · ${(cl.vars || []).length} variables`;
    html += ` · ${(cl.cat_ids || []).length} catégories`;
    html += provenanceHtml(cl);
    html += suggestedOfficialHtml(cl);
    html += `</div>`;
    return html;
}

/* Niveau validé (registre nettoyé) ou seulement suggéré (provenance) */
function levelBadgeHtml(cl) {
    if (cl.level) {
        return `<span class="badge bg-dark" title="Niveau validé dans le registre nettoyé">${escapeHtml(LEVEL_LABELS[cl.level] || cl.level)}</span>`;
    }
    if (cl.level_suggested) {
        return `<span class="badge border border-secondary text-secondary" title="Niveau suggéré d'après les séries qui utilisent la liste">${escapeHtml(LEVEL_LABELS[cl.level_suggested] || cl.level_suggested)} ?</span>`;
    }
    return '';
}

/* Liste « Voir dictionnaire… » : nomenclature officielle qu'elle désigne */
function suggestedOfficialHtml(cl) {
    if (!cl.suggested_official) return '';
    return `<br><span class="text-primary" title="Liste qui renvoie à une nomenclature : la remplacer par la nomenclature officielle">`
        + `→ remplacer par la nomenclature officielle « ${escapeHtml(cl.suggested_official.name)} »</span>`;
}

/* Opérations et séries qui utilisent la liste (source API) */
function provenanceHtml(cl) {
    if (!(cl.operations || []).length) return '';
    return `<br><span title="Opérations statistiques qui utilisent cette CodeList">Opérations : `
        + `${cl.operations.map(escapeHtml).join(', ')}</span> · `
        + `<span title="Séries de ces opérations">Séries : ${(cl.series || []).map(escapeHtml).join(', ')}</span>`;
}

/* Tableau des modalités (valeur | étiquette) d'une CodeList */
function buildCodesTableHtml(codes) {
    if (!codes || !codes.length) {
        return '<small class="text-muted">Aucune modalité.</small>';
    }
    const truncate = (s) => escapeHtml(String(s || '').slice(0, 60));
    let rows = '';
    for (const pair of codes) {
        rows += `<tr><td>${escapeHtml(pair[0])}</td><td>${truncate(pair.length > 1 ? pair[1] : '')}</td></tr>`;
    }
    let html = `<div class="code-compare-wrap">`;
    html += `<table class="table table-sm table-bordered code-table mb-1">`;
    html += `<thead><tr><th>Valeur</th><th>Étiquette</th></tr></thead>`;
    html += `<tbody>${rows}</tbody></table></div>`;
    return html;
}

/* Colonne de droite : modalités de la CodeList sélectionnée, puis ses doublons */
function renderCodelistDetail(id) {
    const detail = document.getElementById('codelistDetail');
    const cl = id && registryData ? registryData[id] : null;
    if (!cl) {
        detail.innerHTML = '<div class="text-center py-4 text-muted">Sélectionnez une CodeList.</div>';
        return;
    }
    const allDups = cl.duplicates || [];
    const dups = visibleDuplicates(cl);

    // Doublon actif par défaut : le premier en attente, sinon le premier ; son
    // tableau comparatif est ouvert d'office
    if (!dups.some(d => d.id === activeDupId)) {
        activeDupId = (dups.find(d => decisionOf(d) === 'pending') || dups[0])?.id || null;
        if (activeDupId) openComparisons.add(activeDupId);
    }

    let html = `<div class="card"><div id="detailHeader">${renderDetailHeader(id)}</div>`;
    html += `<div class="card-body">`;

    html += `<details${allDups.length ? '' : ' open'} class="mb-3"><summary class="fw-bold">Modalités (${(cl.codes || []).length})</summary>`;
    html += `<div class="mt-2">${buildCodesTableHtml(cl.codes)}</div></details>`;

    if (allDups.length) {
        html += `<h6>Doublons détectés (${dups.length}${dups.length !== allDups.length ? ` sur ${allDups.length}, filtre actif` : ''})</h6>`;
        for (const dup of dups) html += renderDuplicateCard({ ...cl, id }, dup);
    } else {
        // Liste sans doublon détecté → ajout manuel au registre nettoyé
        html += `<div class="d-flex align-items-center flex-wrap gap-2">`;
        html += `<span class="text-muted small">Aucun doublon détecté.</span>`;
        if (inCleaned.has(id)) {
            html += `<span class="badge bg-success">✅ Dans le registre</span>`;
            html += `<button type="button" class="btn btn-sm btn-outline-danger" data-action="remove-clean">`;
            html += `↩ Retirer du registre nettoyé</button>`;
        } else if (cl.dismissed) {
            html += `<span class="badge bg-secondary">🚫 Écartée</span>`;
            html += `<button type="button" class="btn btn-sm btn-outline-secondary" data-action="undismiss">`;
            html += `↩ Annuler l'écart</button>`;
        } else {
            html += `<button type="button" class="btn btn-sm btn-outline-success" data-action="add-clean">`;
            html += `➕ Ajouter au registre nettoyé</button>`;
            html += `<button type="button" class="btn btn-sm btn-outline-secondary" data-action="dismiss" `;
            html += `title="Ne pas ajouter cette liste au registre nettoyé (conservé entre les runs)">`;
            html += `🚫 Écarter</button>`;
        }
        html += `</div>`;
    }
    html += `</div></div>`;
    detail.innerHTML = html;
}

/* Réaffiche un seul doublon (après une décision) sans toucher au reste de la page */
function refreshDuplicateCard(clId, dupId) {
    const cl = registryData[clId];
    const dup = (cl?.duplicates || []).find(d => d.id === dupId);
    const card = document.querySelector(`#codelistDetail .dup-card[data-dup-id="${CSS.escape(dupId)}"]`);
    if (!dup || !card) return;
    card.outerHTML = renderDuplicateCard({ ...cl, id: clId }, dup);
    const header = document.getElementById('detailHeader');
    if (header) header.innerHTML = renderDetailHeader(clId);
}

function setActiveDup(dupId, { scroll = true } = {}) {
    activeDupId = dupId;
    document.querySelectorAll('#codelistDetail .dup-card').forEach(card => {
        const on = card.dataset.dupId === dupId;
        card.classList.toggle('dup-active', on);
        if (on && scroll) card.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
    });
}

function toggleComparison(dupId) {
    if (openComparisons.has(dupId)) openComparisons.delete(dupId);
    else openComparisons.add(dupId);
    refreshDuplicateCard(selectedClId, dupId);
}

async function addToCleaned(clId) {
    const flags = registryData[clId]?.quality_flags || [];
    if (flags.length && !await confirmDialog(
        `Liste suspecte (${flags.map(f => (QUALITY_FLAGS[f] || [f])[0]).join(', ')}). L'ajouter quand même au registre nettoyé ?`,
        { title: 'Liste suspecte', confirmLabel: 'Ajouter quand même' }
    )) return;
    try {
        const resp = await fetch(regUrl('api/registry/add-to-cleaned'), {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ cl_id: clId }),
        });
        if (resp.status === 409) {
            showToast('Déjà présente dans le registre nettoyé.', 'warning');
            markInCleaned([clId]);
            return;
        }
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        const data = await resp.json();
        showToast(`Ajoutée au registre nettoyé · ${data.count} entrées`, 'success');
        if (data.path) document.getElementById('cleanedPath').value = data.path;
        markInCleaned([clId]);
    } catch (err) {
        showToast(`Erreur: ${err.message}`, 'danger');
    }
}

/* ------------------------------------------------------------------ */
/* Ajout en lot au registre nettoyé
/* ------------------------------------------------------------------ */

/* CodeLists sans doublon et non écartées : seules éligibles à l'ajout en lot */
function listsWithoutDuplicates() {
    if (!registryData) return [];
    return Object.keys(registryData).filter(
        id => !(registryData[id].duplicates || []).length && !registryData[id].dismissed
    );
}

/* Retire du registre nettoyé des listes ajoutées à la main (sans `replaces`) */
async function removeFromCleaned(ids, btn) {
    if (!await confirmDialog(`Retirer ${ids.length} CodeList(s) du registre nettoyé ?`,
        { confirmLabel: 'Retirer', danger: true })) return;
    btn.disabled = true;
    try {
        const data = await postJson(regUrl('api/registry/remove-from-cleaned'), { cl_ids: ids });
        let msg = `${data.removed.length} retirée(s) du registre nettoyé · ${data.count} entrées`;
        if (data.protected.length) {
            msg += ` · ${data.protected.length} protégée(s) (issues de décisions approuvées)`;
        }
        showToast(msg, data.protected.length ? 'warning' : 'success');

        for (const id of [...data.removed, ...data.not_present]) inCleaned.delete(id);
        renderCodelistNav();
        renderCodelistDetail(selectedClId);
    } catch (err) {
        showToast(`Erreur: ${err.message}`, 'danger');
        btn.disabled = false;
    }
}

function markInCleaned(ids) {
    for (const id of ids) {
        inCleaned.add(id);
        batchSelected.delete(id);
    }
    renderCodelistNav();
    // Réaffiche le panneau de droite pour griser son bouton d'ajout
    if (ids.includes(selectedClId)) renderCodelistDetail(selectedClId);
}

/* Fait d'un doublon la maître de son groupe (enregistré automatiquement) */
async function swapParent(parentId, newParentId, btn) {
    const newName = registryData[parentId]?.duplicates?.find(d => d.id === newParentId)?.name || newParentId;
    if (!await confirmDialog(
        `Définir « ${newName} » comme maître du groupe ?\nL'ancienne maître devient un doublon en attente.`,
        { title: 'Changer la maître', confirmLabel: 'Définir comme maître' }
    )) return;
    btn.disabled = true;
    try {
        const data = await postJson(regUrl('api/registry/swap-parent'),
            { parent_id: parentId, new_parent_id: newParentId });

        // Remplace l'entrée en gardant l'ordre du registre
        const rebuilt = {};
        for (const [clId, clData] of Object.entries(registryData)) {
            if (clId === parentId) rebuilt[newParentId] = data.entry;
            else rebuilt[clId] = clData;
        }
        registryData = rebuilt;
        selectedClId = newParentId;
        activeDupId = null;
        openComparisons.clear();

        setSaveStatus(data);
        showToast('Maître changée', 'success');
        updateStats();
        renderCodelistsFiltered();
    } catch (err) {
        showToast(`Erreur: ${err.message}`, 'danger');
        btn.disabled = false;
    }
}

/* Écarte (ou réintègre) une CodeList sans doublon (enregistré automatiquement) */
async function setDismissed(clId, dismissed, btn) {
    btn.disabled = true;
    try {
        const data = await postJson(regUrl('api/registry/dismiss'), { cl_ids: [clId], dismissed });
        if (!data.updated.includes(clId)) throw new Error('CodeList non modifiable');

        if (dismissed) {
            registryData[clId].dismissed = true;
            batchSelected.delete(clId);
        } else {
            delete registryData[clId].dismissed;
        }
        setSaveStatus(data);
        showToast(dismissed ? 'CodeList écartée' : 'Écart annulé', 'success');
        renderCodelistNav();
        renderCodelistDetail(selectedClId);
    } catch (err) {
        showToast(`Erreur: ${err.message}`, 'danger');
        btn.disabled = false;
    }
}

function updateBatchBar() {
    const eligible = listsWithoutDuplicates().filter(id => !inCleaned.has(id));
    document.getElementById('batchCount').textContent = batchSelected.size;
    document.getElementById('batchAddBtn').disabled = batchSelected.size === 0;
    document.getElementById('noDupCount').textContent = eligible.length;
    document.getElementById('addAllNoDupBtn').disabled = eligible.length === 0;

    // « Tout sélectionner » reflète les cases visibles de la page affichée
    const boxes = [...document.querySelectorAll('#codelistNav .batch-check')];
    const checked = boxes.filter(b => b.checked).length;
    const all = document.getElementById('batchSelectAll');
    all.disabled = boxes.length === 0;
    all.checked = boxes.length > 0 && checked === boxes.length;
    all.indeterminate = checked > 0 && checked < boxes.length;
}

function toggleSelectAllVisible(checked) {
    document.querySelectorAll('#codelistNav .batch-check').forEach(box => {
        box.checked = checked;
        if (checked) batchSelected.add(box.dataset.id);
        else batchSelected.delete(box.dataset.id);
    });
    updateBatchBar();
}

async function postBatchToCleaned(body, btn) {
    // Ne pas toucher au contenu du bouton (il porte des éléments comme #noDupCount) :
    // on ajoute seulement un spinner, retiré à la fin.
    const spinner = document.createElement('span');
    spinner.className = 'spinner-border spinner-border-sm me-1';
    btn.prepend(spinner);
    btn.disabled = true;
    try {
        const data = await postJson(regUrl('api/registry/add-to-cleaned/batch'), body);

        let msg = `${data.added.length} ajoutée(s) au registre nettoyé · ${data.count} entrées`;
        if (data.already_present.length) msg += ` · ${data.already_present.length} déjà présente(s)`;
        const ignored = data.not_found.length + data.skipped_with_duplicates.length;
        if (ignored) msg += ` · ${ignored} ignorée(s)`;
        showToast(msg, ignored ? 'warning' : 'success');

        if (data.path) document.getElementById('cleanedPath').value = data.path;
        markInCleaned([...data.added, ...data.already_present]);
    } catch (err) {
        showToast(`Erreur: ${err.message}`, 'danger');
    } finally {
        spinner.remove();
        updateBatchBar();   // recalcule l'état disabled des boutons
    }
}

async function addBatchToCleaned(btn) {
    const ids = [...batchSelected];
    if (!ids.length) return;
    if (!await confirmDialog(`Ajouter ${ids.length} CodeList(s) au registre nettoyé ?${suspectWarning(ids)}`,
        { confirmLabel: 'Ajouter' })) return;
    return postBatchToCleaned({ cl_ids: ids }, btn);
}

async function addAllWithoutDuplicates(btn) {
    const ids = listsWithoutDuplicates().filter(id => !inCleaned.has(id));
    if (!ids.length) return;
    if (!await confirmDialog(`Ajouter les ${ids.length} CodeList(s) sans doublon au registre nettoyé ?${suspectWarning(ids)}`,
        { confirmLabel: 'Ajouter' })) return;
    return postBatchToCleaned({ all_without_duplicates: true }, btn);
}

/* ------------------------------------------------------------------ */
/* Comparaison des codes maître / doublon
/* ------------------------------------------------------------------ */

/* Appariement des codes par valeur (insensible à la casse) :
   communs (dont libellé différent), seulement maître, seulement doublon. */
function compareCodes(srcCodes, dstCodes) {
    const index = (codes) => {
        const byLower = {};
        for (const pair of codes || []) {
            const raw = String(pair[0]);
            byLower[raw.toLowerCase()] = [raw, pair.length > 1 ? String(pair[1]) : ''];
        }
        return byLower;
    };
    const src = index(srcCodes), dst = index(dstCodes);
    const common = Object.keys(src).filter(k => k in dst).sort();
    return {
        src, dst, common,
        changed: common.filter(k => src[k][0] !== dst[k][0] || src[k][1] !== dst[k][1]),
        onlySrc: Object.keys(src).filter(k => !(k in dst)).sort(),
        onlyDst: Object.keys(dst).filter(k => !(k in src)).sort(),
    };
}

/* Résumé d'une ligne, visible sans ouvrir le tableau */
function codeDiffSummaryHtml(srcCodes, dstCodes) {
    const diff = compareCodes(srcCodes, dstCodes);
    const parts = [`${diff.common.length} code(s) en commun`];
    if (diff.changed.length) parts.push(`<span class="diff-changed">${diff.changed.length} libellé(s) différent(s)</span>`);
    if (diff.onlySrc.length) parts.push(`<span class="diff-only-src">${diff.onlySrc.length} seulement dans la maître</span>`);
    if (diff.onlyDst.length) parts.push(`<span class="diff-only-dst">${diff.onlyDst.length} seulement dans le doublon</span>`);
    if (!diff.changed.length && !diff.onlySrc.length && !diff.onlyDst.length) parts.push('aucune différence');
    return parts.join(' · ');
}

/* Tableau comparatif de codes : Valeur P | Étiquette P | Valeur D | Étiquette D.
   3 sections : communs (matching case-insensitive), uniquement parent,
   uniquement doublon. Lignes surlignées quand les étiquettes divergent. */
function buildCodeComparisonHtml(srcCodes, dstCodes) {
    const { src, dst, common, changed, onlySrc, onlyDst } = compareCodes(srcCodes, dstCodes);
    const changedSet = new Set(changed);
    const truncate = (s) => escapeHtml((s || '').slice(0, 60));
    let rows = '';
    for (const k of common) {
        rows += `<tr class="${changedSet.has(k) ? 'table-warning' : ''}">`;
        rows += `<td>${escapeHtml(src[k][0])}</td><td>${truncate(src[k][1])}</td>`;
        rows += `<td>${escapeHtml(dst[k][0])}</td><td>${truncate(dst[k][1])}</td></tr>`;
    }
    for (const k of onlySrc) {
        rows += `<tr class="table-info">`;
        rows += `<td>${escapeHtml(src[k][0])}</td><td>${truncate(src[k][1])}</td><td></td><td></td></tr>`;
    }
    for (const k of onlyDst) {
        rows += `<tr class="table-danger">`;
        rows += `<td></td><td></td><td>${escapeHtml(dst[k][0])}</td><td>${truncate(dst[k][1])}</td></tr>`;
    }

    let html = `<div class="code-compare-wrap">`;
    html += `<table class="table table-sm table-bordered code-table mb-1">`;
    html += `<thead><tr><th>Valeur maître</th><th>Étiquette maître</th>`;
    html += `<th>Valeur doublon</th><th>Étiquette doublon</th></tr></thead>`;
    html += `<tbody>${rows}</tbody></table></div>`;
    html += `<small class="text-muted"><span class="legend-swatch table-warning"></span> libellé différent `;
    html += `<span class="legend-swatch table-info"></span> seulement maître `;
    html += `<span class="legend-swatch table-danger"></span> seulement doublon</small>`;
    return html;
}

/* Indicateurs de liste suspecte (quality.py) : libellé court et infobulle */
const QUALITY_FLAGS = {
    placeholder: ['renvoi', 'Les codes renvoient ailleurs (« Voir dictionnaire des nomenclatures », « Code quartier… ») : la liste est comparée sur son nom'],
    numeric_range: ['numérique', 'Représentation numérique mise en liste (« 0 à 7 », « de 0 à N ») : un type numérique avec bornes conviendrait mieux'],
    single_code: ['code unique', 'Un seul code métier'],
};

function qualityBadgeHtml(flags) {
    if (!flags?.length) return '';
    const help = flags.map(f => (QUALITY_FLAGS[f] || [f, f])[1]).join(' · ');
    return ` <span class="badge bg-warning text-dark" title="${escapeHtml(help)}">⚠</span>`;
}

/* Mention ajoutée aux confirmations d'ajout en lot quand des listes suspectes en font partie */
function suspectWarning(ids) {
    const n = ids.filter(id => (registryData[id]?.quality_flags || []).length).length;
    return n ? `\n\n⚠ ${n} liste(s) suspecte(s) en font partie (filtre « Listes suspectes »).` : '';
}

/* Libellé affiché et infobulle de chaque type de détection */
const DETECTION_LABELS = {
    exact: ['exact', 'mêmes codes (valeurs et libellés)'],
    exact_business: ['exact hors échappement', 'mêmes codes métier ; seuls les codes d\'échappement (sans objet, non réponse…) diffèrent'],
    labels: ['libellés', 'mêmes libellés de codes métier, valeurs différentes (ex. 1/0 et oui/non)'],
    fuzzy: ['fuzzy', 'codes et noms proches'],
    usage: ['usage', 'mêmes variables référentes'],
    semantic_list: ['sémantique', 'proximité sémantique des listes (embeddings)'],
    semantic_var: ['sémantique (variables)', 'proximité sémantique via les variables'],
    lineage: ['lignée', 'copiée de la maître (BasedOnObject DDI), contenu éventuellement modifié depuis'],
};

const DETECTION_BADGES = {
    exact: 'success',
    exact_business: 'success',
    labels: 'primary',
    fuzzy: 'warning text-dark',
    semantic_list: 'info text-dark',
    semantic_var: 'info text-dark',
    usage: 'secondary',
    lineage: 'dark',
};

const DECISION_BUTTONS = [
    ['approve', '✓ Approuver', 'success', 'a'],
    ['reject', '✗ Rejeter', 'danger', 'r'],
    ['pending', '⏳ En attente', 'secondary', 'p'],
];

function renderDuplicateCard(cl, dup) {
    const conf = dup.confidence || 0;
    const decision = decisionOf(dup);
    const dupName = dup.name || 'Inconnu';
    const compareOpen = openComparisons.has(dup.id);

    let html = `<div class="dup-card ${decision} p-2 mb-2${dup.id === activeDupId ? ' dup-active' : ''}" `;
    html += `data-dup-id="${escapeHtml(dup.id)}" tabindex="-1">`;
    html += `<div class="d-flex justify-content-between align-items-start gap-2 flex-wrap">`;
    html += `<div class="flex-grow-1"><strong>${escapeHtml(dupName)}</strong>`;
    html += ` <code class="dup-id">${escapeHtml(dup.id)}</code>`;
    html += ` <button type="button" class="btn btn-link p-0 copy-id-btn" data-copy-id="${escapeHtml(dup.id)}" title="Copier l'ID" aria-label="Copier l'ID">📋</button>`;
    if (dup.origin === 'registry') html += ` <span class="badge bg-primary">📘 registre</span>`;
    html += qualityBadgeHtml(dup.quality_flags);
    html += ` ${levelBadgeHtml(dup)}`;
    if (dup.label) html += `<br><small class="text-muted">${escapeHtml(dup.label)}</small>`;
    if (dup.suggested_official) html += `<small>${suggestedOfficialHtml(dup)}</small>`;

    // Types de détection + confiance
    html += `<div class="mt-1">`;
    for (const type of dup.detection_types || []) {
        const [label, help] = DETECTION_LABELS[type] || [type, type];
        html += `<span class="badge badge-detection bg-${DETECTION_BADGES[type] || 'secondary'} me-1" `;
        html += `title="Signal de détection : ${escapeHtml(help)}">${escapeHtml(label)}</span>`;
    }
    const confClass = conf >= 0.95 ? 'confidence-high' : conf >= 0.7 ? 'confidence-medium' : 'confidence-low';
    html += `<span class="confidence-pill ${confClass}" title="Confiance de la détection">${Math.round(conf * 100)} %</span>`;
    html += `</div>`;

    // Résumé du diff des codes, visible sans ouvrir le tableau
    html += `<div class="small mt-1 diff-summary">${codeDiffSummaryHtml(cl.codes || [], dup.codes || [])}</div>`;
    html += `</div>`;

    // Décision : 3 boutons (état courant mis en évidence)
    html += `<div class="text-end"><div class="btn-group btn-group-sm decision-group" role="group" aria-label="Décision">`;
    for (const [value, label, color, key] of DECISION_BUTTONS) {
        const on = decision === value;
        html += `<button type="button" class="btn ${on ? `btn-${color}` : `btn-outline-${color}`}" `;
        html += `data-decision="${value}" aria-pressed="${on}" title="${label.slice(2)} (${key})">${label}</button>`;
    }
    html += `</div>`;
    if (dup.decision_carried) {
        html += `<div><small class="text-muted" title="Décision reprise d'un run précédent (decisions.json)">↻ reprise</small></div>`;
    }
    html += `</div></div>`;

    // Codes d'échappement (sans objet, non réponse…) de part et d'autre
    const escMaster = cl.escape_codes || [];
    const escDup = dup.escape_codes || [];
    if (escMaster.length || escDup.length) {
        const fmt = codes => codes.length
            ? codes.map(c => escapeHtml(`${c[0]} ${c[1] || ''}`.trim())).join(' · ')
            : 'aucun';
        html += `<div class="small text-muted mt-1">Codes d'échappement — maître : ${fmt(escMaster)}`;
        html += ` | doublon : ${fmt(escDup)}</div>`;
    }

    // Lien indirect : aucune paire détectée directement avec la CodeList principale
    const nameInGroup = id => escapeHtml((cl.duplicates || []).find(d => d.id === id)?.name || id);
    if (dup.via?.length) {
        html += `<div class="small text-muted mt-1">↪ lien indirect, via ${dup.via.map(nameInGroup).join(', ')}</div>`;
    }

    // Filiation DDI (BasedOnObject) entre le doublon et les listes du groupe
    const nameOf = id => id === cl.id ? 'la maître' : nameInGroup(id);
    const groupIds = new Set([cl.id, ...(cl.duplicates || []).map(d => d.id)]);
    const basedOn = (dup.based_on || []).filter(id => groupIds.has(id));
    if (basedOn.length) {
        html += `<div class="small text-muted mt-1" title="BasedOnObject DDI">↳ basé sur ${basedOn.map(nameOf).join(', ')}</div>`;
    }
    if ((cl.based_on || []).includes(dup.id)) {
        html += `<div class="small text-muted mt-1" title="BasedOnObject DDI">↰ la maître est basée sur ce doublon</div>`;
    }
    const outside = (dup.based_on || []).filter(id => !groupIds.has(id));
    if (outside.length) {
        html += `<div class="small text-muted mt-1" title="BasedOnObject DDI vers une liste hors du groupe">↳ basé sur ${outside.map(id => `<code>${escapeHtml(id)}</code>`).join(', ')} (hors groupe)</div>`;
    }

    // Autres liens du doublon dans le groupe (paires avec d'autres doublons)
    const otherLinks = (dup.links || []).filter(lk => lk.id !== cl.id && !(dup.via || []).includes(lk.id));
    if (otherLinks.length) {
        const parts = otherLinks.map(lk => `${nameInGroup(lk.id)} (${escapeHtml(lk.detection_types.join(', '))})`);
        html += `<div class="small text-muted mt-1">🔗 lié aussi à ${parts.join(', ')}</div>`;
        if (decision === 'reject') {
            html += `<div class="small text-warning-emphasis">Rejeté par rapport à la maître : `;
            html += `définissez-le comme maître pour examiner ces liens.</div>`;
        }
    }

    // Actions secondaires
    html += `<div class="d-flex flex-wrap gap-2 mt-2">`;
    html += `<button type="button" class="btn btn-sm btn-outline-secondary" data-action="compare" aria-expanded="${compareOpen}">`;
    html += `${compareOpen ? '▾ Masquer' : '▸ Comparer'} les codes (c)</button>`;
    // Promotion en maître (impossible pour une maître du registre nettoyé
    // ou un registre produit sans liens par une version antérieure)
    if (cl.origin !== 'registry' && dup.links) {
        html += `<button type="button" class="btn btn-sm btn-outline-primary" data-action="swap" `;
        html += `title="Faire de ce doublon la CodeList maître du groupe">⇅ Définir comme maître</button>`;
    }
    html += `</div>`;

    if (compareOpen) {
        html += `<div class="code-comparison mt-2">${buildCodeComparisonHtml(cl.codes || [], dup.codes || [])}`;
        html += `<div class="small text-muted mt-2">`;
        html += `<div><strong>Codes :</strong> ${dup.codes_count || 0}`;
        if (dup.version) {
            html += ` · <strong>Référence DDI :</strong> ${escapeHtml(`${dup.agency || '?'}:${dup.id}:${dup.version}`)}`;
        }
        html += `</div>`;
        if (dup.vars?.length) {
            html += `<div><strong>Variables :</strong> ${dup.vars.map(escapeHtml).join(', ')}</div>`;
        }
        if (dup.operations?.length) {
            html += `<div><strong>Opérations :</strong> ${dup.operations.map(escapeHtml).join(', ')}`;
            html += ` · <strong>Séries :</strong> ${(dup.series || []).map(escapeHtml).join(', ')}</div>`;
        }
        if (dup.cat_ids?.length) {
            html += `<div class="text-break"><strong>Catégories :</strong> ${dup.cat_ids.map(escapeHtml).join(', ')}</div>`;
        }
        html += `</div></div>`;
    }
    html += `</div>`;
    return html;
}

/* ------------------------------------------------------------------ */
/* Pagination
/* ------------------------------------------------------------------ */

function renderPagination(total, page, pageSize) {
    TOTAL_PAGES = Math.ceil(total / pageSize) || 1;
    let html = '';

    // Previous
    html += `<li class="page-item ${page <= 0 ? 'disabled' : ''}">`;
    html += `<a class="page-link" href="#" data-page="${page - 1}">← Précédent</a></li>`;

    // Page numbers (max 5 visible)
    const startPage = Math.max(0, page - 2);
    const endPage = Math.min(TOTAL_PAGES - 1, page + 2);

    for (let i = startPage; i <= endPage; i++) {
        html += `<li class="page-item ${i === page ? 'active' : ''}">`;
        html += `<a class="page-link" href="#" data-page="${i}">${i + 1}</a></li>`;
    }

    // Next
    html += `<li class="page-item ${page >= TOTAL_PAGES - 1 ? 'disabled' : ''}">`;
    html += `<a class="page-link" href="#" data-page="${page + 1}">Suivant →</a></li>`;
    html += `<li class="page-item disabled"><span class="page-link">${total} CodeList(s)</span></li>`;

    for (const id of ['pagination', 'paginationBottom']) {
        document.getElementById(id).innerHTML = html;
    }
}

function goToPage(page) {
    if (page === currentPage || page < 0 || page >= TOTAL_PAGES) return;
    currentPage = page;
    selectedClId = null;
    renderCodelistsFiltered();
}

/* ------------------------------------------------------------------ */
/* Decision updates (enregistrées automatiquement par le serveur)
/* ------------------------------------------------------------------ */

function findDuplicate(clId, dupId) {
    return (registryData?.[clId]?.duplicates || []).find(d => d.id === dupId);
}

function applyLocalDecision(clId, dupId, decision) {
    const dup = findDuplicate(clId, dupId);
    if (!dup) return;
    dup.decision = decision;
    delete dup.decision_carried;
}

/* Rafraîchit ce qui dépend des décisions sans refiltrer la liste de gauche
   (une liste décidée reste visible jusqu'au prochain changement de filtre) */
function refreshAfterDecisions() {
    updateStats();
    renderCodelistNav();
    updateBulkCounts();
}

async function updateDecision(clId, dupId, decision) {
    const dup = findDuplicate(clId, dupId);
    if (!dup) return;
    const previous = { decision: decisionOf(dup), carried: dup.decision_carried };
    if (previous.decision === decision) return;

    // Affichage immédiat, annulé si le serveur refuse
    applyLocalDecision(clId, dupId, decision);
    if (clId === selectedClId) refreshDuplicateCard(clId, dupId);
    refreshAfterDecisions();

    try {
        const data = await postJson(regUrl('api/registry/decision'),
            { cl_id: clId, dup_id: dupId, decision }, 'PATCH');
        setSaveStatus(data);
    } catch (err) {
        console.error('Update decision:', err);
        dup.decision = previous.decision;
        if (previous.carried) dup.decision_carried = true;
        if (clId === selectedClId) refreshDuplicateCard(clId, dupId);
        refreshAfterDecisions();
        showToast(`Décision non prise en compte : ${err.message}`, 'danger');
        return;
    }
    if (clId === selectedClId && decision !== 'pending') advanceAfterDecision(clId);
}

/* Après une décision : doublon en attente suivant, puis (option) CodeList suivante */
function advanceAfterDecision(clId) {
    const dups = visibleDuplicates(registryData[clId]);
    const idx = dups.findIndex(d => d.id === activeDupId);
    const next = [...dups.slice(idx + 1), ...dups.slice(0, Math.max(idx, 0))]
        .find(d => decisionOf(d) === 'pending');
    if (next) {
        setActiveDup(next.id);
        return;
    }
    if (document.getElementById('autoAdvance').checked) selectNextPending();
}

/* ------------------------------------------------------------------ */
/* Actions de masse : sur les doublons affichés par le filtre courant
/* ------------------------------------------------------------------ */

const BULK_ACTIONS = {
    approveExact: {
        decision: 'approve',
        label: 'Approuver les doublons exacts en attente',
        match: d => decisionOf(d) === 'pending' && (d.detection_types || []).includes('exact'),
    },
    approveHigh: {
        decision: 'approve',
        label: 'Approuver les doublons en attente de confiance ≥ 95 %',
        match: d => decisionOf(d) === 'pending' && (d.confidence || 0) >= 0.95,
    },
    rejectAll: {
        decision: 'reject',
        label: 'Rejeter les doublons affichés',
        match: d => decisionOf(d) !== 'reject',
        danger: true,
    },
    pendingAll: {
        decision: 'pending',
        label: 'Remettre en attente les doublons affichés',
        match: d => decisionOf(d) !== 'pending',
        danger: true,
    },
};

/* Doublons visibles de toutes les CodeLists du filtre (toutes pages) */
function bulkTargets(action) {
    const items = [];
    for (const cl of filteredItems) {
        for (const dup of visibleDuplicates(registryData[cl.id])) {
            if (action.match(dup)) items.push({ cl_id: cl.id, dup_id: dup.id, decision: action.decision });
        }
    }
    return items;
}

function updateBulkCounts() {
    if (!registryData) return;
    document.getElementById('bulkExactCount').textContent = bulkTargets(BULK_ACTIONS.approveExact).length;
    document.getElementById('bulkHighCount').textContent = bulkTargets(BULK_ACTIONS.approveHigh).length;
}

async function bulkDecision(key) {
    const action = BULK_ACTIONS[key];
    const items = bulkTargets(action);
    if (!items.length) {
        showToast('Aucun doublon concerné dans l\'affichage courant.', 'info');
        return;
    }
    const lists = new Set(items.map(i => i.cl_id)).size;
    if (!await confirmDialog(
        `${action.label} : ${items.length} doublon(s) dans ${lists} CodeList(s) du filtre courant.\nVous pourrez annuler juste après.`,
        { title: 'Action de masse', confirmLabel: action.label.split(' ')[0], danger: action.danger }
    )) return;
    await applyBatch(items, `${action.label} : ${items.length} doublon(s)`);
}

/* Applique un lot de décisions ; le toast propose d'annuler (lot inverse) */
async function applyBatch(items, message, { undo = false } = {}) {
    try {
        const data = await postJson(regUrl('api/registry/decisions'), { items });
        for (const item of items) applyLocalDecision(item.cl_id, item.dup_id, item.decision);
        setSaveStatus(data);
        renderCodelistDetail(selectedClId);
        refreshAfterDecisions();
        if (undo) {
            showToast(`Annulé : ${data.count} décision(s) rétablie(s)`, 'info');
        } else {
            showToast(message, 'success', {
                actionLabel: 'Annuler',
                onAction: () => applyBatch(data.previous, '', { undo: true }),
            });
        }
    } catch (err) {
        showToast(`Erreur: ${err.message}`, 'danger');
    }
}

/* ------------------------------------------------------------------ */
/* Navigation au clavier
/* ------------------------------------------------------------------ */

function selectRelative(step) {
    const idx = filteredItems.findIndex(cl => cl.id === selectedClId);
    const target = filteredItems[idx + step];
    if (!target) return;
    const pageSize = getFilters().pageSize;
    const page = Math.floor((idx + step) / pageSize);
    if (page !== currentPage) {
        selectedClId = target.id;
        renderCodelistsFiltered();
    } else {
        selectCodelist(target.id);
    }
}

/* Prochaine CodeList du filtre ayant des doublons en attente (après la courante, en boucle) */
function selectNextPending() {
    const idx = filteredItems.findIndex(cl => cl.id === selectedClId);
    const ordered = [...filteredItems.slice(idx + 1), ...filteredItems.slice(0, Math.max(idx, 0))];
    const next = ordered.find(cl => pendingCount(visibleDuplicates(registryData[cl.id])) > 0);
    if (!next) {
        showToast('Plus aucun doublon en attente dans ce filtre.', 'success');
        return;
    }
    selectedClId = next.id;
    activeDupId = null;
    openComparisons.clear();
    renderCodelistsFiltered();
}

function moveActiveDup(step) {
    const dups = visibleDuplicates(registryData?.[selectedClId]);
    if (!dups.length) return;
    const idx = dups.findIndex(d => d.id === activeDupId);
    const next = dups[Math.min(dups.length - 1, Math.max(0, idx + step))];
    if (next) setActiveDup(next.id);
}

function isTyping(target) {
    return target.closest('input, textarea, select, [contenteditable="true"]');
}

function handleValidationKeys(e) {
    if (!registryData || e.ctrlKey || e.metaKey || e.altKey) return;
    if (!document.getElementById('validation').classList.contains('active')) return;
    if (document.querySelector('.modal.show') || isTyping(e.target)) return;

    const keys = {
        j: () => selectRelative(1),
        k: () => selectRelative(-1),
        n: selectNextPending,
        ArrowDown: () => moveActiveDup(1),
        ArrowUp: () => moveActiveDup(-1),
        a: () => activeDupId && updateDecision(selectedClId, activeDupId, 'approve'),
        r: () => activeDupId && updateDecision(selectedClId, activeDupId, 'reject'),
        p: () => activeDupId && updateDecision(selectedClId, activeDupId, 'pending'),
        c: () => activeDupId && toggleComparison(activeDupId),
        '/': () => document.getElementById('searchFilter').focus(),
        '?': () => bootstrap.Modal.getOrCreateInstance(document.getElementById('shortcutsModal')).show(),
    };
    const handler = keys[e.key];
    if (!handler) return;
    e.preventDefault();
    handler();
}

/* ------------------------------------------------------------------ */
/* État dans l'URL : run ouvert, filtres, page, CodeList affichée
/* ------------------------------------------------------------------ */

const HASH_FIELDS = {
    f: 'decisionFilter',
    o: 'originFilter',
    q: 'searchFilter',
    s: 'sortBy',
    n: 'pageSize',
};

function writeHash() {
    if (!registryPath) return;
    const params = new URLSearchParams({ run: registryPath });
    for (const [key, id] of Object.entries(HASH_FIELDS)) {
        const value = document.getElementById(id).value;
        if (value) params.set(key, value);
    }
    if (currentPage) params.set('p', currentPage);
    if (selectedClId) params.set('cl', selectedClId);
    history.replaceState(null, '', `#${params}`);
}

function restoreFromHash() {
    const params = new URLSearchParams(location.hash.slice(1));
    const run = params.get('run');
    if (!run) return false;
    for (const [key, id] of Object.entries(HASH_FIELDS)) {
        const el = document.getElementById(id);
        const value = params.get(key);
        // Une valeur inconnue d'un <select> le laisserait vide
        if (value !== null && (el.tagName !== 'SELECT' || [...el.options].some(o => o.value === value))) {
            el.value = value;
        }
    }
    openRegistry(run, { page: parseInt(params.get('p') || '0', 10), cl: params.get('cl') });
    return true;
}

/* ------------------------------------------------------------------ */
/* DOM Ready
/* ------------------------------------------------------------------ */

document.addEventListener('DOMContentLoaded', () => {
    document.getElementById('openRunBtn')
        .addEventListener('click', () => openRegistry(document.getElementById('runSelect').value));
    document.getElementById('runSelect')
        .addEventListener('dblclick', () => openRegistry(document.getElementById('runSelect').value));
    document.getElementById('outputRoot').addEventListener('change', (e) => {
        setOutputRoot(e.target.value);
        loadRunOptions();
        if (typeof refreshRunsTable === 'function') refreshRunsTable();
    });
    document.getElementById('loadRegistryBtn').addEventListener('click', loadRegistry);
    document.getElementById('registryPath').addEventListener('keydown', (e) => {
        if (e.key === 'Enter') loadRegistry();
    });
    document.getElementById('saveBtn').addEventListener('click', saveRegistry);
    document.getElementById('downloadBtn').addEventListener('click', downloadRegistry);
    document.getElementById('downloadCleanedBtn').addEventListener('click', downloadCleanedRegistry);

    // Filtres appliqués dès le changement
    const applyNow = () => {
        currentPage = 0;
        selectedClId = null;
        renderCodelistsFiltered();
    };
    for (const id of ['decisionFilter', 'originFilter', 'sortBy', 'pageSize']) {
        document.getElementById(id).addEventListener('change', applyNow);
    }
    let searchTimer = null;
    document.getElementById('searchFilter').addEventListener('input', () => {
        clearTimeout(searchTimer);
        searchTimer = setTimeout(applyNow, 300);
    });

    // Pagination (haut et bas)
    for (const id of ['pagination', 'paginationBottom']) {
        document.getElementById(id).addEventListener('click', (e) => {
            const link = e.target.closest('.page-item:not(.disabled) .page-link[data-page]');
            if (!link) return;
            e.preventDefault();
            goToPage(parseInt(link.dataset.page, 10));
        });
    }

    // Liste de gauche : sélection et cases d'ajout en lot
    const nav = document.getElementById('codelistNav');
    nav.addEventListener('click', (e) => {
        const box = e.target.closest('.batch-check');
        if (box) {
            // Cocher une case ne doit pas changer la CodeList affichée à droite
            e.stopPropagation();
            if (box.checked) batchSelected.add(box.dataset.id);
            else batchSelected.delete(box.dataset.id);
            updateBatchBar();
            return;
        }
        const item = e.target.closest('.list-group-item[data-id]');
        if (!item) return;
        e.preventDefault();
        selectCodelist(item.dataset.id);
    });

    // Colonne de droite : décisions, comparaison, maître, registre nettoyé
    document.getElementById('codelistDetail').addEventListener('click', (e) => {
        const card = e.target.closest('.dup-card');
        const btn = e.target.closest('button');
        if (card && card.dataset.dupId !== activeDupId) setActiveDup(card.dataset.dupId, { scroll: false });
        if (!btn) return;

        if (btn.dataset.decision && card) {
            updateDecision(selectedClId, card.dataset.dupId, btn.dataset.decision);
            return;
        }
        switch (btn.dataset.action) {
            case 'compare': return toggleComparison(card.dataset.dupId);
            case 'swap': return swapParent(selectedClId, card.dataset.dupId, btn);
            case 'add-clean': return addToCleaned(selectedClId);
            case 'remove-clean': return removeFromCleaned([selectedClId], btn);
            case 'dismiss': return setDismissed(selectedClId, true, btn);
            case 'undismiss': return setDismissed(selectedClId, false, btn);
        }
    });

    // Ajout en lot au registre nettoyé
    document.getElementById('batchSelectAll')
        .addEventListener('change', (e) => toggleSelectAllVisible(e.target.checked));
    document.getElementById('batchAddBtn')
        .addEventListener('click', (e) => addBatchToCleaned(e.currentTarget));
    document.getElementById('addAllNoDupBtn')
        .addEventListener('click', (e) => addAllWithoutDuplicates(e.currentTarget));

    // Actions de masse (sur les doublons affichés)
    document.getElementById('bulkApproveExact').addEventListener('click', () => bulkDecision('approveExact'));
    document.getElementById('bulkApproveHighConf').addEventListener('click', () => bulkDecision('approveHigh'));
    document.getElementById('bulkRejectAll').addEventListener('click', () => bulkDecision('rejectAll'));
    document.getElementById('bulkPendingAll').addEventListener('click', () => bulkDecision('pendingAll'));

    // Préférence « passer à la CodeList suivante »
    const autoAdvance = document.getElementById('autoAdvance');
    autoAdvance.checked = loadPref('autoAdvance', '0') === '1';
    autoAdvance.addEventListener('change', () => savePref('autoAdvance', autoAdvance.checked ? '1' : '0'));

    // Raccourcis clavier et aide
    document.addEventListener('keydown', handleValidationKeys);
    document.getElementById('shortcutsHelpBtn').addEventListener('click', () => {
        bootstrap.Modal.getOrCreateInstance(document.getElementById('shortcutsModal')).show();
    });
    new bootstrap.Tooltip(document.getElementById('legendHelp'));

    loadRunOptions();
    restoreFromHash();
});
