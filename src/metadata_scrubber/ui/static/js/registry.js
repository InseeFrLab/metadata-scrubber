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
    select.innerHTML = '<option value="">Chargement des runs…</option>';
    try {
        const runs = await fetchRuns(root);
        if (!runs.length) {
            select.innerHTML = '<option value="">Aucun run dans ce dossier</option>';
            return;
        }
        select.innerHTML = runs.map(run => {
            const source = runSourceLabel(run.xml_source);
            const status = run.status === 'error' ? ' · en échec' : '';
            const n = run.counts?.candidates != null ? ` · ${run.counts.candidates} candidats` : '';
            const label = `${formatRunId(run.run_id)}${source ? ` · ${source}` : ''}${n}${status}`;
            return `<option value="${escapeHtml(run.registry_path)}"${run.status === 'error' ? ' disabled' : ''}>${escapeHtml(label)}</option>`;
        }).join('');
        if (registryPath && runs.some(r => r.registry_path === registryPath)) {
            select.value = registryPath;
        }
    } catch (err) {
        select.innerHTML = '<option value="">Runs indisponibles</option>';
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
        showOpenRun(path);

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

/* Run ouvert, rappelé dans la barre du haut */
function showOpenRun(path) {
    const parts = path.split('/');
    const runId = parts.length > 1 ? parts[parts.length - 2] : '';
    const label = /^\d{8}-\d{4}/.test(runId) ? `Run du ${formatRunId(runId)}` : parts.pop();
    document.getElementById('ctxRunLabel').textContent = label;
    document.getElementById('ctxRun').title = `Run ouvert dans Valider : ${path}`;
    document.getElementById('ctxRun').classList.remove('d-none');
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

/* Ids déjà présents dans le registre nettoyé : le repère « référentiel » reste fiable d'une session à l'autre */
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
    setBusy(btn, true, 'Mise à jour…');

    try {
        const resp = await fetch(regUrl('api/registry/save'), { method: 'POST' });
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        const data = await resp.json();
        showToast(
            `Référentiel mis à jour (${data.cleaned_count ?? 0} entrées)`
            + ` · ${data.decisions_count ?? 0} décision(s) conservée(s) pour les prochains runs`,
            'success'
        );
        if (data.cleaned_path) document.getElementById('cleanedPath').value = data.cleaned_path;
        setSaveStatus({ saved: true });
        invalidateCleaned();
    } catch (err) {
        showToast(`Erreur: ${err.message}`, 'danger');
    } finally {
        setBusy(btn, false);
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
            showToast('Pas encore de référentiel : utilisez « Mettre à jour le référentiel ».', 'warning');
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
        el.innerHTML = `<span class="save-error">${icon('alert')} Échec de l'enregistrement</span> `
            + '<button type="button" class="btn btn-quiet btn-sm py-0" id="retrySaveBtn">Réessayer</button>';
        el.title = result.save_error || '';
        document.getElementById('retrySaveBtn').addEventListener('click', retrySave);
        return;
    }
    const at = result.saved_at ? new Date(result.saved_at) : new Date();
    el.innerHTML = `<span class="saved">${icon('check')} Enregistré ${at.toLocaleTimeString('fr-FR', { hour: '2-digit', minute: '2-digit' })}</span>`;
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
    const total = stats.total_duplicates;
    const pct = (n) => (total ? (100 * n / total) : 0);
    document.getElementById('decisionSegOk').style.width = `${pct(stats.approved)}%`;
    document.getElementById('decisionSegKo').style.width = `${pct(stats.rejected)}%`;
    const decided = stats.approved + stats.rejected;
    document.getElementById('decisionProgress').setAttribute('aria-label',
        `${decided} décisions sur ${total} doublons`);
    document.getElementById('decisionLegend').innerHTML =
        `<span><b class="num">${decided}</b> / <span class="num">${total}</span> décisions`
        + ` · <span class="num">${stats.total_code_lists}</span> CodeLists</span>`
        + `<span class="ok"><span class="dot"></span> <span class="num">${stats.approved}</span> approuvés</span>`
        + `<span class="ko"><span class="dot"></span> <span class="num">${stats.rejected}</span> rejetés</span>`
        + `<span class="wait"><span class="dot"></span> <span class="num">${stats.pending}</span> en attente</span>`;
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

const PAGE_SIZE = 50;   // CodeLists par page dans la colonne de gauche

function getFilters() {
    return {
        decisionFilter: document.getElementById('decisionFilter').value,
        originFilter: document.getElementById('originFilter').value,
        search: document.getElementById('searchFilter').value.trim(),
        sortBy: document.getElementById('sortBy').value,
        pageSize: PAGE_SIZE,
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

/* Filtres de décision, en pastilles avec leur nombre de CodeLists */
const DECISION_CHIPS = [
    ['todo', 'À traiter'],
    ['pending', 'En attente'],
    ['approve', 'Approuvés'],
    ['reject', 'Rejetés'],
    ['none', 'Sans doublon'],
    ['dismissed', 'Écartées'],
    ['approve,reject,pending', 'Toutes'],
];

function renderDecisionChips(counts) {
    const current = document.getElementById('decisionFilter').value;
    document.getElementById('decisionChips').innerHTML = DECISION_CHIPS.map(([value, label]) =>
        `<button type="button" class="chip" data-filter="${value}" aria-pressed="${value === current}">`
        + `${label}<span class="num">${counts[value] ?? 0}</span></button>`
    ).join('');
}

function renderCodelistsFiltered() {
    if (!registryData) return;
    const { decisionFilter, originFilter, search, sortBy, pageSize } = getFilters();
    const searchLower = search.toLowerCase();

    const items = [];
    const chipCounts = {};
    for (const [clId, clData] of Object.entries(registryData)) {

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

        // Compteurs des pastilles : mêmes filtres, hors filtre de décision
        for (const [value] of DECISION_CHIPS) {
            if (matchesDecisionFilter(value, clId, clData)) chipCounts[value] = (chipCounts[value] || 0) + 1;
        }
        if (!matchesDecisionFilter(decisionFilter, clId, clData)) continue;

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

    renderDecisionChips(chipCounts);
    document.getElementById('listCount').textContent =
        `${items.length} CodeList${items.length > 1 ? 's' : ''}`;
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
        nav.innerHTML = '<div class="empty">Aucune CodeList pour ce filtre.</div>';
        updateBatchBar();
        return;
    }

    let html = '';
    for (const cl of lastNavItems) {
        const data = registryData[cl.id] || {};
        const dups = data.duplicates || [];
        const added = inCleaned.has(cl.id);
        const dismissed = !!data.dismissed;
        const active = cl.id === selectedClId;
        html += `<div class="cl-item${active ? ' active' : ''}${dismissed ? ' dismissed' : ''}" role="option" tabindex="0" `;
        html += `data-id="${escapeHtml(cl.id)}" aria-selected="${active}">`;
        // Case d'ajout en lot : uniquement pour les listes sans doublon pas encore ajoutées
        html += `<span class="lead">`;
        if (!dups.length && !added && !dismissed) {
            html += `<input type="checkbox" class="form-check-input batch-check" `;
            html += `aria-label="Sélectionner ${escapeHtml(cl.name)}" `;
            html += `data-id="${escapeHtml(cl.id)}"${batchSelected.has(cl.id) ? ' checked' : ''}>`;
        } else {
            html += navStateHtml(dups, added);
        }
        html += `</span>`;
        html += `<span class="nm"><span>${escapeHtml(cl.name)}</span>`;
        if (data.origin === 'registry') html += `<span class="text-faint" title="Liste du référentiel">${icon('book')}</span>`;
        html += qualityBadgeHtml(data.quality_flags);
        html += `</span>`;
        html += navCountHtml(dups, added, dismissed);
        html += `<span class="sub"><span class="mono" title="${escapeHtml(cl.id)}">${escapeHtml(cl.id)}</span>`;
        html += `<span title="Variables qui référencent cette CodeList">${cl.varsCount} var.</span>`;
        if (cl.seriesCount) {
            html += `<span title="Séries d'opérations qui utilisent cette CodeList">${cl.seriesCount} série(s)</span>`;
        }
        html += `</span></div>`;
    }
    nav.innerHTML = html;
    updateBatchBar();
    nav.querySelector('.cl-item.active')?.scrollIntoView({ block: 'nearest' });
}

/* Rond d'état : en attente (ambre), tout décidé (vert), dans le référentiel (bleu), sans doublon (pointillé) */
function navStateHtml(dups, added) {
    if (added) return '<span class="state in-cleaned" title="Dans le référentiel"></span>';
    if (!dups.length) return '<span class="state none" title="Sans doublon"></span>';
    return pendingCount(dups)
        ? '<span class="state" title="Doublons en attente"></span>'
        : '<span class="state done" title="Tout est décidé"></span>';
}

/* Pastille de droite : nombre de doublons en attente, ou ✓ quand tout est décidé */
function navCountHtml(dups, added, dismissed) {
    if (!dups.length) {
        const text = added ? 'référentiel' : dismissed ? 'écartée' : 'aucun';
        return `<span class="count none">${text}</span>`;
    }
    const approved = dups.filter(d => decisionOf(d) === 'approve').length;
    const rejected = dups.filter(d => decisionOf(d) === 'reject').length;
    const pending = dups.length - approved - rejected;
    const title = `${dups.length} doublon(s) : ${approved} approuvé(s), ${rejected} rejeté(s), ${pending} en attente`;
    return pending
        ? `<span class="count" title="${title}">${pending}</span>`
        : `<span class="count done" title="${title}">${icon('check', 'sm')}${dups.length}</span>`;
}

function selectCodelist(id, { keepActive = false } = {}) {
    if (id !== selectedClId) openComparisons.clear();
    selectedClId = id;
    if (!keepActive) activeDupId = null;
    document.querySelectorAll('#codelistNav .cl-item').forEach(el => {
        const on = el.dataset.id === id;
        el.classList.toggle('active', on);
        el.setAttribute('aria-selected', on);
        if (on) el.scrollIntoView({ block: 'nearest' });
    });
    renderCodelistDetail(id);
    writeHash();
}

/* En-tête de la CodeList maître affichée à droite (nom, URN, niveau, provenance) */
function renderDetailHeader(id) {
    const cl = registryData[id];
    if (!cl) return '';
    const dups = cl.duplicates || [];

    let html = `<div class="master-top"><div>`;
    html += `<div class="eyebrow">${dups.length ? 'CodeList maître' : 'CodeList'}</div>`;
    html += `<h2>${escapeHtml(cl.name || id)}</h2>`;
    if (cl.label && cl.label !== cl.name) html += `<div class="text-muted small">${escapeHtml(cl.label)}</div>`;
    html += `</div><div class="tags">`;
    if (cl.origin === 'registry') html += `<span class="tag accent">${icon('book', 'sm')}référentiel</span>`;
    if (cl.master_chosen) html += `<span class="tag" title="Maître choisie par l'utilisateur, conservée aux runs suivants">${icon('swap', 'sm')}maître choisie</span>`;
    html += levelTagHtml(cl.level, cl.level_suggested);
    for (const flag of cl.quality_flags || []) {
        const [label, help] = QUALITY_FLAGS[flag] || [flag, flag];
        html += `<span class="tag warn" title="${escapeHtml(help)}">${icon('alert', 'sm')}${escapeHtml(label)}</span>`;
    }
    html += `</div></div>`;

    html += `<div class="meta">`;
    const ref = cl.version ? `${cl.agency || '?'}:${id}:${cl.version}` : id;
    html += `<span>${cl.version ? 'Référence' : 'ID'} <span class="mono">${escapeHtml(ref)}</span> `;
    html += `<button type="button" class="copy-id-btn" data-copy-id="${escapeHtml(id)}" title="Copier l'ID" aria-label="Copier l'ID">${icon('copy', 'sm')}</button></span>`;
    html += `<span>${cl.codes_count ?? (cl.codes || []).length} codes</span>`;
    html += `<span>${(cl.vars || []).length} variable(s)</span>`;
    if ((cl.cat_ids || []).length) html += `<span>${cl.cat_ids.length} catégories</span>`;
    html += provenanceHtml(cl);
    html += `</div>`;
    html += suggestedOfficialHtml(cl);
    return html;
}

/* Liste « Voir dictionnaire… » : nomenclature officielle qu'elle désigne */
function suggestedOfficialHtml(cl) {
    if (!cl.suggested_official) return '';
    return `<div class="hint" title="Liste qui renvoie à une nomenclature : la remplacer par la nomenclature officielle">`
        + `${icon('link')}Renvoi vers une nomenclature : remplacer par `
        + `<strong>${escapeHtml(cl.suggested_official.name)}</strong></div>`;
}

/* Opérations et séries qui utilisent la liste (source API) */
function provenanceHtml(cl) {
    if (!(cl.operations || []).length) return '';
    return `<span title="Opérations statistiques qui utilisent cette CodeList">Opérations `
        + `<span class="mono">${cl.operations.map(escapeHtml).join(', ')}</span></span>`
        + `<span title="Séries de ces opérations">Séries <span class="mono">${(cl.series || []).map(escapeHtml).join(', ')}</span></span>`;
}

/* Tableau des modalités (valeur | libellé) d'une CodeList */
function buildCodesTableHtml(codes) {
    if (!codes || !codes.length) {
        return '<div class="text-muted small">Aucune modalité.</div>';
    }
    let rows = '';
    for (const pair of codes) {
        rows += `<tr><td>${escapeHtml(pair[0])}</td><td>${escapeHtml(pair.length > 1 ? pair[1] : '')}</td></tr>`;
    }
    return `<div class="code-compare-wrap"><table class="codes">`
        + `<thead><tr><th>Code</th><th>Libellé</th></tr></thead><tbody>${rows}</tbody></table></div>`;
}

/* Colonne de droite : la maître, puis ses doublons */
function renderCodelistDetail(id) {
    const detail = document.getElementById('codelistDetail');
    const cl = id && registryData ? registryData[id] : null;
    if (!cl) {
        detail.innerHTML = '<div class="panel empty">Sélectionnez une CodeList dans la liste.</div>';
        return;
    }
    const allDups = cl.duplicates || [];
    const dups = visibleDuplicates(cl);

    // Doublon actif par défaut : le premier en attente, sinon le premier ; sa
    // comparaison des codes est ouverte d'office
    if (!dups.some(d => d.id === activeDupId)) {
        activeDupId = (dups.find(d => decisionOf(d) === 'pending') || dups[0])?.id || null;
        if (activeDupId) openComparisons.add(activeDupId);
    }

    let html = `<div class="panel master"><div id="detailHeader">${renderDetailHeader(id)}</div>`;
    html += `<details class="codes-details"${allDups.length ? '' : ' open'}>`;
    html += `<summary>Modalités (${(cl.codes || []).length})</summary>`;
    html += `<div class="mt-2">${buildCodesTableHtml(cl.codes)}</div></details></div>`;

    if (allDups.length) {
        // Faux doublons : tous rejetés, la maître peut entrer seule au référentiel
        if (!allDups.some(d => decisionOf(d) !== 'reject')) {
            html += `<div class="panel nodup">`;
            if (inCleaned.has(id)) {
                html += `<span class="tag accent">${icon('book', 'sm')}Dans le référentiel</span>`;
                html += `<span class="actions"><button type="button" class="btn btn-danger-quiet btn-sm" data-action="remove-clean">`;
                html += `${icon('undo')}Retirer du référentiel</button></span>`;
            } else {
                html += `<span>Aucun doublon retenu : gardez cette liste dans le référentiel.</span>`;
                html += `<span class="actions"><button type="button" class="btn btn-primary btn-sm" data-action="add-clean">`;
                html += `${icon('plus')}Ajouter au référentiel</button></span>`;
            }
            html += `</div>`;
        }
        if (dups.length !== allDups.length) {
            html += `<div class="text-muted small">${dups.length} doublon(s) sur ${allDups.length} affiché(s) : filtre actif.</div>`;
        }
        for (const dup of dups) html += renderDuplicateCard({ ...cl, id }, dup);
    } else {
        // Liste sans doublon détecté → ajout manuel au référentiel
        html += `<div class="panel nodup">`;
        if (inCleaned.has(id)) {
            html += `<span class="tag accent">${icon('book', 'sm')}Dans le référentiel</span>`;
            html += `<span class="actions"><button type="button" class="btn btn-danger-quiet btn-sm" data-action="remove-clean">`;
            html += `${icon('undo')}Retirer du référentiel</button></span>`;
        } else if (cl.dismissed) {
            html += `<span class="tag">${icon('ban', 'sm')}Écartée</span>`;
            html += `<span>Elle ne sera pas ajoutée au référentiel.</span>`;
            html += `<span class="actions"><button type="button" class="btn btn-quiet btn-sm" data-action="undismiss">`;
            html += `${icon('undo')}Annuler l'écart</button></span>`;
        } else {
            html += `<span>Aucun doublon détecté pour cette liste.</span>`;
            html += `<span class="actions"><button type="button" class="btn btn-ghost btn-sm" data-action="dismiss" `;
            html += `title="Ne pas ajouter cette liste au référentiel (conservé entre les runs)">${icon('ban')}Écarter</button>`;
            html += `<button type="button" class="btn btn-primary btn-sm" data-action="add-clean">`;
            html += `${icon('plus')}Ajouter au référentiel</button></span>`;
        }
        html += `</div>`;
    }
    detail.innerHTML = html;
}

/* Réaffiche un seul doublon (après une décision) sans toucher au reste de la page */
function refreshDuplicateCard(clId, dupId) {
    const cl = registryData[clId];
    const dup = (cl?.duplicates || []).find(d => d.id === dupId);
    const card = document.querySelector(`#codelistDetail .dup-card[data-dup-id="${CSS.escape(dupId)}"]`);
    if (!dup || !card) return;
    // Tous rejetés ou plus : la barre d'ajout de la maître apparaît ou disparaît
    const allRejected = !cl.duplicates.some(d => decisionOf(d) !== 'reject');
    if (allRejected !== !!document.querySelector('#codelistDetail .nodup')) {
        renderCodelistDetail(clId);
        return;
    }
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

/* Maître du registre ou doublon de la liste affichée */
function codelistData(clId) {
    return registryData[clId]
        || (registryData[selectedClId]?.duplicates || []).find(d => d.id === clId);
}

async function addToCleaned(clId) {
    const flags = codelistData(clId)?.quality_flags || [];
    if (flags.length && !await confirmDialog(
        `Liste suspecte (${flags.map(f => (QUALITY_FLAGS[f] || [f])[0]).join(', ')}). L'ajouter quand même au référentiel ?`,
        { title: 'Liste suspecte', confirmLabel: 'Ajouter quand même' }
    )) return;
    try {
        const resp = await fetch(regUrl('api/registry/add-to-cleaned'), {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ cl_id: clId }),
        });
        if (resp.status === 409) {
            const detail = (await resp.json().catch(() => ({}))).detail || '';
            if (detail.includes('déjà présente')) {
                showToast('Déjà présente dans le référentiel.', 'warning');
                markInCleaned([clId]);
            } else {
                showToast(detail || 'Ajout refusé.', 'warning');
            }
            return;
        }
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        const data = await resp.json();
        showToast(`Ajoutée au référentiel · ${data.count} entrées`, 'success');
        if (data.path) document.getElementById('cleanedPath').value = data.path;
        markInCleaned([clId]);
        invalidateCleaned();
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
    if (!await confirmDialog(`Retirer ${ids.length} CodeList(s) du référentiel ?`,
        { confirmLabel: 'Retirer', danger: true })) return;
    btn.disabled = true;
    try {
        const data = await postJson(regUrl('api/registry/remove-from-cleaned'), { cl_ids: ids });
        let msg = `${data.removed.length} retirée(s) du référentiel · ${data.count} entrées`;
        if (data.protected.length) {
            msg += ` · ${data.protected.length} protégée(s) (issues de décisions approuvées)`;
        }
        showToast(msg, data.protected.length ? 'warning' : 'success');

        for (const id of [...data.removed, ...data.not_present]) inCleaned.delete(id);
        invalidateCleaned();
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
    // Réaffiche le panneau de droite (maître ou un de ses doublons) pour griser son bouton d'ajout
    const shown = [selectedClId, ...(registryData[selectedClId]?.duplicates || []).map(d => d.id)];
    if (ids.some(id => shown.includes(id))) renderCodelistDetail(selectedClId);
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

        let msg = `${data.added.length} ajoutée(s) au référentiel · ${data.count} entrées`;
        if (data.already_present.length) msg += ` · ${data.already_present.length} déjà présente(s)`;
        const ignored = data.not_found.length + data.skipped_with_duplicates.length;
        if (ignored) msg += ` · ${ignored} ignorée(s)`;
        showToast(msg, ignored ? 'warning' : 'success');

        if (data.path) document.getElementById('cleanedPath').value = data.path;
        markInCleaned([...data.added, ...data.already_present]);
        invalidateCleaned();
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
    if (!await confirmDialog(`Ajouter ${ids.length} CodeList(s) au référentiel ?${suspectWarning(ids)}`,
        { confirmLabel: 'Ajouter' })) return;
    return postBatchToCleaned({ cl_ids: ids }, btn);
}

async function addAllWithoutDuplicates(btn) {
    const ids = listsWithoutDuplicates().filter(id => !inCleaned.has(id));
    if (!ids.length) return;
    if (!await confirmDialog(`Ajouter les ${ids.length} CodeList(s) sans doublon au référentiel ?${suspectWarning(ids)}`,
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
    const same = diff.common.length - diff.changed.length;
    const parts = [`<span>${same} code(s) identique(s)</span>`];
    if (diff.changed.length) parts.push(`<span class="chg">${diff.changed.length} libellé(s) différent(s)</span>`);
    if (diff.onlySrc.length) parts.push(`<span class="only-src">${diff.onlySrc.length} seulement dans la maître</span>`);
    if (diff.onlyDst.length) parts.push(`<span class="only-dst">${diff.onlyDst.length} seulement dans le doublon</span>`);
    return parts.join('');
}

/* Comparaison côte à côte : une ligne par code, libellé de la maître | libellé du
   doublon. Appariement insensible à la casse ; lignes marquées ≠ (libellé ou
   casse différents), − (seulement maître), + (seulement doublon). */
function buildCodeComparisonHtml(srcCodes, dstCodes, srcName, dstName) {
    const { src, dst, common, changed, onlySrc, onlyDst } = compareCodes(srcCodes, dstCodes);
    const changedSet = new Set(changed);
    const codeCell = (k) => {
        const a = src[k]?.[0], b = dst[k]?.[0];
        const shown = a ?? b;
        return a !== undefined && b !== undefined && a !== b
            ? `${escapeHtml(a)} / ${escapeHtml(b)}` : escapeHtml(shown);
    };
    let rows = '';
    for (const k of common) {
        const chg = changedSet.has(k);
        rows += `<tr class="${chg ? 'chg' : ''}"><td class="mk">${chg ? '≠' : ''}</td><td class="code">${codeCell(k)}</td>`;
        rows += `<td>${escapeHtml(src[k][1])}</td>`;
        rows += chg ? `<td>${escapeHtml(dst[k][1])}</td>` : `<td class="same">identique</td>`;
        rows += `</tr>`;
    }
    for (const k of onlySrc) {
        rows += `<tr class="only-src"><td class="mk">−</td><td class="code">${codeCell(k)}</td>`;
        rows += `<td>${escapeHtml(src[k][1])}</td><td class="absent">absent</td></tr>`;
    }
    for (const k of onlyDst) {
        rows += `<tr class="only-dst"><td class="mk">+</td><td class="code">${codeCell(k)}</td>`;
        rows += `<td class="absent">absent</td><td>${escapeHtml(dst[k][1])}</td></tr>`;
    }

    let html = `<div class="code-compare-wrap"><table class="cmp">`;
    html += `<thead><tr><th class="mk"></th><th class="code">Code</th>`;
    html += `<th title="${escapeHtml(srcName)}">Libellé · ${escapeHtml(srcName)}</th>`;
    html += `<th title="${escapeHtml(dstName)}">Libellé · ${escapeHtml(dstName)}</th></tr></thead>`;
    html += `<tbody>${rows}</tbody></table></div>`;
    html += `<div class="cmp-legend"><span><i class="sw chg"></i>≠ libellé différent</span>`;
    html += `<span><i class="sw only-src"></i>− seulement dans la maître</span>`;
    html += `<span><i class="sw only-dst"></i>+ seulement dans le doublon</span></div>`;
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
    return `<span class="flag" title="Liste suspecte : ${escapeHtml(help)}">${icon('alert')}</span>`;
}

/* Mention ajoutée aux confirmations d'ajout en lot quand des listes suspectes en font partie */
function suspectWarning(ids) {
    const n = ids.filter(id => (registryData[id]?.quality_flags || []).length).length;
    return n ? `\n\nAttention : ${n} liste(s) suspecte(s) en font partie (filtre « Listes suspectes »).` : '';
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

const DECISION_BUTTONS = [
    ['approve', 'check', 'Approuver', 'a'],
    ['reject', 'x', 'Rejeter', 'r'],
    ['pending', 'clock', 'Attente', 'p'],
];

function renderDuplicateCard(cl, dup) {
    const conf = dup.confidence || 0;
    const decision = decisionOf(dup);
    const dupName = dup.name || 'Inconnu';
    const compareOpen = openComparisons.has(dup.id);
    const dups = cl.duplicates || [];
    const position = dups.length > 1 ? ` ${dups.findIndex(d => d.id === dup.id) + 1} / ${dups.length}` : '';

    let html = `<div class="panel dup-card ${decision}${dup.id === activeDupId ? ' dup-active' : ''}" `;
    html += `data-dup-id="${escapeHtml(dup.id)}" tabindex="-1">`;
    html += `<div class="dup-head">`;

    // Identité du doublon
    html += `<div class="dup-title"><span class="eyebrow">Doublon${position}</span>`;
    html += `<span class="nm">${escapeHtml(dupName)}</span>`;
    html += `<span class="meta"><span><span class="mono">${escapeHtml(dup.id)}</span> `;
    html += `<button type="button" class="copy-id-btn" data-copy-id="${escapeHtml(dup.id)}" title="Copier l'ID" aria-label="Copier l'ID">${icon('copy', 'sm')}</button></span>`;
    if (dup.operations?.length) html += `<span>Opérations <span class="mono">${dup.operations.map(escapeHtml).join(', ')}</span></span>`;
    if (dup.vars?.length) html += `<span>${dup.vars.length} variable(s)</span>`;
    html += `</span>`;
    if (dup.label && dup.label !== dup.name) html += `<span class="text-muted small">${escapeHtml(dup.label)}</span>`;
    html += `</div>`;

    // Détection, confiance, niveau
    html += `<div class="det">`;
    if (dup.origin === 'registry') html += `<span class="tag accent">${icon('book', 'sm')}référentiel</span>`;
    html += levelTagHtml(dup.level, dup.level_suggested);
    for (const type of dup.detection_types || []) {
        const [label, help] = DETECTION_LABELS[type] || [type, type];
        html += `<span class="tag" title="Signal de détection : ${escapeHtml(help)}">${escapeHtml(label)}</span>`;
    }
    html += `<span class="conf" title="Confiance de la détection">${Math.round(conf * 100)} %</span>`;
    html += qualityBadgeHtml(dup.quality_flags);
    html += `</div>`;

    // Décision : 3 boutons (état courant mis en évidence)
    html += `<div class="decide-col"><div class="decide" role="group" aria-label="Décision">`;
    for (const [value, ico, label, key] of DECISION_BUTTONS) {
        html += `<button type="button" class="${value}" data-decision="${value}" aria-pressed="${decision === value}" `;
        html += `aria-label="${label}" title="${label} (${key})">${icon(ico)}<span class="lbl">${label}</span> <kbd>${key}</kbd></button>`;
    }
    html += `</div>`;
    if (dup.decision_carried) {
        html += `<span class="carried" title="Décision reprise d'un run précédent (decisions.json)">${icon('history', 'sm')}reprise d'un run précédent</span>`;
    }
    html += `</div></div>`;

    // Résumé des différences et actions secondaires
    html += `<div class="diffline">${codeDiffSummaryHtml(cl.codes || [], dup.codes || [])}<span class="spacer"></span>`;
    html += `<button type="button" class="btn btn-ghost btn-sm" data-action="compare" aria-expanded="${compareOpen}">`;
    html += `${icon('columns')}${compareOpen ? 'Masquer' : 'Comparer'} les codes <kbd>c</kbd></button>`;
    // Promotion en maître (impossible pour une maître du référentiel
    // ou un registre produit sans liens par une version antérieure)
    if (cl.origin !== 'registry' && dup.links) {
        html += `<button type="button" class="btn btn-ghost btn-sm" data-action="swap" `;
        html += `title="Faire de ce doublon la CodeList maître du groupe">${icon('swap')}Définir comme maître</button>`;
    }
    html += `</div>`;

    // Notes : renvoi, échappement, liens indirects, filiation DDI
    const notes = [];
    if (dup.suggested_official) {
        notes.push(`${icon('link', 'sm')}<span>Renvoi vers une nomenclature : remplacer par « ${escapeHtml(dup.suggested_official.name)} »</span>`);
    }
    const escMaster = cl.escape_codes || [];
    const escDup = dup.escape_codes || [];
    if (escMaster.length || escDup.length) {
        const fmt = codes => codes.length
            ? codes.map(c => escapeHtml(`${c[0]} ${c[1] || ''}`.trim())).join(' · ')
            : 'aucun';
        notes.push(`${icon('info', 'sm')}<span>Codes d'échappement · maître : ${fmt(escMaster)} · doublon : ${fmt(escDup)}</span>`);
    }
    const nameInGroup = id => escapeHtml(dups.find(d => d.id === id)?.name || id);
    if (dup.via?.length) {
        notes.push(`${icon('link', 'sm')}<span>Lien indirect, via ${dup.via.map(nameInGroup).join(', ')}</span>`);
    }
    const nameOf = id => id === cl.id ? 'la maître' : nameInGroup(id);
    const groupIds = new Set([cl.id, ...dups.map(d => d.id)]);
    const basedOn = (dup.based_on || []).filter(id => groupIds.has(id));
    if (basedOn.length) {
        notes.push(`${icon('git', 'sm')}<span title="BasedOnObject DDI">Copié de ${basedOn.map(nameOf).join(', ')}</span>`);
    }
    if ((cl.based_on || []).includes(dup.id)) {
        notes.push(`${icon('git', 'sm')}<span title="BasedOnObject DDI">La maître est copiée de ce doublon</span>`);
    }
    const outside = (dup.based_on || []).filter(id => !groupIds.has(id));
    if (outside.length) {
        notes.push(`${icon('git', 'sm')}<span title="BasedOnObject DDI vers une liste hors du groupe">Copié de ${outside.map(id => `<code>${escapeHtml(id)}</code>`).join(', ')} (hors groupe)</span>`);
    }
    const otherLinks = (dup.links || []).filter(lk => lk.id !== cl.id && !(dup.via || []).includes(lk.id));
    if (otherLinks.length) {
        const parts = otherLinks.map(lk => `${nameInGroup(lk.id)} (${escapeHtml(lk.detection_types.join(', '))})`);
        notes.push(`${icon('link', 'sm')}<span>Lié aussi à ${parts.join(', ')}</span>`);
        if (decision === 'reject') {
            notes.push(`<span class="warn">Rejeté par rapport à la maître : définissez-le comme maître pour examiner ces liens.</span>`);
        }
    }
    // Faux doublon : le doublon rejeté peut entrer au référentiel à côté de la maître
    if (decision === 'reject' || inCleaned.has(dup.id)) {
        notes.push(inCleaned.has(dup.id)
            ? `${icon('book', 'sm')}<span>Dans le référentiel</span>`
                + `<button type="button" class="note-action" data-action="remove-dup-clean">Retirer</button>`
            : `${icon('plus', 'sm')}<span>Pas un doublon ? Gardez aussi cette liste.</span>`
                + `<button type="button" class="note-action" data-action="add-dup-clean">Ajouter au référentiel</button>`);
    }
    if (notes.length) html += `<div class="notes">${notes.map(n => `<div>${n}</div>`).join('')}</div>`;

    if (compareOpen) {
        html += buildCodeComparisonHtml(cl.codes || [], dup.codes || [], cl.name || cl.id, dupName);
        if (dup.cat_ids?.length) {
            // Repliés : utiles pour une vérification ponctuelle, pas pour décider
            html += `<details class="notes cat-ids"><summary>Identifiants des catégories (${dup.cat_ids.length})</summary>`
                + `<div class="text-break mono">${dup.cat_ids.map(escapeHtml).join(', ')}</div></details>`;
        }
    }
    html += `</div>`;
    return html;
}

/* ------------------------------------------------------------------ */
/* Pagination
/* ------------------------------------------------------------------ */

function renderPagination(total, page, pageSize) {
    TOTAL_PAGES = Math.ceil(total / pageSize) || 1;
    const html = TOTAL_PAGES <= 1 ? '' :
        `<button type="button" data-page="${page - 1}" ${page <= 0 ? 'disabled' : ''} aria-label="Page précédente">${icon('chevron-left')}</button>`
        + `<span>${page + 1} / ${TOTAL_PAGES}</span>`
        + `<button type="button" data-page="${page + 1}" ${page >= TOTAL_PAGES - 1 ? 'disabled' : ''} aria-label="Page suivante">${icon('chevron')}</button>`;
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
    document.addEventListener('outputrootchange', loadRunOptions);
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
    for (const id of ['originFilter', 'sortBy']) {
        document.getElementById(id).addEventListener('change', applyNow);
    }
    document.getElementById('decisionChips').addEventListener('click', (e) => {
        const chip = e.target.closest('[data-filter]');
        if (!chip) return;
        document.getElementById('decisionFilter').value = chip.dataset.filter;
        applyNow();
    });
    let searchTimer = null;
    document.getElementById('searchFilter').addEventListener('input', () => {
        clearTimeout(searchTimer);
        searchTimer = setTimeout(applyNow, 300);
    });

    // Pagination (haut et bas)
    for (const id of ['pagination', 'paginationBottom']) {
        document.getElementById(id).addEventListener('click', (e) => {
            const link = e.target.closest('button[data-page]:not([disabled])');
            if (!link) return;
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
        const item = e.target.closest('.cl-item[data-id]');
        if (!item) return;
        selectCodelist(item.dataset.id);
    });
    nav.addEventListener('keydown', (e) => {
        const item = e.target.closest('.cl-item[data-id]');
        if (item && e.key === 'Enter') selectCodelist(item.dataset.id);
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
            case 'add-dup-clean': return addToCleaned(card.dataset.dupId);
            case 'remove-dup-clean': return removeFromCleaned([card.dataset.dupId], btn);
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
