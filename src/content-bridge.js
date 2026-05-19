/*
 * ISOLATED-world bridge for PWA External Link Handler.
 *
 * The only crossing between page-realm MAIN-world messages and the
 * extension's privileged service worker. Validates message shape, enforces a
 * scheme allow-list and URL length cap, deduplicates rapid identical events,
 * and forwards to the background via chrome.runtime.sendMessage.
 *
 * Pages can spoof MAIN-world messages — that is accepted, because the
 * resulting capability is "open an http(s) URL via the OS launcher," which
 * is the page's own existing capability via <a target>. Validation here is
 * defence in depth, not a trust boundary.
 */

(() => {
    'use strict';

    const LOG_PREFIX = '[pwa-elh:bridge]';
    const URL_MAX_LENGTH = 8192;
    const DEDUP_WINDOW_MS = 250;
    const DEDUP_MAX_ENTRIES = 64;

    // Mirror of content-main.js's PWA mode set. Content scripts cannot
    // `import`, so this constant is duplicated intentionally — keep the two
    // definitions in lock-step.
    const PWA_MODES = ['standalone', 'minimal-ui', 'fullscreen', 'window-controls-overlay', 'tabbed'];

    function isInPwa() {
        try {
            return PWA_MODES.some(m => window.matchMedia(`(display-mode: ${m})`).matches);
        } catch (_) {
            return false;
        }
    }

    /** @type {Map<string, number>} key = `${kind}|${url}` → last-seen ms */
    const recent = new Map();

    // Outside a PWA window the bridge is inert. The per-message check below
    // is the load-bearing one; this top-level gate is just a cheap filter.
    if (!isInPwa()) return;

    window.addEventListener('message', onMessage, false);

    function onMessage(ev) {
        if (ev.source !== window) return;
        if (ev.origin !== window.location.origin) return;

        const d = ev.data;
        if (!d || typeof d !== 'object') return;
        if (d.__pwa_elh !== true) return;

        // Per-message PWA re-check. Without it, a page in a regular tab
        // could post { __pwa_elh: true, kind: 'pwa-active' } and drive the
        // toolbar icon into the active state.
        if (!isInPwa()) return;

        try {
            if (d.kind === 'open-external') {
                handleOpenExternal(d);
            } else if (d.kind === 'pwa-active') {
                handlePwaActive();
            }
            // Unknown kinds are silently ignored for forward-compat.
        } catch (e) {
            console.warn(LOG_PREFIX, 'message handler threw', e);
        }
    }

    function handleOpenExternal(message) {
        const rawUrl = message.payload?.url;
        const url = sanitizeUrl(rawUrl);
        if (!url) return;
        if (!shouldDispatch('open-external', url)) return;
        const source = typeof message.payload?.source === 'string'
            ? message.payload.source
            : 'unknown';
        sendToBackground({ kind: 'open-external', url, source });
    }

    function handlePwaActive() {
        // Key the dedup on the page's own origin so multiple identical
        // announcements from re-runs of the MAIN script collapse inside the
        // 250ms window.
        if (!shouldDispatch('pwa-active', window.location.origin)) return;
        sendToBackground({ kind: 'pwa-active' });
    }

    /**
     * Returns the normalized URL string when `raw` is an http(s) URL under
     * URL_MAX_LENGTH; null otherwise. The host re-validates independently.
     */
    function sanitizeUrl(raw) {
        if (typeof raw !== 'string') return null;
        if (raw.length === 0 || raw.length > URL_MAX_LENGTH) return null;
        try {
            const u = new URL(raw);
            if (u.protocol !== 'http:' && u.protocol !== 'https:') return null;
            return u.toString();
        } catch (_) {
            return null;
        }
    }

    /**
     * Best-effort 250ms-window dedup for (kind, url) pairs. The map size is
     * bounded by evicting the oldest entry once it exceeds DEDUP_MAX_ENTRIES.
     * Map iteration order is insertion order, which gives a sliding-FIFO
     * approximation; that suffices given the small time window.
     */
    function shouldDispatch(kind, url) {
        const key = `${kind}|${url}`;
        const now = Date.now();
        const prev = recent.get(key);
        if (prev !== undefined && now - prev < DEDUP_WINDOW_MS) return false;
        // Map.set on an existing key does NOT move it to insertion-most-
        // recent. Delete first, then set, so the bounded-size GC below
        // evicts the true LRU entry rather than the oldest-inserted one.
        recent.delete(key);
        recent.set(key, now);
        if (recent.size > DEDUP_MAX_ENTRIES) {
            const oldestKey = recent.keys().next().value;
            if (oldestKey !== undefined) recent.delete(oldestKey);
        }
        return true;
    }

    function sendToBackground(payload) {
        try {
            // Callback form (not Promise form): the callback fires after the
            // service worker has actually woken and processed the message,
            // giving correct lastError surfacing. The Promise form has been
            // flaky during SW cold-wake in some Chromium builds.
            chrome.runtime.sendMessage(payload, () => {
                // Drain lastError to suppress the "unchecked runtime.lastError"
                // console noise when the SW is asleep or the receiver is gone.
                const err = chrome.runtime.lastError;
                if (err) {
                    console.warn(LOG_PREFIX, 'sendMessage reported', err.message || err);
                }
            });
        } catch (e) {
            console.warn(LOG_PREFIX, 'sendMessage threw', e);
        }
    }
})();
