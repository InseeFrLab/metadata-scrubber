/* pipeline.js — Gestion du pipeline (form, SSE progress, logs) */

// API_BASE is defined in config.js (loaded first)

function showToast(message, type = 'info') {
    const container = document.getElementById('toastContainer');
    const toastEl = document.createElement('div');
    toastEl.className = `toast align-items-center text-bg-${type} border-0`;
    toastEl.setAttribute('role', 'alert');
    toastEl.innerHTML = `
        <div class="d-flex">
            <div class="toast-body">${message}</div>
            <button type="button" class="btn-close btn-close-white me-2 m-auto" data-bs-dismiss="toast"></button>
        </div>
    `;
    container.appendChild(toastEl);
    const toast = new bootstrap.Toast(toastEl, { delay: 5000 });
    toast.show();
    toastEl.addEventListener('hidden.bs.toast', () => toastEl.remove());
}

/* ------------------------------------------------------------------ */

async function launchPipeline() {
    const xmlSource = document.getElementById('xmlSource').value.trim();
    const outputBase = document.getElementById('outputBase').value.trim();
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
        logsContainer.textContent = logs.map(l => `[${l.ts}] ${l.text}`).join('\n');
        logsContainer.scrollTop = logsContainer.scrollHeight;
        logCountEl.textContent = `(${logs.length})`;
    }

    // --- EventSource ---
    const es = new EventSource(API_BASE + `api/pipeline/${jobId}/sse`);

    es.addEventListener('init', (ev) => {
        const d = JSON.parse(ev.data);
        if (d.status === 'running' || d.status === 'pending') {
            updateUI(d.progress, d.current_log, d.phase, d.phase_label);
        }
        if (d.logs) d.logs.forEach(appendLog);
    });

    es.addEventListener('progress', (ev) => {
        const d = JSON.parse(ev.data);
        updateUI(d.progress, d.message, d.phase, d.phase_label);
    });

    es.addEventListener('log', (ev) => {
        const d = JSON.parse(ev.data);
        appendLog(d.message);
    });

    es.addEventListener('done', (ev) => {
        const d = JSON.parse(ev.data);
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
    });

    es.addEventListener('error', (ev) => {
        console.error('SSE error:', ev);
    });

    es.onerror = () => {
        console.error('Connection lost');
        const lb = document.getElementById('launchBtn');
        lb.disabled = false;
        lb.textContent = '🚀 Lancer le pipeline';
        showToast('Connexion SSE perdue', 'danger');
    };

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
