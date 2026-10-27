/* pipeline.js — Gestion du pipeline (form, SSE progress, logs) */

// API_BASE is defined in config.js (loaded first)

// showToast is defined in main.js — shared globally


/* ------------------------------------------------------------------ */

function sourceIsApi() {
    return document.getElementById('sourceApi').checked;
}

/* Champs de la source choisie (XML ou API) ; séries de l'API chargées une fois */
let seriesPicker = null;
let operationsPicker = null;
let seriesLoaded = false;
function toggleSourceFields() {
    const api = sourceIsApi();
    document.getElementById('sourceXmlFields').classList.toggle('d-none', api);
    document.getElementById('sourceApiFields').classList.toggle('d-none', !api);
    if (api) loadSeries();
}

function loadSeries() {
    if (seriesLoaded) return;
    seriesLoaded = true;
    seriesPicker.setOptions([], 'Chargement des séries…');
    fetch(`${API_BASE}api/insee/series`)
        .then(r => (r.ok ? r.json() : Promise.reject(new Error(r.status))))
        .then(series => seriesPicker.setOptions(series, 'Aucune série ne correspond'))
        .catch(() => {
            // Nouvel essai au prochain passage dans le champ
            seriesLoaded = false;
            seriesPicker.setOptions([], 'API Insee indisponible : tapez un identifiant puis Entrée');
        });
}

/* Opérations d'une série, gardées pour la session (null si indisponibles) */
const serieOperations = new Map();
function fetchSerieOperations(serieId) {
    if (!serieOperations.has(serieId)) {
        const p = fetch(`${API_BASE}api/insee/series/${encodeURIComponent(serieId)}/operations`)
            .then(r => (r.ok ? r.json() : null))
            .catch(() => null)
            .then((ops) => {
                if (ops === null) serieOperations.delete(serieId);
                return ops;
            });
        serieOperations.set(serieId, p);
    }
    return serieOperations.get(serieId);
}

/* Les opérations proposées sont celles des séries choisies : elles restreignent la série */
let operationsRequest = 0;
let operationsState = { series: [], options: [], failed: [] };
async function refreshOperations() {
    const all = document.getElementById('apiAll').checked;
    const series = seriesPicker.getValues();
    seriesPicker.setDisabled(all);
    const request = ++operationsRequest;
    let options = [];
    let failed = [];
    if (!all && series.length) {
        document.getElementById('apiOperationsNote').textContent = 'Chargement des opérations…';
        const lists = await Promise.all(series.map(fetchSerieOperations));
        if (request !== operationsRequest) return;
        options = series.flatMap((sid, i) => (lists[i] || [])
            .map(op => ({ ...op, hint: series.length > 1 ? sid : '' })));
        failed = series.filter((_, i) => lists[i] === null);
    }
    // Les opérations d'une série retirée partent avec elle
    const previous = new Set(operationsState.options.map(o => o.id));
    const known = new Set(options.map(o => o.id));
    operationsState = { all, series, options, failed };
    operationsPicker.setOptions(options, 'Aucune opération ne correspond');
    operationsPicker.setValues(series.length && !all
        ? operationsPicker.getValues().filter(id => known.has(id) || !previous.has(id)) : []);
}

function updateOperationsNote() {
    const { all, series, options, failed } = operationsState;
    const chosen = operationsPicker.getValues().length;
    const note = document.getElementById('apiOperationsNote');
    operationsPicker.setDisabled(all || !series.length || (options.length <= 1 && !chosen && !failed.length),
        all ? 'Toutes les opérations' : series.length ? '' : 'Choisissez d\'abord une série');
    if (all || !series.length) {
        note.textContent = '';
    } else if (failed.length) {
        note.textContent = `Opérations indisponibles pour ${failed.join(', ')} (API Insee) : `
            + 'tapez un identifiant puis Entrée.';
    } else if (!options.length) {
        note.textContent = series.length > 1 ? 'Ces séries ne contiennent aucune opération.'
            : 'Cette série ne contient aucune opération.';
    } else if (options.length === 1) {
        note.textContent = `Une seule opération : ${options[0].id} – ${options[0].label}. Elle est chargée.`;
    } else if (chosen) {
        const s = chosen > 1 ? 's' : '';
        note.textContent = `${chosen} opération${s} choisie${s} sur ${options.length} ; les autres ne sont pas chargées.`;
    } else {
        note.textContent = `${options.length} opérations : sans choix, toutes sont chargées.`;
    }
}

function initApiPickers() {
    seriesPicker = createIdPicker(document.getElementById('apiSeriesPicker'), {
        inputId: 'apiSeriesInput',
        valueInput: document.getElementById('apiSeries'),
        placeholder: 'Identifiant ou nom, ex. s1998 ou Tous salariés',
    });
    operationsPicker = createIdPicker(document.getElementById('apiOperationsPicker'), {
        inputId: 'apiOperationsInput',
        valueInput: document.getElementById('apiOperations'),
        placeholder: 'Toutes les opérations de la série',
    });
    seriesPicker.input.addEventListener('focus', loadSeries);
    document.getElementById('apiSeriesPicker').addEventListener('change', refreshOperations);
    document.getElementById('apiOperationsPicker').addEventListener('change', updateOperationsNote);
    document.getElementById('apiAll').addEventListener('change', refreshOperations);
    refreshOperations();
}

async function launchPipeline() {
    const xmlSource = document.getElementById('xmlSource').value.trim();
    const outputBase = getOutputRoot();
    const registryPath = document.getElementById('pipelineRegistryPath').value.trim();
    const runLlm = document.getElementById('runLlm').checked;
    const verbose = document.getElementById('verbose').checked;
    const api = sourceIsApi();
    const apiSeries = parseIds(document.getElementById('apiSeries').value);
    const apiOperations = parseIds(document.getElementById('apiOperations').value);
    const apiAll = document.getElementById('apiAll').checked;

    if (!api && !xmlSource) {
        showToast('Veuillez saisir un chemin DDI (local ou S3).', 'warning');
        return;
    }
    if (api && !apiSeries.length && !apiOperations.length && !apiAll) {
        showToast('Choisissez au moins une série, ou cochez « Toute la base ».', 'warning');
        return;
    }
    if (!outputBase) {
        showToast('Choisissez un dossier de sortie dans la barre du haut.', 'warning');
        return;
    }

    // --- UI: loading ---
    const launchBtn = document.getElementById('launchBtn');
    setBusy(launchBtn, true, 'Lancement…');

    const progressSection = document.getElementById('progressSection');
    progressSection.classList.remove('d-none');
    document.getElementById('resultSection').classList.add('d-none');
    document.getElementById('logsContainer').textContent = '';
    renderPhaseStrip(0);
    lastRunRegistryPath = null;

    // --- API call ---
    try {
        const resp = await fetch(API_BASE + 'api/pipeline', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                xml_source: api ? null : xmlSource,
                api_series: api && !apiAll ? apiSeries : [],
                api_operations: api && !apiAll ? apiOperations : [],
                api_all_operations: api && apiAll,
                output_base: outputBase,
                run_llm: runLlm,
                verbose: verbose,
                registry_path: registryPath || null,
            }),
        });

        if (!resp.ok) {
            throw new Error(`HTTP ${resp.status}: ${await resp.text()}`);
        }

        const data = await resp.json();
        const jobId = data.job_id;
        showToast(`Analyse lancée · job ${jobId}`, 'info');

        connectSSE(jobId);

    } catch (err) {
        console.error('Lancement pipeline:', err);
        showToast(`Erreur: ${err.message}`, 'danger');
        setBusy(launchBtn, false);
    }
}

/* Frise des étapes du pipeline (numéros de phase de main.py regroupés) */
const PHASE_GROUPS = [
    ['Lecture', [0, 1]],
    ['Extraction', [2, 3]],
    ['Signatures et exact', [4, 5]],
    ['Flou et usage', [6, 7]],
    ['Sémantique', [8]],
    ['Registre', [9]],
];

function renderPhaseStrip(phase, status = 'running') {
    const current = PHASE_GROUPS.findIndex(([, phases]) => phases.includes(phase));
    document.getElementById('phaseStrip').innerHTML = PHASE_GROUPS.map(([label], i) => {
        let cls = i < current ? 'done' : i === current ? 'now' : '';
        if (status === 'success') cls = 'done';
        if (status === 'error' && i === current) cls = 'failed';
        return `<div class="phase ${cls}"><i></i>${escapeHtml(label)}</div>`;
    }).join('');
}

/* ------------------------------------------------------------------ */

function connectSSE(jobId) {
    const progressPct = document.getElementById('progressPct');
    const phaseLabelEl = document.getElementById('phaseLabel');
    const logsContainer = document.getElementById('logsContainer');
    const logCountEl = document.getElementById('logCount');
    const resultSection = document.getElementById('resultSection');

    const phaseNames = {
        0: 'Lancement…',
        1: 'Lecture & parsing XML…',
        2: 'Extraction CodeLists…',
        3: 'Extraction variables…',
        4: 'Signature de contenu…',
        5: 'Détection exacte…',
        6: 'Détection floue…',
        7: 'Signaux d\'usage…',
        8: 'Détection sémantique…',
        9: 'Génération du registre…',
    };

    let logs = [];  // array of { text, ts }

    // Fallback polling : certains proxys (port forward VS Code) bufferisent
    // le flux SSE et ne le délivrent qu'à la fermeture. Si le SSE reste
    // silencieux > 3 s pendant que le job tourne, on bascule sur du polling
    // du endpoint /status toutes les 2 s (bascule définitive pour ce job).
    let lastEventTs = Date.now();
    let finished = false;
    let usePolling = false;

    let lastPhase = 0;
    function updateUI(progress, message, phase, phase_label) {
        const pct = Math.min(Math.round((progress ?? 0) * 100), 100);
        progressPct.textContent = `${pct} %`;
        phaseLabelEl.textContent = phase_label || message || phaseNames[phase] || '';
        if (phase != null) {
            lastPhase = phase;
            renderPhaseStrip(phase);
        }
    }

    function appendLog(text) {
        if (!text) return;
        // Truncate very long lines
        text = text.length > 500 ? text.substring(0, 500) + '…' : text;
        // Avoid duplicate consecutive lines
        if (logs.length > 0 && logs[logs.length - 1].text === text) return;

        const ts = new Date().toLocaleTimeString('fr-FR');
        logs.push({ text, ts });
        renderLogs();
    }

    function renderLogs() {
        logsContainer.textContent = logs.map(l => `[${l.ts}] ${l.text}`).join('\n');
        logsContainer.scrollTop = logsContainer.scrollHeight;
        logCountEl.textContent = `(${logs.length})`;
    }

    // Remplace l'affichage par le snapshot du polling (50 derniers logs) —
    // évite les doublons quand on mélange sources SSE et polling.
    function setLogsFromSnapshot(lines) {
        const ts = new Date().toLocaleTimeString('fr-FR');
        const prevTs = new Map(logs.map(l => [l.text, l.ts]));
        logs = lines
            .filter(t => t)
            .map(text => {
                const t = text.length > 500 ? text.substring(0, 500) + '…' : text;
                return { text: t, ts: prevTs.get(t) || ts };
            });
        renderLogs();
    }

    // --- EventSource ---
    const es = new EventSource(API_BASE + `api/pipeline/${jobId}/sse`);

    // Fin de job (SSE "done" ou polling) — le premier arrivé gagne.
    function finishJob(d) {
        if (finished) return;
        finished = true;
        clearInterval(poller);
        es.close();

        if (d.status === 'success') {
            showSuccess(d);
        } else {
            showError(d);
        }

        setBusy(document.getElementById('launchBtn'), false);
    }

    es.addEventListener('init', (ev) => {
        lastEventTs = Date.now();
        const d = JSON.parse(ev.data);
        if (d.status === 'running' || d.status === 'pending') {
            updateUI(d.progress, d.current_log, d.phase, d.phase_label);
        }
        if (d.logs) d.logs.forEach(appendLog);
    });

    es.addEventListener('progress', (ev) => {
        lastEventTs = Date.now();
        if (usePolling) return;  // rafale SSE bufferisée arrivée en retard
        const d = JSON.parse(ev.data);
        updateUI(d.progress, d.message, d.phase, d.phase_label);
    });

    es.addEventListener('log', (ev) => {
        lastEventTs = Date.now();
        if (usePolling) return;
        const d = JSON.parse(ev.data);
        appendLog(d.message);
    });

    es.addEventListener('done', (ev) => {
        finishJob(JSON.parse(ev.data));
    });

    es.addEventListener('error', (ev) => {
        console.error('SSE error:', ev);
    });

    es.onerror = () => {
        // Fermeture normale (après "done") → pas une erreur
        if (es.readyState === EventSource.CLOSED) return;
        // SSE mort en cours de job → le polling prend le relais
        if (!finished) {
            console.warn('SSE indisponible — bascule sur le polling');
            usePolling = true;
        }
    };

    // Poller de secours (voir commentaire en tête de connectSSE)
    const poller = setInterval(async () => {
        if (finished) { clearInterval(poller); return; }
        if (!usePolling && Date.now() - lastEventTs < 3000) return;  // SSE vivant
        usePolling = true;
        try {
            const r = await fetch(`${API_BASE}api/pipeline/${jobId}/status`);
            if (!r.ok) return;
            const d = await r.json();
            updateUI(d.progress, d.current_log, d.phase, d.phase_label);
            if (d.logs) setLogsFromSnapshot(d.logs);
            if (d.status === 'success' || d.status === 'error') {
                finishJob({ status: d.status, result: d.result, error: d.error_message });
            }
        } catch (e) { /* serveur temporairement injoignable → on réessaie */ }
    }, 2000);

    window._sseConnection = es;

    /* ---- helpers ---- */

    function showSuccess(data) {
        progressPct.textContent = '100 %';
        phaseLabelEl.textContent = 'Analyse terminée';
        renderPhaseStrip(9, 'success');
        document.getElementById('logsDetails').open = false;

        resultSection.classList.remove('d-none');
        document.getElementById('resultAlert').classList.remove('d-none');
        document.getElementById('resultError').classList.add('d-none');

        const dur = data.result?.duration_seconds;
        document.getElementById('resultDuration').textContent = dur ? `${dur.toFixed(1)} s` : '-';

        const fileLinks = document.getElementById('fileLinks');
        fileLinks.innerHTML = '';
        if (data.result?.output_files) {
            for (const [name, url] of Object.entries(data.result.output_files)) {
                const div = document.createElement('div');
                div.innerHTML = `<strong>${escapeHtml(name)}</strong> <code>${escapeHtml(url)}</code>`;
                fileLinks.appendChild(div);
            }

            // « Valider ce run » ouvre directement le registre du run
            lastRunRegistryPath = data.result.output_files['codelist_duplicates.json'] || null;
        }
        document.getElementById('goToValidationBtn').classList.toggle('d-none', !lastRunRegistryPath);

        renderRunWarnings(document.getElementById('resultWarnings'), data.result?.warnings);
        refreshRunsTable();

        showToast('Analyse terminée', 'success');
    }

    function showError(data) {
        phaseLabelEl.textContent = 'Analyse en échec';
        renderPhaseStrip(lastPhase, 'error');

        resultSection.classList.remove('d-none');
        document.getElementById('resultAlert').classList.add('d-none');
        const errorBox = document.getElementById('resultError');
        errorBox.classList.remove('d-none');
        errorBox.querySelector('.msg').textContent = data.error || data.error_message || 'Erreur inconnue.';
        refreshRunsTable();

        showToast('Analyse en échec : le journal du run est dans son dossier daté', 'danger');
    }
}

/* ------------------------------------------------------------------ */
/* Historique des runs (dossiers datés de la racine de sortie)
/* ------------------------------------------------------------------ */

const RUN_STATUS = {
    success: ['ok', 'Terminé'],
    error: ['ko', 'Échec'],
    unknown: ['', 'Inconnu'],
};

/* « api:s1998 » → « API · s1998 » ; un chemin → son nom de fichier */
function runSourceLabel(source) {
    source = source || '';
    return source.startsWith('api:') ? `API · ${source.slice(4)}` : (source.split('/').pop() || '—');
}

async function refreshRunsTable() {
    const body = document.getElementById('runsTableBody');
    if (!body) return;
    const root = getOutputRoot();
    try {
        const runs = await fetchRuns(root);
        if (!runs.length) {
            body.innerHTML = `<tr><td colspan="5" class="text-muted">Aucun run dans <code>${escapeHtml(root)}</code>.</td></tr>`;
            return;
        }
        body.innerHTML = runs.map(run => {
            let [cls, label] = RUN_STATUS[run.status] || RUN_STATUS.unknown;
            const warnings = run.warnings || [];
            if (run.status === 'success' && warnings.length) {
                cls = 'wait';
                label = `Terminé, ${warnings.length} avertissement(s)`;
            }
            const title = warnings.map(w => w.reason).join(' · ');
            const candidates = run.counts?.candidates ?? '—';
            return `<tr class="hoverable">
                <td class="num text-nowrap">${escapeHtml(formatRunId(run.run_id))}</td>
                <td class="text-break" title="${escapeHtml(run.xml_source || '')}">${escapeHtml(runSourceLabel(run.xml_source))}</td>
                <td><span class="st ${cls}" title="${escapeHtml(title)}"><span class="dot"></span>${escapeHtml(label)}</span></td>
                <td class="r">${escapeHtml(candidates)}</td>
                <td class="r">${run.status === 'error' ? '' : `<button class="btn btn-quiet btn-sm" type="button"
                    data-open-registry="${escapeHtml(run.registry_path)}">Valider</button>`}</td>
            </tr>`;
        }).join('');
    } catch (err) {
        body.innerHTML = `<tr><td colspan="5" class="text-danger">Liste des runs indisponible : ${escapeHtml(err.message)}</td></tr>`;
    }
}

/* ------------------------------------------------------------------ */

let lastRunRegistryPath = null;   // registre du dernier run terminé (« Valider ce run »)

function goToValidation() {
    if (lastRunRegistryPath) {
        openRegistry(lastRunRegistryPath);
    } else {
        switchTab('validation');
    }
}

/* ------------------------------------------------------------------ */

document.addEventListener('DOMContentLoaded', () => {
    // Pipeline form submit
    document.getElementById('launchBtn').addEventListener('click', (e) => {
        e.preventDefault();
        launchPipeline();
    });

    // Go to validation
    document.getElementById('goToValidationBtn').addEventListener('click', goToValidation);

    // La racine de sortie est commune aux onglets (barre du haut)
    document.addEventListener('outputrootchange', refreshRunsTable);

    // Source : fichier XML ou API Insee
    initApiPickers();
    for (const id of ['sourceXml', 'sourceApi']) {
        document.getElementById(id).addEventListener('change', toggleSourceFields);
    }

    // Historique des runs
    document.getElementById('refreshRunsBtn').addEventListener('click', refreshRunsTable);
    document.getElementById('runsTableBody').addEventListener('click', (e) => {
        const btn = e.target.closest('[data-open-registry]');
        if (btn) openRegistry(btn.dataset.openRegistry);
    });
    refreshRunsTable();
});
