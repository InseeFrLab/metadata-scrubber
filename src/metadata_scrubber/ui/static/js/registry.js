/* registry.js — Gestion de la validation des doublons */

// API_BASE is defined in config.js (loaded first)

let registryData = null;
let currentPage = 0;
let TOTAL_PAGES = 0;

/* ------------------------------------------------------------------ */
/* Loading / saving
/* ------------------------------------------------------------------ */

async function loadRegistry() {
    const path = document.getElementById('registryPath').value.trim();
    if (!path) {
        showToast('Veuillez saisir le chemin du registre.', 'warning');
        return;
    }

    const btn = document.getElementById('loadRegistryBtn');
    btn.disabled = true;
    btn.textContent = '⏳ Chargement…';

    try {
        const resp = await fetch(
            `${API_BASE}api/registry?path=${encodeURIComponent(path)}`
        );
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);

        registryData = await resp.json();
        document.getElementById('emptyState').classList.add('d-none');
        document.getElementById('statsSection').classList.remove('d-none');

        updateStats();
        currentPage = 0;
        renderCodelistsFiltered();

        showToast(
            `✅ Registre chargé · ${Object.keys(registryData).length} CodeLists`,
            'success'
        );
    } catch (err) {
        console.error('Chargement registre:', err);
        showToast(`Erreur: ${err.message}`, 'danger');
    } finally {
        btn.disabled = false;
        btn.textContent = '📂 Charger le registre';
    }
}

async function saveRegistry() {
    if (!registryData) {
        showToast('Aucun registre chargé.', 'warning');
        return;
    }
    const path = document.getElementById('registryPath').value.trim();
    if (!path) {
        showToast('Aucun chemin de destination.', 'warning');
        return;
    }

    const btn = document.getElementById('saveBtn');
    btn.disabled = true;
    btn.textContent = '⏳ Sauvegarde…';

    try {
        const resp = await fetch(
            `${API_BASE}api/registry/save?path=${encodeURIComponent(path)}`,
            { method: 'POST' }
        );
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        const data = await resp.json();
        showToast(
            `💾 Registre sauvegardé · registre nettoyé synchronisé (${data.cleaned_count ?? 0} entrées)`,
            'success'
        );
        // Préremplir l'onglet Registre nettoyé
        if (data.cleaned_path) {
            document.getElementById('cleanedPath').value = data.cleaned_path;
        }
    } catch (err) {
        showToast(`Erreur: ${err.message}`, 'danger');
    } finally {
        btn.disabled = false;
        btn.textContent = '💾 Sauvegarder';
    }
}

async function downloadRegistry() {
    if (!registryData) {
        showToast('Aucun registre chargé.', 'warning');
        return;
    }
    const blob = new Blob(
        [JSON.stringify(registryData, null, 2)],
        { type: 'application/json' }
    );
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = 'codelist_duplicates.json';
    a.click();
    URL.revokeObjectURL(url);
    showToast('⬇️ Téléchargement lancé', 'info');
}

async function downloadCleanedRegistry() {
    if (!registryData) {
        showToast('Aucun registre chargé.', 'warning');
        return;
    }
    try {
        const resp = await fetch(`${API_BASE}api/registry/cleaned`);
        if (resp.status === 404) {
            showToast('Sauvegardez d\'abord le registre pour générer le registre nettoyé.', 'warning');
            return;
        }
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        const cleaned = await resp.json();
        const blob = new Blob(
            [JSON.stringify(cleaned, null, 2)],
            { type: 'application/json' }
        );
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = 'cleaned_codelists.json';
        a.click();
        URL.revokeObjectURL(url);
        const count = Object.keys(cleaned.codelists || {}).length;
        showToast(`⬇️ Registre nettoyé · ${count} CodeLists`, 'info');
    } catch (err) {
        showToast(`Erreur: ${err.message}`, 'danger');
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
    document.getElementById('statPending').textContent = stats.pending;
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

/* ------------------------------------------------------------------ */
/* Filtering & rendering
/* ------------------------------------------------------------------ */

function getFilters() {
    return {
        decisionFilter: document.getElementById('decisionFilter').value.split(','),
        originFilter: document.getElementById('originFilter').value,
        search: document.getElementById('searchFilter').value.trim(),
        pageSize: parseInt(document.getElementById('pageSize').value, 10),
    };
}

async function renderCodelistsFiltered() {
    if (!registryData) return;
    const { decisionFilter, originFilter, search, pageSize } = getFilters();

    let items = [];
    for (const [clId, clData] of Object.entries(registryData)) {
        let cl = {
            id: clId,
            name: clData.name || clId,
            label: clData.label || '',
            codes_count: clData.codes_count || 0,
            codes: clData.codes || [],
            origin: clData.origin || 'xml',
            vars_count: (clData.vars || []).length,
            vars: clData.vars || [],
            cat_ids_count: (clData.cat_ids || []).length,
            duplicates: clData.duplicates || [],
            decisions: (clData.duplicates || []).map(d => d.decision || 'pending'),
        };

        // Decision filter ("none" = listes sans doublon détecté)
        if (decisionFilter.length === 1 && decisionFilter[0] === 'none') {
            if (cl.duplicates.length) continue;
        } else if (decisionFilter.length < 3) {
            if (!cl.decisions.some(d => decisionFilter.includes(d))) continue;
        }

        // Origin filter (listes issues du registre nettoyé)
        if (originFilter === 'registry' && cl.origin !== 'registry') continue;

        // Search filter
        if (search) {
            const searchLower = search.toLowerCase();
            const nameLower = (cl.name || '').toLowerCase();
            const dupNames = (cl.duplicates || [])
                .map(d => d.name || '')
                .join(' ').toLowerCase();
            if (!nameLower.includes(searchLower) &&
                !dupNames.includes(searchLower)) {
                continue;
            }
        }

        items.push(cl);
    }

    // Sort by duplicates count desc
    items.sort((a, b) => b.duplicates.length - a.duplicates.length);

    // Paginate
    const start = currentPage * pageSize;
    const paged = items.slice(start, start + pageSize);
    TOTAL_PAGES = Math.ceil(items.length / pageSize) || 1;

    renderCodelistList(paged);
    renderPagination(items.length, currentPage, pageSize);
}

function renderCodelistList(items) {
    const container = document.getElementById('codelistContainer');

    if (!items.length) {
        container.innerHTML = '<div class="text-center py-4 text-muted">Aucune CodeList trouvée.</div>';
        return;
    }

    let html = '';
    for (const cl of items) {
        html += renderCodeListCard(cl);
    }
    container.innerHTML = html;

    // Set up decision select listeners
    for (const cl of items) {
        const addBtn = document.getElementById(`addclean_${cl.id}`);
        if (addBtn) {
            addBtn.addEventListener('click', () => addToCleaned(cl.id));
        }
        for (const dup of cl.duplicates || []) {
            const select = document.getElementById(`decision_${cl.id}_${dup.id}`);
            if (select) {
                select.addEventListener('change', () => {
                    updateDecision(cl.id, dup.id, select.value);
                });
            }

            // Rendu paresseux du tableau comparatif à la première ouverture
            const collapseEl = document.getElementById(`dup_${cl.id}_${dup.id}`);
            if (collapseEl) {
                collapseEl.addEventListener('show.bs.collapse', () => {
                    const target = document.getElementById(`codes_${cl.id}_${dup.id}`);
                    if (target && !target.dataset.rendered) {
                        target.innerHTML = buildCodeComparisonHtml(
                            cl.codes, dup.codes || [], cl.name, dup.name
                        );
                        target.dataset.rendered = '1';
                    }
                });
            }
        }
    }
}

function renderCodeListCard(cl) {
    const dupCount = cl.duplicates?.length || 0;
    const decApproved = (cl.decisions || []).filter(d => d === 'approve').length;
    const decRejected = (cl.decisions || []).filter(d => d === 'reject').length;
    const decPending = dupCount - decApproved - decRejected;

    let html = `<div class="card mb-2">`;
    html += `<div class="card-header d-flex justify-content-between align-items-center">`;
    html += `<div><h6 class="mb-0">${escapeHtml(cl.name || cl.id)} <button class="btn btn-sm btn-link p-0 copy-id-btn" onclick="copyId('${escapeHtml(cl.id)}')" title="Copier l'ID">${escapeHtml(cl.id)}</button></h6>`;
    if (cl.label) html += `<small class="text-muted">${cl.label}</small>`;
    html += `</div>`;
    html += `<div class="d-flex gap-1">`;
    if (cl.origin === 'registry') {
        html += `<span class="badge bg-primary">📘 registre</span>`;
    }
    html += `<span class="badge bg-success">${decApproved}</span>`;
    html += `<span class="badge bg-danger">${decRejected}</span>`;
    html += `<span class="badge bg-warning text-dark">${decPending}</span>`;
    html += `<span class="badge bg-secondary">${dupCount}</span>`;
    html += `</div></div>`;

    html += `<div class="card-body py-1 small">`;
    html += `${cl.codes_count || 0} codes · ${cl.vars_count || 0} variables`;
    html += ` · ${cl.cat_ids_count || 0} catégories</div>`;

    if (cl.duplicates?.length) {
        html += `<div class="card-body pt-0">`;
        for (const dup of cl.duplicates) {
            html += renderDuplicateCard(cl, dup);
        }
        html += `</div>`;
    } else {
        // Liste sans doublon détecté → ajout manuel au registre nettoyé
        html += `<div class="card-body pt-0 d-flex align-items-center gap-2">`;
        html += `<span class="text-muted small">Aucun doublon détecté.</span>`;
        html += `<button class="btn btn-sm btn-outline-success" id="addclean_${cl.id}">`;
        html += `➕ Ajouter au registre nettoyé</button></div>`;
    }

    html += `</div>`;
    return html;
}

async function addToCleaned(clId) {
    const btn = document.getElementById(`addclean_${clId}`);
    try {
        const resp = await fetch(`${API_BASE}api/registry/add-to-cleaned`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ cl_id: clId }),
        });
        if (resp.status === 409) {
            showToast('Déjà présente dans le registre nettoyé.', 'warning');
            if (btn) btn.disabled = true;
            return;
        }
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        const data = await resp.json();
        showToast(`➕ Ajoutée au registre nettoyé · ${data.count} entrées`, 'success');
        if (btn) {
            btn.disabled = true;
            btn.textContent = '✅ Dans le registre';
        }
        if (data.path) document.getElementById('cleanedPath').value = data.path;
    } catch (err) {
        showToast(`Erreur: ${err.message}`, 'danger');
    }
}

function escapeHtml(s) {
    return String(s)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');
}

function copyId(id) {
    navigator.clipboard.writeText(id).then(() => {
        showToast(`✅ ID "${id}" copié`, 'info');
    }).catch(() => {
        const ta = document.createElement('textarea');
        ta.value = id;
        document.body.appendChild(ta);
        ta.select();
        document.execCommand('copy');
        document.body.removeChild(ta);
        showToast(`✅ ID "${id}" copié`, 'info');
    });
}

/* Tableau comparatif de codes : Valeur P | Étiquette P | Valeur D | Étiquette D.
   3 sections : communs (matching case-insensitive), uniquement parent,
   uniquement doublon. Lignes surlignées quand les étiquettes divergent. */
function buildCodeComparisonHtml(srcCodes, dstCodes, srcName, dupName) {
    // Maps valeur → étiquette et clef lowercase → valeur d'origine
    const srcMap = {}, srcMapLower = {};
    for (const pair of srcCodes || []) {
        const raw = String(pair[0]);
        srcMap[raw] = pair.length > 1 ? String(pair[1]) : '';
        srcMapLower[raw.toLowerCase()] = raw;
    }
    const dstMap = {}, dstMapLower = {};
    for (const pair of dstCodes || []) {
        const raw = String(pair[0]);
        dstMap[raw] = pair.length > 1 ? String(pair[1]) : '';
        dstMapLower[raw.toLowerCase()] = raw;
    }

    const srcKeys = Object.keys(srcMapLower);
    const dstKeys = Object.keys(dstMapLower);
    const common = srcKeys.filter(k => k in dstMapLower).sort();
    const onlySrc = srcKeys.filter(k => !(k in dstMapLower)).sort();
    const onlyDst = dstKeys.filter(k => !(k in srcMapLower)).sort();

    const truncate = (s) => escapeHtml((s || '').slice(0, 60));
    let rows = '';
    for (const k of common) {
        const kSrc = srcMapLower[k], kDst = dstMapLower[k];
        const same = kSrc === kDst && srcMap[kSrc] === dstMap[kDst];
        rows += `<tr class="${same ? '' : 'table-warning'}">`;
        rows += `<td>${escapeHtml(kSrc)}</td><td>${truncate(srcMap[kSrc])}</td>`;
        rows += `<td>${escapeHtml(kDst)}</td><td>${truncate(dstMap[kDst])}</td></tr>`;
    }
    for (const k of onlySrc) {
        const kSrc = srcMapLower[k];
        rows += `<tr class="table-info">`;
        rows += `<td>${escapeHtml(kSrc)}</td><td>${truncate(srcMap[kSrc])}</td>`;
        rows += `<td></td><td></td></tr>`;
    }
    for (const k of onlyDst) {
        const kDst = dstMapLower[k];
        rows += `<tr class="table-danger">`;
        rows += `<td></td><td></td>`;
        rows += `<td>${escapeHtml(kDst)}</td><td>${truncate(dstMap[kDst])}</td></tr>`;
    }

    let html = `<div class="code-compare-wrap">`;
    html += `<table class="table table-sm table-bordered code-table mb-1">`;
    html += `<thead><tr><th>Valeur P</th><th>Étiquette P</th>`;
    html += `<th>Valeur D</th><th>Étiquette D</th></tr></thead>`;
    html += `<tbody>${rows}</tbody></table></div>`;

    const summaryParts = [`${common.length} codes en commun`];
    if (onlySrc.length) summaryParts.push(`${onlySrc.length} uniquement ${escapeHtml(srcName || 'parent')}`);
    if (onlyDst.length) summaryParts.push(`${onlyDst.length} uniquement ${escapeHtml(dupName || 'doublon')}`);
    html += `<small class="text-muted">${summaryParts.join(' · ')}</small>`;

    return html;
}

function renderDuplicateCard(cl, dup) {
    const conf = dup.confidence || 0;
    const confClass = conf >= 0.95 ? 'confidence-high' : conf >= 0.7 ? 'confidence-medium' : 'confidence-low';

    let html = `<div class="dup-card p-2 mb-1" style="border-left: 3px solid #ccc;">`;
    html += `<div class="d-flex justify-content-between align-items-start">`;
    html += `<div style="flex: 1;"><strong>${dup.name || 'Inconnu'}</strong>`;
    html += ` <code class="dup-id">${escapeHtml(dup.id)}</code> <button class="btn btn-link p-0 copy-id-btn" onclick="copyId('${escapeHtml(dup.id)}')" title="Copier l'ID">📋</button>`;
    if (dup.origin === 'registry') html += ` <span class="badge bg-primary">📘 registre</span>`;
    if (dup.label) html += `<br><small class="text-muted">${dup.label}</small>`;

    // Detection types
    if (dup.detection_types?.length) {
        html += `<div class="mt-1">`;
        for (const type of dup.detection_types) {
            const badgeClass = {
                exact: 'success',
                fuzzy: 'warning text-dark',
                semantic_list: 'info text-dark',
                usage: 'secondary',
            }[type] || 'secondary';
            html += `<span class="badge badge-detection bg-${badgeClass} me-1">${type}</span>`;
        }
        html += `</div>`;
    }

    // Confidence bar
    html += `<div class="mt-1"><div class="confidence-bar">`;
    html += `<div class="confidence-bar-inner ${confClass}" `;
    html += `style="width: ${Math.round(conf * 100)}%;"></div>`;
    html += `</div><small class="text-muted">${(conf * 100).toFixed(1)}%</small></div></div>`;

    // Decision select
    const valueAttr = [
        `value="pending"`, `value="approve"`, `value="reject"`,
    ].map((v, idx) => {
        const decisionIdx = ['approve', 'reject', 'pending'].indexOf(dup.decision);
        return `${v} ${(idx === decisionIdx) ? 'selected' : ''}`;
    }).join(' ');

    html += `<div><select class="form-select form-select-sm decision-select" `;
    html += `id="decision_${cl.id}_${dup.id}" style="min-width: 140px;">`;
    html += `<option value="pending" ${['approve','reject','pending'].indexOf(dup.decision)===2 ? 'selected' : ''}>⏳ En attente</option>`;
    html += `<option value="approve" ${['approve','reject','pending'].indexOf(dup.decision)===0 ? 'selected' : ''}>✅ Approuver</option>`;
    html += `<option value="reject" ${['approve','reject','pending'].indexOf(dup.decision)===1 ? 'selected' : ''}>❌ Rejeter</option>`;
    html += `</select></div></div>`;

    // Details collapsible
    html += `<div class="mt-2">`;
    html += `<button class="btn btn-sm btn-outline-secondary" type="button" `;
    html += `data-bs-toggle="collapse" data-bs-target="#dup_${cl.id}_${dup.id}">`;
    html += `Détails ▼</button></div>`;
    html += `<div id="dup_${cl.id}_${dup.id}" class="collapse mt-2">`;
    html += `<div class="small text-muted">`;
    html += `<p class="mb-1"><strong>ID:</strong> ${dup.id}</p>`;
    html += `<p class="mb-1"><strong>Codes:</strong> ${dup.codes_count || 0}</p>`;
    if (dup.vars?.length) {
        html += `<p class="mb-1"><strong>VARIABLES:</strong> ${dup.vars.join(', ')}</p>`;
    }
    if (dup.cat_ids?.length) {
        html += `<p class="mb-1"><strong>Catég:</strong> ${dup.cat_ids.join(', ')}</p>`;
    }
    html += `</div>`;
    html += `<div class="code-comparison mt-2" id="codes_${cl.id}_${dup.id}"></div>`;
    html += `</div></div>`;

    return html;
}

/* ------------------------------------------------------------------ */
/* Pagination
/* ------------------------------------------------------------------ */

function renderPagination(total, page, pageSize) {
    TOTAL_PAGES = Math.ceil(total / pageSize) || 1;
    const pagination = document.getElementById('pagination');
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

    pagination.innerHTML = html;

    // Add listeners
    pagination.querySelectorAll('.page-item:not(.disabled) .page-link').forEach(link => {
        link.addEventListener('click', (e) => {
            e.preventDefault();
            const newPage = parseInt(link.dataset.page, 10);
            if (newPage !== currentPage) {
                currentPage = newPage;
                renderCodelistsFiltered();
            }
        });
    });
}

/* ------------------------------------------------------------------ */
/* Decision updates
/* ------------------------------------------------------------------ */

async function updateDecision(clId, dupId, decision) {
    try {
        await fetch(`${API_BASE}api/registry/decision`, {
            method: 'PATCH',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ cl_id: clId, dup_id: dupId, decision }),
        });

        // Update local data
        if (registryData && registryData[clId]) {
            const cl = registryData[clId];
            for (const dup of cl.duplicates || []) {
                if (dup.id === dupId) {
                    dup.decision = decision;
                    break;
                }
            }
        }

        updateStats();
        showToast(`✅ Décision mise à jour`, 'success');
    } catch (err) {
        console.error('Update decision:', err);
        showToast(`Erreur mise à jour: ${err.message}`, 'danger');
    }
}

async function bulkDecision(action, criteria) {
    const labels = {
        approve: '✅ Approuver',
        reject: '❌ Rejeter',
        pending: '🔄 Remettre en attente',
    };
    const criteriaLabels = {
        exact: 'exactes',
        'high-confidence': 'confiance ≥ 0.95',
        all: 'tous',
    };

    if (!confirm(`${labels[action]} ${criteriaLabels[criteria]} ?`)) return;

    const btn = event.target;
    const originalText = btn.textContent;
    btn.disabled = true;
    btn.textContent = '⏳ Traitement…';

    try {
        const resp = await fetch(`${API_BASE}api/registry/bulk`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ action, criteria }),
        });
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);

        const data = await resp.json();
        showToast(
            `${labels[action]} ${criteriaLabels[criteria]} : ${data.count} mis à jour`,
            'success'
        );

        // Update local data
        if (registryData) {
            for (const cl of Object.values(registryData)) {
                for (const dup of cl.duplicates || []) {
                    let should = false;
                    if (criteria === 'all') should = true;
                    else if (criteria === 'exact') {
                        should = (dup.detection_types || []).includes('exact');
                    } else if (criteria === 'high-confidence') {
                        should = (dup.confidence || 0) >= 0.95;
                    }
                    if (should) dup.decision = action;
                }
            }
        }

        updateStats();
        renderCodelistsFiltered();
    } catch (err) {
        showToast(`Erreur: ${err.message}`, 'danger');
    } finally {
        btn.disabled = false;
        btn.textContent = originalText;
    }
}

/* ------------------------------------------------------------------ */
/* DOM Ready
/* ------------------------------------------------------------------ */

document.addEventListener('DOMContentLoaded', () => {
    document.getElementById('loadRegistryBtn')
        .addEventListener('click', loadRegistry);
    document.getElementById('saveBtn')
        .addEventListener('click', saveRegistry);
    document.getElementById('downloadBtn')
        .addEventListener('click', downloadRegistry);
    document.getElementById('downloadCleanedBtn')
        .addEventListener('click', downloadCleanedRegistry);
    document.getElementById('applyFilters')
        .addEventListener('click', () => {
            currentPage = 0;
            renderCodelistsFiltered();
        });
    document.getElementById('pageSize')
        .addEventListener('change', () => {
            currentPage = 0;
            renderCodelistsFiltered();
        });

    // Bulk actions
    document.getElementById('bulkApproveExact')
        .addEventListener('click', () => bulkDecision('approve', 'exact'));
    document.getElementById('bulkApproveHighConf')
        .addEventListener('click', () => bulkDecision('approve', 'high-confidence'));
    document.getElementById('bulkRejectAll')
        .addEventListener('click', () => bulkDecision('reject', 'all'));
    document.getElementById('bulkPendingAll')
        .addEventListener('click', () => bulkDecision('pending', 'all'));

    // Auto-load if registry path was set by API
    const defaultPath = document.getElementById('registryPath').value.trim();
    if (defaultPath && defaultPath.endsWith('.json')) {
        // Don't auto-load on startup, wait for user to click
    }
});
