/* precheck.js — Pré-contrôle d'un fichier DDI (onglet 4)

   Envoie le fichier à /api/precheck (moteur du service scrubber-check) et
   affiche, pour chaque CodeList du fichier, son statut, sa meilleure
   correspondance parmi les listes publiées et l'action conseillée. */

const PRECHECK_STATUS = {
    doublon: ['danger', 'doublon'],
    inclusion: ['warning text-dark', 'inclusion'],
    proche: ['info text-dark', 'proche'],
    nouvelle: ['secondary', 'nouvelle'],
    reutilisee: ['success', 'réutilisée'],
};
const PRECHECK_ORDER = ['doublon', 'inclusion', 'proche', 'nouvelle', 'reutilisee'];
const PRECHECK_DETECTIONS = {
    exact: 'exact',
    exact_business: 'exact hors échappement',
    labels: 'mêmes libellés',
    inclusion: 'sous-ensemble',
    extension: 'extension',
    lineage: 'lignée',
    fuzzy: 'proche',
};

function precheckUrl(format = 'json') {
    const params = new URLSearchParams({ format });
    const index = document.getElementById('precheckIndex').value.trim();
    if (index) params.set('index', index);
    for (const op of parseIds(document.getElementById('precheckExclude').value)) {
        params.append('exclude_operation', op);
    }
    return `${API_BASE}api/precheck?${params}`;
}

async function postPrecheck(format) {
    const file = document.getElementById('precheckFile').files[0];
    if (!file) throw new Error('Choisissez un fichier DDI.');
    const body = new FormData();
    body.append('file', file);
    const resp = await fetch(precheckUrl(format), { method: 'POST', body });
    if (!resp.ok) {
        const detail = (await resp.json().catch(() => ({}))).detail;
        throw new Error(detail || `HTTP ${resp.status}`);
    }
    return resp;
}

async function runPrecheck() {
    const btn = document.getElementById('precheckBtn');
    btn.disabled = true;
    btn.textContent = '⏳ Contrôle…';
    try {
        const report = await (await postPrecheck('json')).json();
        renderPrecheck(report);
    } catch (err) {
        showToast(`Pré-contrôle impossible : ${err.message}`, 'danger');
    } finally {
        btn.disabled = false;
        btn.textContent = '🔎 Contrôler';
    }
}

function diffHtml(diff) {
    const fmt = (codes) => codes.map(c => escapeHtml(`${c[0]} ${c[1] || ''}`.trim())).join(' · ');
    const parts = [];
    if (diff.codes_en_plus.length) parts.push(`<div><strong>En plus dans le fichier :</strong> ${fmt(diff.codes_en_plus)}</div>`);
    if (diff.codes_en_moins.length) parts.push(`<div><strong>Absents du fichier :</strong> ${fmt(diff.codes_en_moins)}</div>`);
    if (diff.libelles_differents.length) {
        parts.push(`<div><strong>Libellés différents :</strong> ${diff.libelles_differents
            .map(d => `${escapeHtml(d[0])} « ${escapeHtml(d[1])} » ≠ « ${escapeHtml(d[2])} »`).join(' · ')}</div>`);
    }
    return parts.join('') || '<div>Codes identiques.</div>';
}

function renderPrecheck(report) {
    document.getElementById('precheckResult').classList.remove('d-none');

    const by = report.summary.by_status;
    let summary = `<strong>${report.summary.codelists} CodeList(s)</strong>`;
    for (const status of PRECHECK_ORDER) {
        if (!by[status]) continue;
        const [cls, label] = PRECHECK_STATUS[status];
        summary += ` <span class="badge bg-${cls}">${by[status]} ${label}</span>`;
    }
    if (report.summary.internal_duplicates) {
        summary += ` <span class="badge bg-danger">${report.summary.internal_duplicates} doublon(s) interne(s)</span>`;
    }
    const idx = report.index || {};
    summary += `<span class="small text-muted ms-auto">Référence : ${escapeHtml(idx.scope || '?')}, `
        + `index du ${escapeHtml(String(idx.built_at || '?').slice(0, 10))}</span>`;
    document.getElementById('precheckSummary').innerHTML = summary;

    const rows = [...report.codelists].sort(
        (a, b) => PRECHECK_ORDER.indexOf(a.status) - PRECHECK_ORDER.indexOf(b.status)
            || (a.name || '').localeCompare(b.name || '', 'fr')
    );
    let html = '';
    rows.forEach((cl, i) => {
        const [cls, label] = PRECHECK_STATUS[cl.status];
        const best = cl.matches[0];
        const target = best || cl.reused;
        const flags = cl.quality_flags.length
            ? ` <span class="badge bg-warning text-dark" title="Liste suspecte : ${escapeHtml(cl.quality_flags.join(', '))}">⚠</span>` : '';
        const hasDetail = cl.matches.length > 0;
        html += `<tr class="${hasDetail ? 'precheck-row' : ''}" data-row="${i}"${hasDetail ? ' tabindex="0" title="Voir le détail"' : ''}>`;
        html += `<td><span class="badge bg-${cls}">${label}</span></td>`;
        html += `<td>${escapeHtml(cl.name || cl.id)}${flags}<br><code class="dup-id">${escapeHtml(cl.id)}</code></td>`;
        html += `<td>${target ? escapeHtml(target.name || target.id) : '—'}`;
        if (target?.series?.length) html += `<br><small class="text-muted">série(s) ${escapeHtml(target.series.join(', '))}</small>`;
        html += `</td><td>${target ? escapeHtml(LEVEL_LABELS[target.level] || target.level || '') : ''}</td>`;
        html += `<td>${best ? escapeHtml(PRECHECK_DETECTIONS[best.detection] || best.detection) : ''}</td>`;
        html += `<td class="text-end">${best ? Math.round(best.confidence * 100) + ' %' : ''}</td></tr>`;
        if (cl.status !== 'reutilisee' && cl.status !== 'nouvelle') {
            html += `<tr class="precheck-action"><td></td><td colspan="5" class="small">→ ${escapeHtml(cl.action)}</td></tr>`;
        }
        if (hasDetail) {
            html += `<tr class="precheck-detail d-none" data-detail="${i}"><td></td><td colspan="5" class="small">`;
            for (const m of cl.matches) {
                html += `<div class="mb-2"><strong>${escapeHtml(m.name || m.id)}</strong> `;
                html += `<code class="dup-id">${escapeHtml(m.urn || m.id)}</code> · `;
                html += `${escapeHtml(PRECHECK_DETECTIONS[m.detection] || m.detection)} ${Math.round(m.confidence * 100)} %`;
                if (m.operations?.length) html += ` · opérations ${escapeHtml(m.operations.join(', '))}`;
                html += diffHtml(m.diff) + `</div>`;
            }
            html += `</td></tr>`;
        }
    });
    document.getElementById('precheckRows').innerHTML = html;

    const internal = report.internal_duplicates || [];
    document.getElementById('precheckInternal').innerHTML = internal.length
        ? `<h6>Doublons à l'intérieur du fichier</h6><ul class="small">${internal.map(d =>
            `<li>${escapeHtml(d.a.name || d.a.id)} ↔ ${escapeHtml(d.b.name || d.b.id)} `
            + `(${escapeHtml(PRECHECK_DETECTIONS[d.detection] || d.detection)}, ${Math.round(d.confidence * 100)} %)</li>`
        ).join('')}</ul>`
        : '';
}

function togglePrecheckDetail(row) {
    document.querySelector(`#precheckRows [data-detail="${row.dataset.row}"]`)?.classList.toggle('d-none');
}

async function downloadPrecheckCsv() {
    try {
        const blob = await (await postPrecheck('csv')).blob();
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = 'precontrole.csv';
        a.click();
        URL.revokeObjectURL(url);
    } catch (err) {
        showToast(`Export impossible : ${err.message}`, 'danger');
    }
}

document.addEventListener('DOMContentLoaded', () => {
    document.getElementById('precheckBtn').addEventListener('click', runPrecheck);
    document.getElementById('precheckCsvBtn').addEventListener('click', downloadPrecheckCsv);
    const rows = document.getElementById('precheckRows');
    rows.addEventListener('click', (e) => {
        const row = e.target.closest('.precheck-row');
        if (row) togglePrecheckDetail(row);
    });
    rows.addEventListener('keydown', (e) => {
        const row = e.target.closest('.precheck-row');
        if (row && (e.key === 'Enter' || e.key === ' ')) {
            e.preventDefault();
            togglePrecheckDetail(row);
        }
    });
    document.getElementById('precheckIndex').value = loadPref('precheckIndex', '');
    document.getElementById('precheckIndex').addEventListener('change', (e) => savePref('precheckIndex', e.target.value.trim()));
});
