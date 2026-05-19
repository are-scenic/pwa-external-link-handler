/*
 * MAIN-world interceptor for PWA External Link Handler.
 *
 * Runs in the page's own JS realm so it can patch window.open and
 * window.navigation.navigate. If the window is an installed-PWA window, this
 * script captures external-origin HTTP(S) navigations and forwards them to
 * the ISOLATED-world bridge via window.postMessage.
 *
 * This script is fail-open by design: the page shares the realm and can
 * replace our wrappers at will. The non-writable sentinel is idempotency
 * hardening, not a trust boundary.
 */

(() => {
    'use strict';

    // `picture-in-picture` is intentionally excluded — it indicates transient
    // Document-PiP state, not an installed-PWA window.
    const PWA_MODES = ['standalone', 'minimal-ui', 'fullscreen', 'window-controls-overlay', 'tabbed'];
    const SENTINEL = '__pwa_elh_main_installed__';
    const LOG_PREFIX = '[pwa-elh:main]';

    // Sub-frames inherit the top frame's display mode, but a PWA may host
    // cross-origin iframes whose realms we don't want to instrument.
    if (window.top !== window) return;

    if (!isInPwa()) return;

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

    function isInPwa() {
        try {
            return PWA_MODES.some(m => window.matchMedia(`(display-mode: ${m})`).matches);
        } catch (_) {
            return false;
        }
    }

    /**
     * Wraps window.open to redirect external HTTP(S) opens through the
     * bridge. Returns null (the spec-conformant popup-blocked shape) when the
     * call is intercepted. The bound reference to the original survives
     * page-level reassignment of window.open.
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
     * Wraps the Navigation API's navigate() for PWAs that route programmatic
     * navigations through it. No-op on Chromium versions without the API.
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
                        // Return a resolved no-op shape so awaiting callers
                        // don't hang.
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
     * Capture-phase click and auxclick listeners. Capture ensures we run
     * before the page's own bubble-phase handlers. auxclick is needed for
     * middle-click on anchors per the UI Events spec.
     */
    function installClickListeners() {
        document.addEventListener('click', onAnchorClick, true);
        document.addEventListener('auxclick', onAnchorClick, true);
    }

    function onAnchorClick(ev) {
        try {
            if (ev.defaultPrevented) return;
            // Left and middle clicks only; right-click context menu is the
            // user's own.
            if (ev.button !== 0 && ev.button !== 1) return;
            const anchor = ev.target?.closest?.('a[href]');
            if (!anchor) return;
            const url = anchor.href; // already absolutized by the DOM
            // <a href=""> and <a href="#frag"> resolve to a same-origin URL
            // via document.baseURI and are correctly rejected as same-origin
            // — they fall through to the page's own navigation.
            if (!isExternalHttp(url)) return;
            ev.preventDefault();
            ev.stopPropagation();
            // Deliberately NOT stopImmediatePropagation — other extensions'
            // capture-phase listeners (e.g., analytics) should still observe
            // the click.
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
     * True iff the URL parses as http/https AND its origin differs from the
     * current document's. Relative URLs are resolved against document.baseURI.
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
     * Posts a message to the ISOLATED-world bridge. Targets the document's
     * own origin so other windows in the same realm cannot eavesdrop. The
     * `__pwa_elh` sentinel lets the bridge cheaply filter unrelated traffic.
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

    function announcePwaActive() {
        postIntercept({ kind: 'pwa-active' });
    }
})();
