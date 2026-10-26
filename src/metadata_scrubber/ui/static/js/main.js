/* main.js — Navigation principale et utilitaires globaux */

/* ------------------------------------------------------------------ */
/* Tab navigation helper
/* ------------------------------------------------------------------ */

function switchTab(tabId) {
    const tabEl = document.querySelector(`[data-bs-target="#${tabId}"]`);
    if (tabEl) {
        const bs = new bootstrap.Tab(tabEl);
        bs.show();
    }
}

/* ------------------------------------------------------------------ */
/* Échappement et copie d'identifiants (partagés par tous les onglets)
/* ------------------------------------------------------------------ */

function escapeHtml(s) {
    return String(s ?? '')
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');
}

function copyId(id) {
    navigator.clipboard.writeText(id).then(() => {
        showToast(`ID « ${id} » copié`, 'info');
    }).catch(() => {
        const ta = document.createElement('textarea');
        ta.value = id;
        document.body.appendChild(ta);
        ta.select();
        document.execCommand('copy');
        document.body.removeChild(ta);
        showToast(`ID « ${id} » copié`, 'info');
    });
}

/* Tout élément portant data-copy-id copie cet identifiant au clic */
document.addEventListener('click', (e) => {
    const el = e.target.closest('[data-copy-id]');
    if (!el) return;
    e.preventDefault();
    e.stopPropagation();
    copyId(el.dataset.copyId);
});

/* ------------------------------------------------------------------ */
/* Préférences locales (localStorage peut être indisponible)
/* ------------------------------------------------------------------ */

function loadPref(key, fallback) {
    try {
        const v = localStorage.getItem(`scrubber.${key}`);
        return v === null ? fallback : v;
    } catch {
        return fallback;
    }
}

function savePref(key, value) {
    try {
        localStorage.setItem(`scrubber.${key}`, value);
    } catch {
        /* navigation privée : préférence non conservée */
    }
}

const DEFAULT_OUTPUT_ROOT = 's3://projet-metadonnees-rmes/scrubber_output';

/* Racine des sorties : un seul réglage, partagé par les onglets */
function getOutputRoot() {
    return loadPref('outputRoot', DEFAULT_OUTPUT_ROOT).replace(/\/+$/, '');
}

function setOutputRoot(root) {
    root = (root || '').trim().replace(/\/+$/, '');
    if (!root) return;
    savePref('outputRoot', root);
    for (const id of ['outputBase', 'outputRoot']) {
        const el = document.getElementById(id);
        if (el && el.value.trim().replace(/\/+$/, '') !== root) el.value = root;
    }
    const cleaned = document.getElementById('cleanedPath');
    if (cleaned && !cleaned.dataset.edited) cleaned.value = `${root}/cleaned_codelists.json`;
    const pipelineRegistry = document.getElementById('pipelineRegistryPath');
    if (pipelineRegistry && !pipelineRegistry.dataset.edited) {
        pipelineRegistry.value = `${root}/cleaned_codelists.json`;
    }
}

/* Runs d'une racine de sortie, du plus récent au plus ancien */
async function fetchRuns(root) {
    const resp = await fetch(`${API_BASE}api/runs?root=${encodeURIComponent(root)}`);
    if (!resp.ok) {
        const detail = (await resp.json().catch(() => ({}))).detail;
        throw new Error(detail || `HTTP ${resp.status}`);
    }
    return (await resp.json()).runs;
}

/* Niveaux de CodeLists (levels.py) */
const LEVEL_LABELS = { officielle: 'officielle', commune: 'commune', specifique: 'spécifique' };

/* « s1998, s2219 » → ["s1998", "s2219"] */
function parseIds(text) {
    return (text || '').split(/[\s,;]+/).map(s => s.trim()).filter(Boolean);
}

/* « 20261022-140509 » → « 22/10/2026 14:05 » (heure UTC du run) */
function formatRunId(runId) {
    const m = /^(\d{4})(\d{2})(\d{2})-(\d{2})(\d{2})/.exec(runId || '');
    return m ? `${m[3]}/${m[2]}/${m[1]} ${m[4]}:${m[5]}` : runId;
}

/* ------------------------------------------------------------------ */
/* Avertissements d'un run (étape sémantique sautée, listes exclues…)
/* ------------------------------------------------------------------ */

const PHASE_LABELS = { semantic: 'Étape sémantique', fuzzy: 'Détection floue', api: 'Source API Insee' };

function renderRunWarnings(container, warnings) {
    if (!container) return;
    container.innerHTML = '';
    for (const w of warnings || []) {
        const div = document.createElement('div');
        div.className = 'alert alert-warning py-2 small mb-2';
        const label = PHASE_LABELS[w.phase] || w.phase;
        const state = { skipped: 'non exécutée', disabled: 'désactivée', partial: 'partielle' }[w.status] || w.status;
        div.textContent = `⚠ ${label} ${state} : ${w.reason}`;
        container.appendChild(div);
    }
}

/* ------------------------------------------------------------------ */
/* Toast notifications (shared across all scripts)
/* ------------------------------------------------------------------ */

/* Le message est du texte (jamais interprété comme HTML).
   options.actionLabel + options.onAction ajoutent un bouton (ex. « Annuler »). */
function showToast(message, type = 'info', options = {}) {
    const container = document.getElementById('toastContainer');
    if (!container) return;

    const colors = {
        success: '#198754',
        danger: '#dc3545',
        warning: '#ffc107',
        info: '#0dcaf0',
    };
    const darkText = type === 'warning' || type === 'info';

    const toastEl = document.createElement('div');
    toastEl.className = 'toast align-items-center border-0 app-toast';
    toastEl.setAttribute('role', type === 'danger' ? 'alert' : 'status');
    toastEl.style.backgroundColor = colors[type] || '#6c757d';

    const body = document.createElement('div');
    body.className = `toast-body flex-grow-1 ${darkText ? 'text-dark' : 'text-white'}`;
    body.textContent = message;
    toastEl.appendChild(body);

    let toast;
    if (options.actionLabel && options.onAction) {
        const action = document.createElement('button');
        action.type = 'button';
        action.className = `btn btn-sm ${darkText ? 'btn-dark' : 'btn-light'} me-2 flex-shrink-0`;
        action.textContent = options.actionLabel;
        action.addEventListener('click', () => {
            toast.hide();
            options.onAction();
        });
        toastEl.appendChild(action);
    }

    const close = document.createElement('button');
    close.type = 'button';
    close.className = `btn-close ${darkText ? '' : 'btn-close-white'} me-2 flex-shrink-0`;
    close.setAttribute('data-bs-dismiss', 'toast');
    close.setAttribute('aria-label', 'Fermer');
    toastEl.appendChild(close);

    container.appendChild(toastEl);
    // Un toast avec action reste plus longtemps : le temps de cliquer « Annuler »
    toast = new bootstrap.Toast(toastEl, { delay: options.onAction ? 10000 : 4000 });
    toast.show();
    toastEl.addEventListener('hidden.bs.toast', () => toastEl.remove());
}

/* ------------------------------------------------------------------ */
/* Confirmation (modale Bootstrap à la place de window.confirm)
/* ------------------------------------------------------------------ */

/* Résout true si l'utilisateur confirme. options : title, confirmLabel, danger. */
function confirmDialog(message, options = {}) {
    const modalEl = document.getElementById('confirmModal');
    const modal = bootstrap.Modal.getOrCreateInstance(modalEl);
    document.getElementById('confirmModalTitle').textContent = options.title || 'Confirmer';
    document.getElementById('confirmModalBody').textContent = message;
    const ok = document.getElementById('confirmModalOk');
    ok.textContent = options.confirmLabel || 'Confirmer';
    ok.className = `btn ${options.danger ? 'btn-danger' : 'btn-primary'}`;

    return new Promise((resolve) => {
        let confirmed = false;
        const onOk = () => {
            confirmed = true;
            modal.hide();
        };
        ok.addEventListener('click', onOk, { once: true });
        modalEl.addEventListener('hidden.bs.modal', () => {
            ok.removeEventListener('click', onOk);
            resolve(confirmed);
        }, { once: true });
        modalEl.addEventListener('shown.bs.modal', () => ok.focus(), { once: true });
        modal.show();
    });
}

/* ------------------------------------------------------------------ */
/* API helper
/* ------------------------------------------------------------------ */

async function apiRequest(path, options = {}) {
    const url = path.startsWith('http') ? path : API_BASE + path;
    const defaultHeaders = {
        'Content-Type': 'application/json',
        ...options.headers,
    };

    return fetch(url, {
        ...options,
        headers: defaultHeaders,
    });
}

/* ------------------------------------------------------------------ */
/* Health check (called on every page load)
/* ------------------------------------------------------------------ */

function checkHealth() {
    const el = document.getElementById('health-status');
    fetch(API_BASE + 'health')
        .then(r => r.json())
        .then(d => {
            if (!el) return;
            const healthy = d.status === 'healthy';
            const jobs = d.jobs_active ? ` · ${d.jobs_active} pipeline(s) en cours` : '';
            el.textContent = `● ${healthy ? 'En ligne' : 'Déconnecté'}${jobs}`;
            el.className = `navbar-text ms-auto small text-${healthy ? 'success' : 'danger'}`;
        })
        .catch(() => {
            if (!el) return;
            el.textContent = '● Déconnecté';
            el.className = 'navbar-text ms-auto small text-danger';
        });
}

/* ------------------------------------------------------------------ */
/* DOM Ready
/* ------------------------------------------------------------------ */

document.addEventListener('DOMContentLoaded', () => {
    checkHealth();
    setInterval(checkHealth, 30000);

    // Chemins dérivés de la racine des sorties, sauf s'ils ont été modifiés à la main
    for (const id of ['cleanedPath', 'pipelineRegistryPath']) {
        const el = document.getElementById(id);
        if (el) el.addEventListener('input', () => { el.dataset.edited = '1'; });
    }
    setOutputRoot(getOutputRoot());
});
