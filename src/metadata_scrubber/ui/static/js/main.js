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

// Fournie par le serveur (SCRUBBER_OUTPUT_ROOT), pour qu'une instance V2 écrive à part
const DEFAULT_OUTPUT_ROOT = document.querySelector('meta[name="scrubber-output-root"]')?.content
    || 's3://projet-metadonnees-rmes/scrubber_output';

/* Racine des sorties : un seul réglage, partagé par les onglets
   (barre du haut). Les écrans qui en dépendent écoutent « outputrootchange ». */
function getOutputRoot() {
    return loadPref('outputRoot', DEFAULT_OUTPUT_ROOT).replace(/\/+$/, '');
}

function setOutputRoot(root, { notify = false } = {}) {
    root = (root || '').trim().replace(/\/+$/, '');
    if (!root) return;
    const changed = root !== getOutputRoot();
    savePref('outputRoot', root);
    const input = document.getElementById('outputRoot');
    if (input && input.value.trim().replace(/\/+$/, '') !== root) input.value = root;
    document.querySelectorAll('[data-output-root]').forEach(el => {
        el.textContent = root;
        el.title = root;
    });
    const cleaned = document.getElementById('cleanedPath');
    if (cleaned && !cleaned.dataset.edited) cleaned.value = `${root}/cleaned_codelists.json`;
    const pipelineRegistry = document.getElementById('pipelineRegistryPath');
    if (pipelineRegistry && !pipelineRegistry.dataset.edited) {
        pipelineRegistry.value = `${root}/cleaned_codelists.json`;
    }
    if (notify && changed) document.dispatchEvent(new CustomEvent('outputrootchange', { detail: root }));
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

/* Étiquette de niveau : validé (plein) ou seulement suggéré (pointillé, « ? ») */
function levelTagHtml(level, suggested) {
    if (level) {
        return `<span class="tag lvl-${escapeHtml(level)}" title="Niveau validé dans le référentiel">`
            + `${escapeHtml(LEVEL_LABELS[level] || level)}</span>`;
    }
    if (suggested) {
        return `<span class="tag suggested lvl-${escapeHtml(suggested)}" title="Niveau suggéré d'après les séries qui utilisent la liste">`
            + `${escapeHtml(LEVEL_LABELS[suggested] || suggested)} ?</span>`;
    }
    return '';
}

/* Icône du sprite de index.html */
function icon(name, cls = '') {
    return `<svg class="i${cls ? ` ${cls}` : ''}" aria-hidden="true"><use href="#i-${name}"/></svg>`;
}

/* Contenu d'un bouton pendant une action longue (le libellé est restauré ensuite) */
function setBusy(btn, busy, label = '') {
    const target = btn.querySelector('.label') || btn;
    if (busy) {
        btn.dataset.idleLabel = target.textContent;
        target.textContent = label || target.textContent;
    } else if (btn.dataset.idleLabel !== undefined) {
        target.textContent = btn.dataset.idleLabel;
        delete btn.dataset.idleLabel;
    }
    btn.disabled = busy;
}

/* « s1998, s2219 » → ["s1998", "s2219"] */
function parseIds(text) {
    return (text || '').split(/[\s,;]+/).map(s => s.trim()).filter(Boolean);
}

/* Texte sans accents ni majuscules, pour filtrer */
function foldText(s) {
    return (s || '').normalize('NFD').replace(/[̀-ͯ]/g, '').toLowerCase();
}

/* `text` échappé, avec la première occurrence de `query` (déjà repliée) en <mark> */
function highlightHtml(text, query) {
    const chars = [...(text || '')];
    const folded = chars.map(foldText);
    const at = query && folded.every(c => c.length === 1) ? folded.join('').indexOf(query) : -1;
    if (at < 0) return escapeHtml(text);
    const end = at + query.length;
    return escapeHtml(chars.slice(0, at).join('')) + '<mark>' + escapeHtml(chars.slice(at, end).join(''))
        + '</mark>' + escapeHtml(chars.slice(end).join(''));
}

/* Sélecteur d'identifiants (combobox ARIA) : on tape un identifiant ou un nom,
   une liste « id – nom » propose les options, chaque choix devient une étiquette.
   Les identifiants choisis sont recopiés, séparés par des virgules, dans
   `valueInput` ; un événement `change` est émis sur `root`. Un identifiant
   absent des options reste accepté (étiquette signalée). */
const ID_PICKER_LIMIT = 50;

function createIdPicker(root, { inputId, valueInput, placeholder = '', emptyText = 'Aucun résultat' }) {
    const listId = `${inputId}-list`;
    root.innerHTML = `
        <div class="idpick-box">
            <input type="text" id="${inputId}" role="combobox" aria-autocomplete="list"
                aria-expanded="false" aria-controls="${listId}" autocomplete="off" spellcheck="false">
        </div>
        <ul class="idpick-list" id="${listId}" role="listbox" hidden></ul>`;
    const box = root.querySelector('.idpick-box');
    const input = root.querySelector('input');
    const list = root.querySelector('ul');
    let options = [];
    let byId = new Map();
    let values = [];
    let shown = [];
    let active = -1;
    let empty = emptyText;
    let hint = placeholder;

    renderTags();

    function commit() {
        valueInput.value = values.join(', ');
        renderTags();
        root.dispatchEvent(new CustomEvent('change'));
    }

    function renderTags() {
        box.querySelectorAll('.tag').forEach(t => t.remove());
        const tags = values.map(id => {
            const opt = byId.get(id);
            const tag = document.createElement('span');
            tag.className = `tag ${opt ? 'accent' : 'warn'}`;
            tag.title = opt ? `${id} – ${opt.label}` : `${id} : identifiant absent de la liste`;
            tag.innerHTML = `<span class="mono">${escapeHtml(id)}</span>`
                + (opt?.label ? `<span class="lab">${escapeHtml(opt.label)}</span>` : '')
                + `<button type="button" data-remove="${escapeHtml(id)}" aria-label="Retirer ${escapeHtml(id)}"
                    ${input.disabled ? 'disabled' : ''}>${icon('x', 'sm')}</button>`;
            return tag;
        });
        input.before(...tags);
        input.placeholder = values.length ? '' : hint;
    }

    function matches() {
        const q = foldText(input.value.trim());
        const taken = new Set(values);
        const hits = options.filter(o => !taken.has(o.id)
            && (!q || foldText(o.id).includes(q) || foldText(o.label).includes(q)));
        if (q) hits.sort((a, b) => foldText(b.id).startsWith(q) - foldText(a.id).startsWith(q));
        return { q, hits };
    }

    function renderList() {
        const { q, hits } = matches();
        shown = hits.slice(0, ID_PICKER_LIMIT);
        active = shown.length ? Math.min(Math.max(active, 0), shown.length - 1) : -1;
        const rows = shown.map((o, i) => `
            <li role="option" id="${listId}-${i}" data-index="${i}" aria-selected="${i === active}">
                <span class="id">${highlightHtml(o.id, q)}</span>
                <span class="lab">${highlightHtml(o.label, q) || '<em>sans nom</em>'}</span>
                ${o.hint ? `<span class="hint">${escapeHtml(o.hint)}</span>` : ''}
            </li>`);
        if (hits.length > shown.length) {
            rows.push(`<li class="more">${hits.length - shown.length} autres : précisez la saisie</li>`);
        }
        list.innerHTML = rows.join('') || `<li class="more">${escapeHtml(empty)}</li>`;
        input.setAttribute('aria-activedescendant', active >= 0 ? `${listId}-${active}` : '');
        list.querySelector('[aria-selected="true"]')?.scrollIntoView({ block: 'nearest' });
    }

    function open() {
        if (input.disabled) return;
        list.hidden = false;
        input.setAttribute('aria-expanded', 'true');
        renderList();
    }

    function close() {
        list.hidden = true;
        input.setAttribute('aria-expanded', 'false');
        input.removeAttribute('aria-activedescendant');
        active = -1;
    }

    /* Ajoute des identifiants saisis : la casse de l'option connue est reprise */
    function add(ids) {
        const fresh = ids.map(id => options.find(o => o.id.toLowerCase() === id.toLowerCase())?.id || id)
            .filter(id => !values.includes(id));
        if (!fresh.length) return;
        values = [...values, ...new Set(fresh)];
        input.value = '';
        commit();
    }

    function pick(i) {
        if (shown[i]) add([shown[i].id]);
        close();
    }

    function remove(id) {
        values = values.filter(v => v !== id);
        commit();
    }

    input.addEventListener('input', () => { active = 0; open(); });
    input.addEventListener('click', () => { if (list.hidden) open(); });
    input.addEventListener('keydown', (e) => {
        if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
            e.preventDefault();
            if (list.hidden) return open();
            if (shown.length) {
                active = (active + (e.key === 'ArrowDown' ? 1 : -1) + shown.length) % shown.length;
                renderList();
            }
        } else if (e.key === 'Enter') {
            e.preventDefault();
            // Un identifiant exact l'emporte, puis l'option active, puis la saisie brute
            const typed = parseIds(input.value);
            const exact = typed.length === 1 && options.some(o => o.id.toLowerCase() === typed[0].toLowerCase());
            if (!exact && !list.hidden && shown[active]) {
                pick(active);
            } else if (typed.length) {
                add(typed);
                close();
            }
        } else if (e.key === 'Escape' && !list.hidden) {
            e.preventDefault();
            e.stopPropagation();
            close();
        } else if (e.key === 'Backspace' && !input.value && values.length) {
            remove(values[values.length - 1]);
            if (!list.hidden) renderList();
        } else if (e.key === 'Tab') {
            close();
        }
    });
    input.addEventListener('paste', (e) => {
        const ids = parseIds(e.clipboardData?.getData('text'));
        if (ids.length > 1) {
            e.preventDefault();
            add(ids);
        }
    });
    box.addEventListener('click', (e) => {
        const btn = e.target.closest('[data-remove]');
        if (btn) {
            remove(btn.dataset.remove);
            input.focus();
        } else if (e.target === box) {
            input.focus();
        }
    });
    // mousedown : la saisie garde le focus pendant le choix
    list.addEventListener('mousedown', e => e.preventDefault());
    list.addEventListener('click', (e) => {
        const li = e.target.closest('li[data-index]');
        if (li) pick(Number(li.dataset.index));
    });
    root.addEventListener('focusout', (e) => {
        if (!root.contains(e.relatedTarget)) close();
    });

    return {
        input,
        getValues: () => [...values],
        setValues(ids) {
            values = [...new Set(ids)];
            commit();
        },
        setOptions(list_, emptyMessage = emptyText) {
            options = list_;
            byId = new Map(options.map(o => [o.id, o]));
            empty = emptyMessage;
            renderTags();
            if (!list.hidden) renderList();
        },
        setDisabled(disabled, disabledPlaceholder = '') {
            input.disabled = disabled;
            root.classList.toggle('disabled', disabled);
            hint = (disabled && disabledPlaceholder) || placeholder;
            if (disabled) close();
            renderTags();
        },
    };
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
        div.className = 'notice';
        const label = PHASE_LABELS[w.phase] || w.phase;
        const state = { skipped: 'non exécutée', disabled: 'désactivée', partial: 'partielle' }[w.status] || w.status;
        div.innerHTML = icon('alert');
        const text = document.createElement('span');
        text.textContent = `${label} ${state} : ${w.reason}`;
        div.appendChild(text);
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
    const icons = { success: 'check', danger: 'alert', warning: 'alert', info: 'info' };

    const toastEl = document.createElement('div');
    toastEl.className = `toast app-toast ${type}`;
    toastEl.setAttribute('role', type === 'danger' ? 'alert' : 'status');
    toastEl.innerHTML = `<span class="toast-ico">${icon(icons[type] || 'info')}</span>`;

    const body = document.createElement('div');
    body.className = 'toast-body';
    body.textContent = message;
    toastEl.appendChild(body);

    let toast;
    if (options.actionLabel && options.onAction) {
        const action = document.createElement('button');
        action.type = 'button';
        action.className = 'toast-action';
        action.textContent = options.actionLabel;
        action.addEventListener('click', () => {
            toast.hide();
            options.onAction();
        });
        toastEl.appendChild(action);
    }

    const close = document.createElement('button');
    close.type = 'button';
    close.className = 'toast-close';
    close.setAttribute('data-bs-dismiss', 'toast');
    close.setAttribute('aria-label', 'Fermer');
    close.innerHTML = icon('x');
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
    if (!el) return;
    const show = (online, text) => {
        el.className = `ctx-item ${online ? 'online' : 'offline'}`;
        el.querySelector('.label').textContent = text;
    };
    fetch(API_BASE + 'health')
        .then(r => r.json())
        .then(d => {
            const healthy = d.status === 'healthy';
            const jobs = d.jobs_active ? ` · ${d.jobs_active} analyse(s) en cours` : '';
            show(healthy, `${healthy ? 'En ligne' : 'Déconnecté'}${jobs}`);
        })
        .catch(() => show(false, 'Déconnecté'));
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

    document.getElementById('outputRootForm').addEventListener('submit', (e) => {
        e.preventDefault();
        setOutputRoot(document.getElementById('outputRoot').value, { notify: true });
        bootstrap.Dropdown.getOrCreateInstance(e.target.previousElementSibling).hide();
    });
    document.getElementById('ctxRun').addEventListener('click', () => switchTab('validation'));
});
