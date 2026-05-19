# PWA External Link Handler

A Manifest V3 Chromium extension that re-routes external-origin link clicks
from installed PWA windows to the user's default browser. Inside a regular
browser tab the extension is inert; inside a PWA window it intercepts
external `<a>` clicks, `window.open()`, and `window.navigation.navigate()`
calls and forwards the URL to a small Python native messaging host that
invokes the OS URL launcher (`xdg-open` / `open` / `start`).

The browser-side component lives in this directory. The Python native host
is published separately under `native-host/`.

## Directory layout

```
.
├── manifest.json            MV3 manifest, CSP, content-script declarations
├── src/
│   ├── content-main.js      MAIN-world interceptor (window.open + click)
│   ├── content-bridge.js    ISOLATED-world bridge (validation + dispatch)
│   └── background.js        Service worker: native-host invocation + icon
├── options/
│   ├── index.html           Options page (browser override, host status)
│   └── index.js             Options page controller
├── icons/                   Placeholder PNGs (active + inactive, 16/32/48/128)
├── updates.xml              Enterprise update manifest template
├── LICENSE                  Apache-2.0
└── README.md                (this file)
```

## Load the extension unpacked (for testing)

1. Open `chrome://extensions` (or `edge://extensions`).
2. Enable **Developer mode**.
3. Click **Load unpacked** and select this directory
   (`/home/aaharonov/pwa-external-link-handler/`).
4. Install the native messaging host separately (see `native-host/`).
5. Open an installed PWA and click an external link — it should open in the
   default browser instead of inside the PWA window. The toolbar icon shows
   active (blue) inside PWA tabs and inactive (grey) elsewhere.

## Design documents

Authoritative sources, in `Tools/Browser/Contributions/` of the project's
Obsidian vault:

- `2026-05-13-pwa-design-draft.md` — primary architecture (v0.3.2; Phase 1 closed)
- `2026-05-13-security-research-findings.md` — CSP & hardening guidance
- `2026-05-13-research-blocking-questions.md` — resolved blocking questions

## Browser compatibility

`minimum_chrome_version` is **116**. The limiting factor is Promise-style
`chrome.runtime.sendNativeMessage` (Chrome 116+). Other APIs in use bottom
out lower (`world: "MAIN"` is Chrome 111+; MV3 service workers are Chrome
88+), so 116 is the effective floor. Edge tracks Chromium, so the same
floor applies. If a Chromium fork lags behind, raise this value rather
than shimming the API.

## Notes for maintainers

- All code is plain JavaScript. No build step, no bundler, no transpiler.
- CSP forbids inline scripts, `eval`, `unsafe-inline`, and `unsafe-eval`.
  Inline `<style>` is likewise disallowed — the options page uses an
  external `index.css`.
- The MAIN/ISOLATED split is load-bearing: MAIN-world is required to wrap
  page-realm `window.open`; ISOLATED-world is required to call privileged
  `chrome.*` APIs.
- `externally_connectable` is declared with empty `matches` and `ids` to
  make the closed-channel guarantee explicit: no web page and no other
  extension can open a port to this extension via `chrome.runtime.connect`.

## Distribution / enterprise deployment

- **Licence:** Apache-2.0 (see `LICENSE`). Per design §10.Q1 the explicit
  patent grant is favoured over MIT for enterprise adoption.
- **Edge dual-ID:** The Chrome Web Store and Microsoft Edge Add-ons each
  generate an **independent** extension ID for the same codebase. The
  native-host manifest's `allowed_origins` must list both IDs once each
  store has assigned one (see design §7.7). The pinned `key` field in
  `manifest.json` fixes the Chrome ID across rebuilds; no equivalent
  mechanism exists for Edge — capture the Edge ID after first publication
  and update the host manifest.
- **Enterprise force-install via `update_url`:** The `update_url` value in
  `manifest.json` currently points at a placeholder GitHub Pages URL —
  confirm it before publishing. The corresponding `updates.xml` template
  ships in the repo root and is published to `gh-pages` per release. The
  `appid`, `codebase`, and `hash_sha256` fields in `updates.xml` are TODO
  placeholders that the release pipeline must fill in. Chrome enterprise
  policies (Linux / macOS JSON example) reference the Chrome extension ID
  and the same `update_url`:

  ```json
  {
    "ExtensionInstallForcelist": [
      "<EXTENSION_ID>;https://aaharonov.github.io/pwa-elh/updates.xml"
    ]
  }
  ```

  Edge enterprise force-install uses the
  `Microsoft\Edge\ExtensionInstallForcelist` policy namespace and the
  Edge-assigned extension ID; deferred for v1 per design §8.D12.
- Never commit the private CRX signing key. Loss of the private key breaks
  updates for all existing enterprise installs (see design §7.7).

## Licence

Apache-2.0 — see `LICENSE`.
