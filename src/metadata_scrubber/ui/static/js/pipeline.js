/* pipeline.js — Gestion du pipeline (form, SSE progress, logs) */

// API_BASE is defined in config.js (loaded first)

// showToast is defined in main.js — shared globally


/* ------------------------------------------------------------------ */

async function launchPipeline() {
    const xmlSource = document.getElementById('xmlSource').value.trim();
    const outputBase = document.getElementById('outputBase').value.trim();
    const registryPath = document.getElementById('pipelineRegistryPath').value.trim();
    const runLlm = document.getElementById('runLlm').checked;
    const verbose = document.getElementById('verbose').checked;

    if (!xmlSource) {
        showToast('Veuillez saisir un chemin DDI (local ou S3).', 'warning');
        return;
    }
    if (!outputBase) {
        showToast('Veuillez saisir un dossier de sortie (local ou S3).', 'warning');
        return;
    }

    // --- UI: loading ---
    const launchBtn = document.getElementById('launchBtn');
    launchBtn.disabled = true;
    launchBtn.textContent = '⏳ Lancement en cours…';

    const progressSection = document.getElementById('progressSection');
    progressSection.classList.remove('d-none');
    document.getElementById('resultSection').classList.add('d-none');

    // --- API call ---
    try {
        const resp = await fetch(API_BASE + 'api/pipeline', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                xml_source: xmlSource,
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
                div.innerHTML = `<strong>${name}</strong><br><code class="text-break">${url}</code>`;
                fileLinks.appendChild(div);
            }

            // Pre-fill registry path in validation tab
            const dupUrl = data.result.output_files['codelist_duplicates.json'];
            if (dupUrl) {
                document.getElementById('registryPath').value = dupUrl;
                // Boucle incrémentale : préremplir aussi les chemins du registre nettoyé
                const cleanedUrl = dupUrl.replace(/codelist_duplicates\.json$/, 'cleaned_codelists.json');
                document.getElementById('cleanedPath').value = cleanedUrl;
                document.getElementById('pipelineRegistryPath').value = cleanedUrl;
            }
        }

        showToast('✅ Pipeline terminé avec succès !', 'success');
    }

    function showError(data) {
        progressBar.style.width = '100%';
        progressBar.className = 'progress-bar bg-danger';
        progressBar.textContent = '❌';

        resultSection.classList.remove('d-none');
        resultSection.querySelector('.alert').className = 'alert alert-danger';
        document.getElementById('resultAlert').innerHTML = `
            <h5>❌ Pipeline échoué</h5>
            <p>${data.error || data.error_message || 'Erreur inconnue'}</p>
        `;

        showToast('❌ Pipeline échoué', 'danger');
    }
}

/* ------------------------------------------------------------------ */

function goToValidation() {
    const tab = document.getElementById('validation-tab');
    const bsTab = new bootstrap.Tab(tab);
    bsTab.show();
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

    // Health check
    fetch(API_BASE + 'health')
        .then(r => r.json())
        .then(d => {
            const el = document.getElementById('health-status');
            el.textContent = `● ${d.jobs_active} job(s)`;
            el.className = 'navbar-text ms-auto text-success small';
        })
        .catch(() => {
            const el = document.getElementById('health-status');
            el.textContent = '● Déconnecté';
            el.className = 'navbar-text ms-auto text-danger small';
        });
});
