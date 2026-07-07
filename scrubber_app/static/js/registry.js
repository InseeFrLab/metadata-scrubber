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
        showToast(`💾 Registre sauvegardé`, 'success');
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
        search: document.getElementById('searchFilter').value.trim(),
        pageSize: parseInt(document.getElementById('pageSize').value, 10),
    };
}

async function renderCodelistsFiltered() {
    if (!registryData) return;
    const { decisionFilter, search, pageSize } = getFilters();

    let items = [];
    for (const [clId, clData] of Object.entries(registryData)) {
        let cl = {
            id: clId,
            name: clData.name || clId,
            label: clData.label || '',
            codes_count: clData.codes_count || 0,
            vars_count: (clData.vars || []).length,
            vars: clData.vars || [],
            cat_ids_count: (clData.cat_ids || []).length,
            duplicates: clData.duplicates || [],
            decisions: (clData.duplicates || []).map(d => d.decision || 'pending'),
        };

        // Decision filter
        if (decisionFilter.length < 3) {
            if (!cl.decisions.some(d => decisionFilter.includes(d))) continue;
        }

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
        for (const dup of cl.duplicates || []) {
            const select = document.getElementById(`decision_${cl.id}_${dup.id}`);
            if (select) {
                select.addEventListener('change', () => {
                    updateDecision(cl.id, dup.id, select.value);
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
    html += `<div><h6 class="mb-0">${cl.name || cl.id}</h6>`;
    if (cl.label) html += `<small class="text-muted">${cl.label}</small>`;
    html += `</div>`;
    html += `<div class="d-flex gap-1">`;
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
    }

    html += `</div>`;
    return html;
}

function renderDuplicateCard(cl, dup) {
    const conf = dup.confidence || 0;
    const confClass = conf >= 0.95 ? 'confidence-high' : conf >= 0.7 ? 'confidence-medium' : 'confidence-low';

    let html = `<div class="dup-card p-2 mb-1" style="border-left: 3px solid #ccc;">`;
    html += `<div class="d-flex justify-content-between align-items-start">`;
    html += `<div style="flex: 1;"><strong>${dup.name || 'Inconnu'}</strong>`;
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
    html += `</div></div></div>`;

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
