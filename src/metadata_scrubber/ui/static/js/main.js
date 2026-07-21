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
/* Toast notifications (shared across all scripts)
/* ------------------------------------------------------------------ */

function showToast(message, type = 'info') {
    const container = document.getElementById('toastContainer');
    if (!container) return;

    const colors = {
        success: '#198754',
        danger: '#dc3545',
        warning: '#ffc107',
        info: '#0dcaf0',
    };
    const bgColor = colors[type] || '#6c757d';

    const toastEl = document.createElement('div');
    toastEl.className = 'toast align-items-center border-0';
    toastEl.setAttribute('role', 'alert');
    toastEl.style.cssText = `
        background-color: ${bgColor};
        border-radius: 0.375rem;
        margin-bottom: 8px;
        display: flex;
        align-items: center;
        width: 100%;
        max-width: 600px;
        min-width: 400px;
        padding: 0.75rem;
    `;
    toastEl.innerHTML = `
        <div class="toast-body text-white">${message}</div>
        <button type="button" class="btn-close btn-close-white ms-3 me-2 m-auto"
                data-bs-dismiss="toast" aria-label="Fermer"></button>
    `;

    container.appendChild(toastEl);
    const toast = new bootstrap.Toast(toastEl, { delay: 4000 });
    toast.show();
    toastEl.addEventListener('hidden.bs.toast', () => toastEl.remove());
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
    fetch(API_BASE + 'health')
        .then(r => r.json())
        .then(d => {
            const el = document.getElementById('health-status');
            if (el) {
                el.textContent = `● ${d.status === 'healthy' ? 'En ligne' : 'Déconnecté'}`;
                el.className = 'navbar-text ms-auto text-' +
                    (d.status === 'healthy' ? 'success' : 'danger') + ' small';
            }
        })
        .catch(() => {
            const el = document.getElementById('health-status');
            if (el) {
                el.textContent = '● Déconnecté';
                el.className = 'navbar-text ms-auto text-danger small';
            }
        });
}

/* ------------------------------------------------------------------ */
/* DOM Ready
/* ------------------------------------------------------------------ */

document.addEventListener('DOMContentLoaded', () => {
    checkHealth();

    // Set default registry path
    const pathInput = document.getElementById('registryPath');
    if (pathInput && !pathInput.value.trim()) {
        pathInput.value = 'audit/codelist_duplicates.json';
    }
});
