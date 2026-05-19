/*
 * ISOLATED-world bridge content script for PWA External Link Handler.
 *
 * The sole crossing between page-realm MAIN-world messages and the
 * extension's privileged service worker. Validates message shape, enforces a
 * scheme allow-list and URL length cap, deduplicates rapid identical events,
 * and forwards to the background via `chrome.runtime.sendMessage`.
 *
 * Pages can spoof MAIN-world messages — this is accepted by design (§7.4)
 * because the resulting capability is "open http(s) URL via OS launcher,"
 * which is the page's own existing capability via <a target>. Validation here
 * is defence-in-depth, not a trust boundary.
 */

(() => {
    'use strict';

    const LOG_PREFIX = '[pwa-elh:bridge]';
    const URL_MAX_LENGTH = 8192;
    const DEDUP_WINDOW_MS = 250;
    const DEDUP_MAX_ENTRIES = 64;

    // Mirror of content-main.js's PWA mode set — intentionally duplicated.
    // Content scripts cannot `import`, and the duplication is one constant +
    // one 4-line function. Keep these two definitions in lock-step.
    const PWA_MODES = ['standalone', 'minimal-ui', 'fullscreen', 'window-controls-overlay', 'tabbed'];

    /** Cheap PWA display-mode check; identical to content-main.js. */
    function isInPwa() {
        try {
            return PWA_MODES.some(m => window.matchMedia(`(display-mode: ${m})`).matches);
        } catch (_) {
            return false;
        }
    }

    /** @type {Map<string, number>} key = `${kind}|${url}` → last-seen ms */
    const recent = new Map();

    // Top-level gate: outside a PWA window the bridge is inert. Cheap-and-
    // cheerful filter; the per-message check below is the load-bearing one
    // (top-level state could in theory be wrong if display-mode changed mid-
    // page, though we do not subscribe to changes — see design §8.D7).
    if (!isInPwa()) return;

    window.addEventListener('message', onMessage, false);

    function onMessage(ev) {
        // Only accept messages from this same window. Cross-window message
        // events are dropped silently.
        if (ev.source !== window) return;
        if (ev.origin !== window.location.origin) return;

        const d = ev.data;
        if (!d || typeof d !== 'object') return;
        if (d.__pwa_elh !== true) return;

        // Per-message PWA re-check (load-bearing). Without this, a page in a
        // regular tab could post { __pwa_elh: true, kind: 'pwa-active' } and
        // drive the toolbar icon into the active state. The top-level gate
        // above already suppresses the listener install outside PWAs; this
        // check is defence in depth.
        if (!isInPwa()) return;

        try {
            if (d.kind === 'open-external') {
                handleOpenExternal(d);
            } else if (d.kind === 'pwa-active') {
                handlePwaActive();
            }
            // Unknown kinds are silently ignored — forward-compat with future
            // MAIN-world additions.
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
        // Dedup-key the pwa-active announcement on the page's own origin —
        // multiple identical announcements from re-runs of the MAIN script
        // are suppressed inside the 250ms window.
        if (!shouldDispatch('pwa-active', window.location.origin)) return;
        sendToBackground({ kind: 'pwa-active' });
    }

    /**
     * Validates URL: must be a string under URL_MAX_LENGTH that parses as a
     * URL with http/https scheme. Returns the normalised string form on
     * success, null otherwise. The host re-validates independently (defence
     * in depth).
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
     * Best-effort 250ms-window dedup for `(kind, url)` pairs. Bounds map size
     * by evicting the oldest entry once it exceeds DEDUP_MAX_ENTRIES; this is
     * a sliding-FIFO approximation (Map iteration order is insertion order),
     * which suffices because the window we care about is small.
     */
    function shouldDispatch(kind, url) {
        const key = `${kind}|${url}`;
        const now = Date.now();
        const prev = recent.get(key);
        if (prev !== undefined && now - prev < DEDUP_WINDOW_MS) return false;
        // Map.set on an existing key does NOT move the key to insertion-most-
        // recent. Delete first, then set — that way the bounded-size GC below
        // evicts the true LRU entry rather than the oldest-inserted one.
        recent.delete(key);
        recent.set(key, now);
        if (recent.size > DEDUP_MAX_ENTRIES) {
            const oldestKey = recent.keys().next().value;
            if (oldestKey !== undefined) recent.delete(oldestKey);
        }
        return true;
    }

    /**
     * Privileged hand-off to the service worker. Errors are logged and
     * swallowed — there is no user-visible feedback path from here; the
     * service worker handles user-visible failures of its own native-host
     * invocation.
     */
    function sendToBackground(payload) {
        try {
            // Callback-form (not Promise-form): the callback fires after the
            // SW has actually woken and processed the message, giving correct
            // lastError surfacing. The Promise form has historically been
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
