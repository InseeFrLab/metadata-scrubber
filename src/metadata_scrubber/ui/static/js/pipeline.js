/* pipeline.js — Gestion du pipeline (form, SSE progress, logs) */

// API_BASE is defined in config.js (loaded first)

// showToast is defined in main.js — shared globally


/* ------------------------------------------------------------------ */

function sourceIsApi() {
    return document.getElementById('sourceApi').checked;
}

/* Champs de la source choisie (XML ou API) ; séries de l'API chargées une fois */
let seriesLoaded = false;
function toggleSourceFields() {
    const api = sourceIsApi();
    document.getElementById('sourceXmlFields').classList.toggle('d-none', api);
    document.getElementById('sourceApiFields').classList.toggle('d-none', !api);
    if (api && !seriesLoaded) {
        seriesLoaded = true;
        fetch(`${API_BASE}api/insee/series`)
            .then(r => (r.ok ? r.json() : []))
            .then(series => {
                document.getElementById('seriesOptions').innerHTML = series
                    .map(s => `<option value="${escapeHtml(s.id)}">${escapeHtml(s.label)}</option>`)
                    .join('');
            })
            .catch(() => { seriesLoaded = false; });
    }
}

async function launchPipeline() {
    const xmlSource = document.getElementById('xmlSource').value.trim();
    const outputBase = document.getElementById('outputBase').value.trim();
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
        showToast('Indiquez au moins une série ou une opération, ou cochez « Toute la base ».', 'warning');
        return;
    }
    if (!outputBase) {
        showToast('Veuillez saisir un dossier de sortie (local ou S3).', 'warning');
        return;
    }
    setOutputRoot(outputBase);

    // --- UI: loading ---
    const launchBtn = document.getElementById('launchBtn');
    launchBtn.disabled = true;
    launchBtn.textContent = '⏳ Lancement en cours…';

    const progressSection = document.getElementById('progressSection');
    progressSection.classList.remove('d-none');
    document.getElementById('resultSection').classList.add('d-none');
    lastRunRegistryPath = null;

    // --- API call ---
    try {
        const resp = await fetch(API_BASE + 'api/pipeline', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                xml_source: api ? null : xmlSource,
                api_series: api ? apiSeries : [],
                api_operations: api ? apiOperations : [],
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
        showToast(`Pipeline lancé · job ${jobId}`, 'info');

        connectSSE(jobId);

    } catch (err) {
        console.error('Lancement pipeline:', err);
        showToast(`Erreur: ${err.message}`, 'danger');
        launchBtn.disabled = false;
        launchBtn.textContent = '🚀 Lancer le pipeline';
    }
}

/* ------------------------------------------------------------------ */

function connectSSE(jobId) {
    const progressBar = document.getElementById('progressBar');
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

    function updateUI(progress, message, phase, phase_label) {
        // Progress bar
        const pct = Math.min(Math.round((progress ?? 0) * 100), 100);
        progressBar.style.width = pct + '%';
        progressBar.textContent = pct + '%';

        // Striped/animated while running
        if (pct < 100) {
            progressBar.classList.add('progress-bar-striped', 'progress-bar-animated');
            progressBar.classList.remove('bg-success', 'bg-danger');
        } else {
            progressBar.classList.remove('progress-bar-striped', 'progress-bar-animated');
        }

        // Phase label
        phaseLabelEl.textContent = (phase_label || message || phaseNames[phase] || '') +
            (progress !== null ? ` (${pct}%)` : '');
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

        // Re-enable button
        const lb = document.getElementById('launchBtn');
        lb.disabled = false;
        lb.textContent = '🚀 Lancer le pipeline';
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
        progressBar.style.width = '100%';
        progressBar.className = 'progress-bar bg-success';
        progressBar.textContent = '100%';

        resultSection.classList.remove('d-none');
        document.getElementById('resultAlert').classList.remove('d-none');
        document.getElementById('resultError').classList.add('d-none');
        document.getElementById('goToValidationBtn').classList.remove('d-none');

        const dur = data.result?.duration_seconds;
        document.getElementById('resultDuration').textContent = dur
            ? dur.toFixed(1) + 's'
            : '-';

        const fileLinks = document.getElementById('fileLinks');
        fileLinks.innerHTML = '';
        if (data.result?.output_files) {
            for (const [name, url] of Object.entries(data.result.output_files)) {
                const div = document.createElement('div');
                div.className = 'mb-2';
                div.innerHTML = `<strong>${escapeHtml(name)}</strong><br><code class="text-break">${escapeHtml(url)}</code>`;
                fileLinks.appendChild(div);
            }

            // « Valider ce run » ouvre directement le registre du run
            lastRunRegistryPath = data.result.output_files['codelist_duplicates.json'] || null;
        }

        renderRunWarnings(document.getElementById('resultWarnings'), data.result?.warnings);
        refreshRunsTable();

        showToast('✅ Pipeline terminé avec succès !', 'success');
    }

    function showError(data) {
        progressBar.style.width = '100%';
        progressBar.className = 'progress-bar bg-danger';
        progressBar.textContent = '❌';

        resultSection.classList.remove('d-none');
        document.getElementById('resultAlert').classList.add('d-none');
        document.getElementById('goToValidationBtn').classList.add('d-none');
        const errorBox = document.getElementById('resultError');
        errorBox.classList.remove('d-none');
        errorBox.querySelector('p').textContent = data.error || data.error_message || 'Erreur inconnue';
        refreshRunsTable();

        showToast('❌ Pipeline échoué — le journal du run est dans son dossier daté', 'danger');
    }
}

/* ------------------------------------------------------------------ */
/* Historique des runs (dossiers datés de la racine de sortie)
/* ------------------------------------------------------------------ */

const RUN_STATUS_BADGES = {
    success: ['bg-success', 'terminé'],
    error: ['bg-danger', 'en erreur'],
    unknown: ['bg-secondary', 'inconnu'],
};

async function refreshRunsTable() {
    const body = document.getElementById('runsTableBody');
    if (!body) return;
    const root = getOutputRoot();
    try {
        const runs = await fetchRuns(root);
        if (!runs.length) {
            body.innerHTML = `<tr><td colspan="5" class="text-muted small p-3">Aucun run dans ${escapeHtml(root)}.</td></tr>`;
            return;
        }
        body.innerHTML = runs.map(run => {
            const [cls, label] = RUN_STATUS_BADGES[run.status] || RUN_STATUS_BADGES.unknown;
            const source = (run.xml_source || '').startsWith('api:')
                ? `API ${run.xml_source.slice(4)}`
                : (run.xml_source || '').split('/').pop() || '—';
            const candidates = run.counts?.candidates ?? '—';
            const warn = (run.warnings || []).length
                ? ` <span class="badge bg-warning text-dark" title="${escapeHtml(run.warnings.map(w => w.reason).join(' · '))}">⚠ ${run.warnings.length}</span>`
                : '';
            return `<tr>
                <td class="text-nowrap">${escapeHtml(formatRunId(run.run_id))}</td>
                <td class="text-break" title="${escapeHtml(run.xml_source || '')}">${escapeHtml(source)}</td>
                <td><span class="badge ${cls}">${label}</span>${warn}</td>
                <td class="text-end">${escapeHtml(candidates)}</td>
                <td class="text-end"><button class="btn btn-sm btn-outline-primary" type="button"
                    data-open-registry="${escapeHtml(run.registry_path)}"
                    ${run.status === 'error' ? 'disabled' : ''}>Ouvrir</button></td>
            </tr>`;
        }).join('');
    } catch (err) {
        body.innerHTML = `<tr><td colspan="5" class="text-danger small p-3">Liste des runs indisponible : ${escapeHtml(err.message)}</td></tr>`;
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

    // La racine de sortie est commune aux onglets
    document.getElementById('outputBase').addEventListener('change', (e) => {
        setOutputRoot(e.target.value);
        refreshRunsTable();
    });

    // Source : fichier XML ou API Insee
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
