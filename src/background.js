/*
 * MV3 service worker for PWA External Link Handler.
 *
 * Single privileged endpoint. Receives validated messages from the
 * ISOLATED-world bridge and:
 *   - routes `open-external` to the native host via sendNativeMessage,
 *   - routes `pwa-active` to per-tab icon state,
 *   - enforces a per-tab 100ms native-host throttle (10 Hz cap) — design §3.3.4,
 *   - surfaces native-host failure via badge + tooltip + first-time options page.
 *
 * Holds no long-lived state; the per-tab maps are best-effort and reset on
 * worker termination. That's acceptable because they exist for rate-limiting,
 * not authorisation.
 */

const NATIVE_HOST_NAME = 'com.aaharonov.pwa_elh';
const LOG_PREFIX = '[pwa-elh:bg]';
const PER_TAB_THROTTLE_MS = 100;            // design §3.3.4 — 10 Hz/tab ceiling
const STORAGE_KEY_BROWSER = 'browserBinary';
const STORAGE_KEY_HOST_NOTICED = 'hostMissingNoticedOnce';
const STORAGE_KEY_LAST_ERROR = 'lastNativeHostError';
// Fixed tooltip per design §3.3.6 — must not be concatenated with diagnostic
// text. Detailed error info is surfaced via the options page instead.
const FAILURE_TOOLTIP = 'Native host not installed — click for help';

const ICONS_INACTIVE = {
    16: 'icons/inactive-16.png',
    32: 'icons/inactive-32.png',
    48: 'icons/inactive-48.png',
    128: 'icons/inactive-128.png'
};
const ICONS_ACTIVE = {
    16: 'icons/active-16.png',
    32: 'icons/active-32.png',
    48: 'icons/active-48.png',
    128: 'icons/active-128.png'
};

/** @type {Map<number, number>} tabId → last dispatch timestamp ms */
const lastDispatchByTab = new Map();

// --- Lifecycle -------------------------------------------------------------

chrome.runtime.onInstalled.addListener((details) => {
    try {
        chrome.action.setIcon({ path: ICONS_INACTIVE }, () => {
            // Drain lastError; setIcon without tabId sets the global default.
            void chrome.runtime.lastError;
        });
        chrome.action.setTitle({ title: 'PWA External Link Handler (inactive — not a PWA tab)' });
    } catch (e) {
        console.warn(LOG_PREFIX, 'onInstalled icon set failed', e);
    }
});

chrome.action.onClicked.addListener(() => {
    // The action serves as a status indicator; clicking it opens the options
    // page for diagnostics, regardless of state. Failure-mode users land here
    // automatically too — see notifyUserFailure.
    openOptionsPage();
});

// --- Message routing -------------------------------------------------------

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
    // Only accept messages from this extension's own content scripts.
    if (sender.id !== chrome.runtime.id) return false;
    // sender.tab presence implies the sender is a content script with a tab —
    // options-page traffic does not carry sender.tab and is excluded from
    // this channel.
    if (!sender.tab) return false;
    if (!msg || typeof msg !== 'object') return false;

    if (msg.kind === 'open-external') {
        handleOpenExternal(msg, sender)
            .then((ok) => { try { sendResponse({ ok }); } catch (_) {} })
            .catch((e) => {
                console.warn(LOG_PREFIX, 'handleOpenExternal rejected', e);
                try { sendResponse({ ok: false }); } catch (_) {}
            });
        return true; // async response
    }

    if (msg.kind === 'pwa-active') {
        setActiveIcon(sender.tab.id);
        return false;
    }

    return false;
});

// --- open-external pipeline ------------------------------------------------

/**
 * Validates, throttles, and dispatches the open-external request to the
 * native host. Returns `true` if the request was dispatched and the host
 * acknowledged success; `false` otherwise (including throttled).
 */
async function handleOpenExternal(msg, sender) {
    const url = typeof msg.url === 'string' ? msg.url : '';
    if (!isValidExternalUrl(url)) {
        console.warn(LOG_PREFIX, 'rejecting malformed url');
        return false;
    }

    const tabId = sender.tab.id;
    if (isThrottled(tabId)) {
        // Silent drop — a malicious page in a tight window.open loop is the
        // dominant case here; we deliberately do not surface anything.
        return false;
    }
    markDispatched(tabId);

    let browserOverride = null;
    try {
        const stored = await chrome.storage.local.get([STORAGE_KEY_BROWSER]);
        const raw = typeof stored[STORAGE_KEY_BROWSER] === 'string'
            ? stored[STORAGE_KEY_BROWSER].trim()
            : '';
        browserOverride = raw.length > 0 ? raw : null;
    } catch (e) {
        console.warn(LOG_PREFIX, 'storage.get failed; using default browser', e);
    }

    const request = { url, browser_override: browserOverride };

    try {
        const reply = await chrome.runtime.sendNativeMessage(NATIVE_HOST_NAME, request);
        if (!reply || reply.ok !== true) {
            const errText = (reply && typeof reply.error === 'string') ? reply.error : 'host returned not-ok';
            await notifyUserFailure(tabId, errText);
            return false;
        }
        return true;
    } catch (e) {
        const errText = String(e?.message ?? e);
        console.warn(LOG_PREFIX, 'sendNativeMessage threw', errText);
        await notifyUserFailure(tabId, errText);
        return false;
    }
}

function isValidExternalUrl(url) {
    if (typeof url !== 'string' || url.length === 0 || url.length > 8192) return false;
    try {
        const u = new URL(url);
        return u.protocol === 'http:' || u.protocol === 'https:';
    } catch (_) {
        return false;
    }
}

function isThrottled(tabId) {
    const prev = lastDispatchByTab.get(tabId);
    if (prev === undefined) return false;
    return (Date.now() - prev) < PER_TAB_THROTTLE_MS;
}

function markDispatched(tabId) {
    // Map.set on an existing key does NOT move the key to insertion-most-
    // recent. Delete first, then set — that way the bounded-size GC below
    // evicts the true LRU tab rather than the oldest-inserted tab (which is
    // typically a long-lived tab the user is still actively using).
    lastDispatchByTab.delete(tabId);
    lastDispatchByTab.set(tabId, Date.now());
    if (lastDispatchByTab.size > 256) {
        const oldestKey = lastDispatchByTab.keys().next().value;
        if (oldestKey !== undefined) lastDispatchByTab.delete(oldestKey);
    }
}

// --- Failure surface -------------------------------------------------------

/**
 * Sets a red "!" badge with diagnostic tooltip on the action; on the first
 * encounter per install also opens the options page so the user notices when
 * the PWA toolbar is hidden (design §3.3.6).
 */
async function notifyUserFailure(tabId, errText) {
    try {
        await chrome.action.setBadgeText({ tabId, text: '!' });
        await chrome.action.setBadgeBackgroundColor({ tabId, color: '#c0392b' });
        // Tooltip is the fixed design §3.3.6 string. The diagnostic detail
        // (errText) is stashed in storage so the options page can render it.
        await chrome.action.setTitle({ tabId, title: FAILURE_TOOLTIP });
    } catch (e) {
        console.warn(LOG_PREFIX, 'setting badge/title failed', e);
    }

    // Persist diagnostic detail for the options page to render in its status
    // area. Keeps the tooltip clean while preserving diagnosability.
    try {
        const detail = typeof errText === 'string' ? errText : String(errText ?? '');
        await chrome.storage.local.set({
            [STORAGE_KEY_LAST_ERROR]: { message: detail, ts: Date.now() }
        });
    } catch (e) {
        console.warn(LOG_PREFIX, 'persisting lastNativeHostError failed', e);
    }

    // First-failure: open options page (design §3.3.6 step 3).
    try {
        const flags = await chrome.storage.local.get([STORAGE_KEY_HOST_NOTICED]);
        if (!flags[STORAGE_KEY_HOST_NOTICED]) {
            await chrome.storage.local.set({ [STORAGE_KEY_HOST_NOTICED]: true });
            openOptionsPage();
        }
    } catch (e) {
        console.warn(LOG_PREFIX, 'first-failure flag handling failed', e);
    }
}

function openOptionsPage() {
    try {
        // `openOptionsPage` honours `options_ui.open_in_tab: true` from the
        // manifest, which we want — the options page is the diagnostics path
        // and must be visible even when invoked from a PWA window.
        chrome.runtime.openOptionsPage(() => {
            void chrome.runtime.lastError;
        });
    } catch (e) {
        console.warn(LOG_PREFIX, 'openOptionsPage failed', e);
    }
}

// --- Per-tab icon state ----------------------------------------------------

/**
 * Switches the action icon to the "active" variant for the given tab and
 * clears any stale error badge. Design §3.3.5: per-tab; reverts naturally on
 * tab close. We do not proactively reset on intra-tab navigation (§9.A4).
 */
async function setActiveIcon(tabId) {
    try {
        await chrome.action.setIcon({ tabId, path: ICONS_ACTIVE });
        await chrome.action.setTitle({ tabId, title: 'PWA External Link Handler — active' });
        await chrome.action.setBadgeText({ tabId, text: '' });
    } catch (e) {
        console.warn(LOG_PREFIX, 'setActiveIcon failed', e);
    }
}
