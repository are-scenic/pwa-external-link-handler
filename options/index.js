/*
 * Options page controller for PWA External Link Handler.
 *
 * Wires the Save / Clear / Re-check controls to chrome.storage.local and a
 * direct probe of the native host. No inline event handlers (the
 * extension-pages CSP forbids them) and no external dependencies.
 */

'use strict';

const NATIVE_HOST_NAME = 'com.aaharonov.pwa_elh';
const STORAGE_KEY_BROWSER = 'browserBinary';
const STORAGE_KEY_LAST_ERROR = 'lastNativeHostError';
const LOG_PREFIX = '[pwa-elh:options]';
const STATUS_CLEAR_MS = 2000;

/** @type {HTMLInputElement | null} */ let inputEl = null;
/** @type {HTMLButtonElement | null} */ let saveBtn = null;
/** @type {HTMLButtonElement | null} */ let clearBtn = null;
/** @type {HTMLButtonElement | null} */ let recheckBtn = null;
/** @type {HTMLElement | null}        */ let saveStatusEl = null;
/** @type {HTMLElement | null}        */ let hostStatusEl = null;
/** @type {HTMLElement | null}        */ let hostErrorDetailEl = null;

document.addEventListener('DOMContentLoaded', () => {
    inputEl           = /** @type {HTMLInputElement} */ (document.getElementById('browser-binary'));
    saveBtn           = /** @type {HTMLButtonElement} */ (document.getElementById('save'));
    clearBtn          = /** @type {HTMLButtonElement} */ (document.getElementById('clear'));
    recheckBtn        = /** @type {HTMLButtonElement} */ (document.getElementById('recheck'));
    saveStatusEl      = document.getElementById('save-status');
    hostStatusEl      = document.getElementById('host-status');
    hostErrorDetailEl = document.getElementById('host-error-detail');

    loadStoredValue();
    renderLastNativeHostError();
    saveBtn?.addEventListener('click', onSaveClicked);
    clearBtn?.addEventListener('click', onClearClicked);
    recheckBtn?.addEventListener('click', probeHostStatus);
    probeHostStatus();
});

async function loadStoredValue() {
    try {
        const got = await chrome.storage.local.get([STORAGE_KEY_BROWSER]);
        const value = typeof got[STORAGE_KEY_BROWSER] === 'string' ? got[STORAGE_KEY_BROWSER] : '';
        if (inputEl) inputEl.value = value;
    } catch (e) {
        console.warn(LOG_PREFIX, 'storage.get failed', e);
    }
}

async function onSaveClicked() {
    const raw = inputEl?.value ?? '';
    const trimmed = raw.trim();
    try {
        await chrome.storage.local.set({ [STORAGE_KEY_BROWSER]: trimmed });
        setSaveStatus(trimmed.length === 0
            ? 'Saved (using OS default browser).'
            : 'Saved.', 'ok');
    } catch (e) {
        console.warn(LOG_PREFIX, 'storage.set failed', e);
        setSaveStatus('Save failed — see browser console.', 'error');
    }
}

async function onClearClicked() {
    try {
        await chrome.storage.local.remove([STORAGE_KEY_BROWSER]);
        if (inputEl) inputEl.value = '';
        setSaveStatus('Cleared — using OS default browser.', 'ok');
    } catch (e) {
        console.warn(LOG_PREFIX, 'storage.remove failed', e);
        setSaveStatus('Clear failed — see browser console.', 'error');
    }
}

function setSaveStatus(text, kind) {
    if (!saveStatusEl) return;
    saveStatusEl.textContent = text;
    saveStatusEl.classList.remove('ok', 'error', 'pending');
    if (kind) saveStatusEl.classList.add(kind);
    // Auto-clear non-error messages after a couple of seconds; errors
    // persist so the user has time to read them.
    if (text && kind !== 'error') {
        setTimeout(() => {
            if (saveStatusEl?.textContent === text) {
                saveStatusEl.textContent = '';
                saveStatusEl.classList.remove('ok', 'error', 'pending');
            }
        }, STATUS_CLEAR_MS);
    }
}

/**
 * Probes the native host with `{ url: 'about:blank', probe: true }`. The
 * host treats this as a no-spawn health check and replies
 * `{ ok: true, probe: true }`.
 */
async function probeHostStatus() {
    setHostStatus('Checking…', 'pending');
    const request = { url: 'about:blank', browser_override: null, probe: true };
    try {
        const reply = await chrome.runtime.sendNativeMessage(NATIVE_HOST_NAME, request);
        if (reply && reply.ok === true) {
            setHostStatus('Native host installed and reachable.', 'ok');
            clearHostErrorDetail();
            // A successful probe clears the persisted last-error record so
            // it doesn't linger after the underlying issue is fixed.
            try { await chrome.storage.local.remove([STORAGE_KEY_LAST_ERROR]); } catch (_) {}
        } else {
            const errText = (reply && typeof reply.error === 'string') ? reply.error : 'host returned not-ok';
            setHostStatus(`Native host responded with an error: ${errText}`, 'error');
        }
    } catch (e) {
        const errText = String(e?.message ?? e);
        // Typical: "Specified native messaging host not found."
        setHostStatus(`Native host not reachable — ${errText}`, 'error');
    }
}

function setHostStatus(text, kind) {
    if (!hostStatusEl) return;
    hostStatusEl.textContent = text;
    hostStatusEl.classList.remove('ok', 'error', 'pending');
    if (kind) hostStatusEl.classList.add(kind);
}

/**
 * Surfaces the most recent native-host error recorded by the service
 * worker. The SW writes diagnostic detail to chrome.storage.local because
 * the action tooltip is a fixed user-facing string and cannot carry it.
 */
async function renderLastNativeHostError() {
    try {
        const got = await chrome.storage.local.get([STORAGE_KEY_LAST_ERROR]);
        const rec = got[STORAGE_KEY_LAST_ERROR];
        if (rec && typeof rec === 'object'
                && typeof rec.message === 'string'
                && rec.message.length > 0) {
            const ts = typeof rec.ts === 'number'
                ? new Date(rec.ts).toLocaleString()
                : 'unknown time';
            showHostErrorDetail(`Last error (${ts}): ${rec.message}`);
        } else {
            clearHostErrorDetail();
        }
    } catch (e) {
        console.warn(LOG_PREFIX, 'reading lastNativeHostError failed', e);
    }
}

function showHostErrorDetail(text) {
    if (!hostErrorDetailEl) return;
    hostErrorDetailEl.textContent = text;
    hostErrorDetailEl.hidden = false;
}

function clearHostErrorDetail() {
    if (!hostErrorDetailEl) return;
    hostErrorDetailEl.textContent = '';
    hostErrorDetailEl.hidden = true;
}
