/*
 * MAIN-world interceptor content script for PWA External Link Handler.
 *
 * Runs in the page's own JS realm so it can patch `window.open` and
 * `window.navigation.navigate`. Detects PWA display modes; if the window is a
 * PWA, installs interceptors that capture external-origin HTTP(S) navigations
 * and forwards them to the ISOLATED-world bridge via `window.postMessage`.
 *
 * Security posture: this script is fail-OPEN by design. The page shares the
 * realm and could replace our wrappers at will; the non-writable sentinel is
 * idempotency hardening only, not a trust boundary. See design §3.1, §7.4.
 */

(() => {
    'use strict';

    // Forward-compatible set per design §3.1.3 / research Q1.
    // `picture-in-picture` is intentionally excluded — it indicates transient
    // Document-PiP state, not an installed-PWA window.
    const PWA_MODES = ['standalone', 'minimal-ui', 'fullscreen', 'window-controls-overlay', 'tabbed'];
    const SENTINEL = '__pwa_elh_main_installed__';
    const LOG_PREFIX = '[pwa-elh:main]';

    // Only run in the top frame. Sub-frames inherit the top frame's display
    // mode, but a PWA may host cross-origin iframes whose realms we don't want
    // to instrument. See design §3.1.3.
    if (window.top !== window) return;

    if (!isInPwa()) return;

    // Idempotency hardening — prevents double-installation if the script is
    // somehow loaded twice. Not a security boundary.
    if (window[SENTINEL]) return;
    try {
        Object.defineProperty(window, SENTINEL, {
            value: true,
            configurable: false,
            writable: false,
            enumerable: false
        });
    } catch (e) {
        console.warn(LOG_PREFIX, 'sentinel install failed; aborting', e);
        return;
    }

    installWindowOpenProxy();
    installNavigationApiProxy();
    installClickListeners();
    announcePwaActive();

    /**
     * Returns true when the current window matches any installed-PWA display
     * mode. Runs once at script start; we do not subscribe to changes (§8.D7).
     */
    function isInPwa() {
        try {
            return PWA_MODES.some(m => window.matchMedia(`(display-mode: ${m})`).matches);
        } catch (_) {
            return false;
        }
    }

    /**
     * Wraps `window.open` to redirect external HTTP(S) opens through the
     * bridge. Returns `null` (spec-conformant popup-blocked shape) to the
     * caller in that case. Keeps a bound reference to the original to survive
     * page-level reassignment.
     */
    function installWindowOpenProxy() {
        try {
            const originalOpen = window.open.bind(window);
            window.open = function patchedOpen(url, target, features) {
                try {
                    if (typeof url === 'string' && isExternalHttp(url)) {
                        postIntercept({
                            kind: 'open-external',
                            payload: { url: absolutize(url), source: 'window.open' }
                        });
                        return null;
                    }
                } catch (e) {
                    console.warn(LOG_PREFIX, 'window.open proxy threw; falling through', e);
                }
                return originalOpen(url, target, features);
            };
        } catch (e) {
            console.warn(LOG_PREFIX, 'window.open proxy install failed', e);
        }
    }

    /**
     * Wraps the Navigation API's `navigate` for modern PWAs that route
     * programmatic navigations through it. Feature-gated; no-op on older
     * Chromium.
     */
    function installNavigationApiProxy() {
        try {
            if (typeof window.navigation?.navigate !== 'function') return;
            const originalNavigate = window.navigation.navigate.bind(window.navigation);
            window.navigation.navigate = function patchedNavigate(url, options) {
                try {
                    if (typeof url === 'string' && isExternalHttp(url)) {
                        postIntercept({
                            kind: 'open-external',
                            payload: { url: absolutize(url), source: 'navigation.navigate' }
                        });
                        // Return a no-op shape so awaiting callers don't hang.
                        return {
                            committed: Promise.resolve(),
                            finished: Promise.resolve()
                        };
                    }
                } catch (e) {
                    console.warn(LOG_PREFIX, 'navigation.navigate proxy threw; falling through', e);
                }
                return originalNavigate(url, options);
            };
        } catch (e) {
            console.warn(LOG_PREFIX, 'navigation.navigate proxy install failed', e);
        }
    }

    /**
     * Capture-phase click + auxclick listeners. Capture phase ensures we run
     * before the page's bubble-phase handlers. `auxclick` covers middle-click
     * on anchors per UI Events spec.
     */
    function installClickListeners() {
        document.addEventListener('click', onAnchorClick, true);
        document.addEventListener('auxclick', onAnchorClick, true);
    }

    function onAnchorClick(ev) {
        try {
            if (ev.defaultPrevented) return;
            // Left and middle clicks only — right-click context menu is the
            // user's own.
            if (ev.button !== 0 && ev.button !== 1) return;
            const anchor = ev.target?.closest?.('a[href]');
            if (!anchor) return;
            const url = anchor.href; // already absolutized by the DOM
            // Note: `<a href="">` and `<a href="#frag">` absolutise to a
            // same-origin URL via document.baseURI and are correctly rejected
            // by isExternalHttp() as same-origin — they fall through to the
            // page's own navigation.
            if (!isExternalHttp(url)) return;
            ev.preventDefault();
            ev.stopPropagation();
            // We intentionally do NOT stopImmediatePropagation — other
            // extensions' capture-phase listeners (e.g., analytics) should
            // still observe the click.
            postIntercept({
                kind: 'open-external',
                payload: { url, source: 'anchor-click', modifiers: pickModifiers(ev) }
            });
        } catch (e) {
            console.warn(LOG_PREFIX, 'click handler threw', e);
        }
    }

    function pickModifiers(ev) {
        return {
            ctrl: !!ev.ctrlKey,
            meta: !!ev.metaKey,
            shift: !!ev.shiftKey,
            alt: !!ev.altKey,
            button: ev.button
        };
    }

    /**
     * Returns true iff the given URL parses as http/https AND has an origin
     * distinct from the current document's origin. Resolves relative URLs
     * against `document.baseURI` so anchor-relative hrefs are handled.
     */
    function isExternalHttp(url) {
        try {
            const u = new URL(url, document.baseURI);
            if (u.protocol !== 'http:' && u.protocol !== 'https:') return false;
            return u.origin !== window.location.origin;
        } catch (_) {
            return false;
        }
    }

    function absolutize(url) {
        try {
            return new URL(url, document.baseURI).toString();
        } catch (_) {
            return url;
        }
    }

    /**
     * Sends a message to the ISOLATED-world bridge. Always targets the own
     * origin so other windows in the same realm cannot eavesdrop. Includes a
     * sentinel so the bridge can cheaply filter unrelated messages.
     */
    function postIntercept(body) {
        try {
            window.postMessage(
                { __pwa_elh: true, ...body },
                window.location.origin
            );
        } catch (e) {
            console.warn(LOG_PREFIX, 'postMessage failed', e);
        }
    }

    /**
     * One-shot signal to the bridge that this window is a PWA. The bridge
     * forwards it to the service worker, which switches the action icon to
     * the active state for the tab.
     */
    function announcePwaActive() {
        postIntercept({ kind: 'pwa-active' });
    }
})();
