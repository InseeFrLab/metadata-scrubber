/* precheck.js — Pré-contrôle d'un fichier DDI (onglet 4)

   Envoie le fichier à /api/precheck (moteur du service scrubber-check) et
   affiche, pour chaque CodeList du fichier, son statut, sa meilleure
   correspondance parmi les listes publiées et l'action conseillée. */

const PRECHECK_STATUS = {
    doublon: ['doublon', 'doublons', 'Réutiliser la liste publiée'],
    inclusion: ['inclusion', 'inclusions', 'Réutiliser la liste, ou vérifier la restriction ou les codes ajoutés'],
    proche: ['proche', 'proches', 'Vérifier les différences avant de créer une nouvelle liste'],
    nouvelle: ['nouvelle', 'nouvelles', 'Aucune liste équivalente publiée'],
    reutilisee: ['réutilisée', 'réutilisées', 'La liste publiée elle-même : rien à faire'],
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
    setBusy(btn, true, 'Contrôle…');
    try {
        const report = await (await postPrecheck('json')).json();
        renderPrecheck(report);
        document.getElementById('precheckCsvBtn').disabled = false;
    } catch (err) {
        showToast(`Pré-contrôle impossible : ${err.message}`, 'danger');
    } finally {
        setBusy(btn, false);
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

const statusHtml = (status) =>
    `<span class="status s-${status}"><span class="dot"></span>${escapeHtml(PRECHECK_STATUS[status][0])}</span>`;

function renderPrecheck(report) {
    document.getElementById('precheckResult').classList.remove('d-none');

    // Répartition des statuts
    const by = report.summary.by_status;
    const total = report.summary.codelists;
    const present = PRECHECK_ORDER.filter(s => by[s]);
    const idx = report.index || {};
    let summary = `<div class="dist-head"><span><strong>${total} CodeList(s) contrôlée(s)</strong>`;
    summary += ` · référence : ${escapeHtml(idx.scope || '?')}, index du ${escapeHtml(String(idx.built_at || '?').slice(0, 10))}</span>`;
    if (report.summary.internal_duplicates) {
        summary += `<span class="status s-interne"><span class="dot"></span>${report.summary.internal_duplicates} doublon(s) dans le fichier</span>`;
    }
    summary += `</div>`;
    summary += `<div class="dist-bar" role="img" aria-label="${present.map(s => `${by[s]} ${PRECHECK_STATUS[s][0]}`).join(', ')}">`;
    summary += present.map(s => `<span class="s-${s}" style="flex: ${by[s]}" title="${by[s]} ${PRECHECK_STATUS[s][0]}">${by[s]}</span>`).join('');
    summary += `</div><div class="dist-legend">`;
    summary += present.map(s => `<div class="s-${s}"><span class="dot" style="color: var(--c)"></span>`
        + `<b>${by[s]}</b><span>${by[s] > 1 ? PRECHECK_STATUS[s][1] : PRECHECK_STATUS[s][0]}</span></div>`).join('');
    summary += `</div>`;
    document.getElementById('precheckSummary').innerHTML = summary;

    // Tableau groupé par statut, doublons d'abord
    const rows = [...report.codelists].sort(
        (a, b) => PRECHECK_ORDER.indexOf(a.status) - PRECHECK_ORDER.indexOf(b.status)
            || (a.name || '').localeCompare(b.name || '', 'fr')
    );
    let html = '';
    let group = null;
    rows.forEach((cl, i) => {
        if (cl.status !== group) {
            group = cl.status;
            html += `<tr class="group-row"><td colspan="6">${statusHtml(group)} · ${escapeHtml(PRECHECK_STATUS[group][2])}</td></tr>`;
        }
        const best = cl.matches[0];
        const target = best || cl.reused;
        const flags = cl.quality_flags.length
            ? ` <span class="flag" title="Liste suspecte : ${escapeHtml(cl.quality_flags.join(', '))}">${icon('alert')}</span>` : '';
        const hasDetail = cl.matches.length > 0;
        html += `<tr class="hoverable${hasDetail ? ' precheck-row' : ''}" data-row="${i}"`;
        html += hasDetail ? ` tabindex="0" aria-expanded="false" title="Voir le détail des codes">` : '>';
        html += `<td>${hasDetail ? `<span class="chev">${icon('chevron', 'sm')}</span> ` : ''}${statusHtml(cl.status)}</td>`;
        html += `<td>${escapeHtml(cl.name || cl.id)}${flags}<div class="dup-id">${escapeHtml(cl.id)}</div></td>`;
        html += `<td>${target ? escapeHtml(target.name || target.id) : '<span class="text-faint">aucune correspondance</span>'}`;
        if (target?.series?.length) html += `<div class="small text-muted">série(s) ${escapeHtml(target.series.join(', '))}</div>`;
        if (cl.action && !['reutilisee', 'nouvelle'].includes(cl.status)) {
            html += `<div class="precheck-action">${escapeHtml(cl.action)}</div>`;
        }
        html += `</td><td>${target ? levelTagHtml(target.level) : ''}</td>`;
        html += `<td>${best ? `<span class="tag">${escapeHtml(PRECHECK_DETECTIONS[best.detection] || best.detection)}</span>` : ''}</td>`;
        html += `<td class="r">${best ? Math.round(best.confidence * 100) + ' %' : '—'}</td></tr>`;
        if (hasDetail) {
            html += `<tr class="precheck-detail d-none" data-detail="${i}"><td></td><td colspan="5">`;
            for (const m of cl.matches) {
                html += `<div class="match"><div><strong>${escapeHtml(m.name || m.id)}</strong> `;
                html += `<span class="dup-id">${escapeHtml(m.urn || m.id)}</span></div>`;
                html += `<div class="text-muted small">${escapeHtml(PRECHECK_DETECTIONS[m.detection] || m.detection)} · ${Math.round(m.confidence * 100)} %`;
                if (m.operations?.length) html += ` · opérations ${escapeHtml(m.operations.join(', '))}`;
                html += `</div>${diffHtml(m.diff)}</div>`;
            }
            html += `</td></tr>`;
        }
    });
    document.getElementById('precheckRows').innerHTML = html;

    const internal = report.internal_duplicates || [];
    document.getElementById('precheckInternal').innerHTML = internal.length
        ? `<div class="panel internal-list"><strong>Doublons à l'intérieur du fichier</strong><ul>${internal.map(d =>
            `<li>${escapeHtml(d.a.name || d.a.id)} ↔ ${escapeHtml(d.b.name || d.b.id)} `
            + `(${escapeHtml(PRECHECK_DETECTIONS[d.detection] || d.detection)}, ${Math.round(d.confidence * 100)} %)</li>`
        ).join('')}</ul></div>`
        : '';
}

function togglePrecheckDetail(row) {
    const detail = document.querySelector(`#precheckRows [data-detail="${row.dataset.row}"]`);
    if (!detail) return;
    const open = detail.classList.toggle('d-none') === false;
    row.setAttribute('aria-expanded', open);
}

/* Fichier choisi (sélecteur ou glisser-déposer) */
function showPrecheckFile() {
    const file = document.getElementById('precheckFile').files[0];
    const pill = document.getElementById('precheckFileName');
    pill.classList.toggle('d-none', !file);
    pill.querySelector('.mono').textContent = file ? file.name : '';
    document.getElementById('precheckCsvBtn').disabled = true;
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
    const input = document.getElementById('precheckFile');
    input.addEventListener('change', showPrecheckFile);
    const drop = document.getElementById('precheckDrop');
    drop.addEventListener('dragover', (e) => {
        e.preventDefault();
        drop.classList.add('dragover');
    });
    drop.addEventListener('dragleave', () => drop.classList.remove('dragover'));
    drop.addEventListener('drop', (e) => {
        e.preventDefault();
        drop.classList.remove('dragover');
        if (!e.dataTransfer.files.length) return;
        const dt = new DataTransfer();
        dt.items.add(e.dataTransfer.files[0]);
        input.files = dt.files;
        showPrecheckFile();
    });
    document.getElementById('precheckIndex').value = loadPref('precheckIndex', '');
    document.getElementById('precheckIndex').addEventListener('change', (e) => savePref('precheckIndex', e.target.value.trim()));
});
