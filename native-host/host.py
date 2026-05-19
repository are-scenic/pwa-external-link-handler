#!/usr/bin/env python3
"""PWA External Link Handler — Chrome Native Messaging Host.

This module implements the Chromium Native Messaging protocol host for the
PWA External Link Handler browser extension. It reads a single
length-prefixed JSON request from stdin, opens the requested HTTP(S) URL
via the OS default-browser launcher (or an optionally configured browser
binary), writes a single length-prefixed JSON response to stdout, and
exits.

Protocol
--------
Chrome Native Messaging wire format on stdio:

* Each message is preceded by a 4-byte little-endian unsigned 32-bit length
  header indicating the size of the UTF-8 encoded JSON payload that follows.
* Extension -> host messages are capped by Chromium at 64 MiB per message.
  We enforce a conservative ``MAX_REQUEST_BYTES`` (1 MiB) cap.
* Host -> extension messages are capped by Chromium at 1 MiB per message.
  Our responses are tiny (``{"ok": true}`` shape); we enforce a 64 KiB cap
  defensively (``MAX_RESPONSE_BYTES``).

Probe contract (per design §3.5.4)
----------------------------------
The options page may send ``{"url": "about:blank", "browser_override": null,
"probe": true}`` to verify the host is reachable. The host answers
``{"ok": true, "probe": true}`` *without spawning anything*. Probe handling
is taken **only** when both ``probe === true`` AND ``url === "about:blank"``
— any other URL combined with ``probe: true`` is rejected as malformed.
The probe path is not a covert no-spawn channel for arbitrary URLs.

Security model
--------------
* The host re-validates the URL even though the extension already did.
  Only ``http://`` and ``https://`` URLs are accepted, with a non-empty
  network location and a maximum length of ``MAX_URL`` characters.
* On Windows, ``os.startfile(url, "open")`` is used instead of
  ``subprocess.Popen(['cmd', '/c', 'start', ...])`` to avoid ``cmd.exe``
  metacharacter reparsing (``&``, ``%``, ``^``, ``|``, etc.) — which would
  otherwise mangle every URL containing a query string.
* On POSIX, ``subprocess.Popen`` is used with a list-form ``argv``,
  ``shell=False`` (default), and ``start_new_session=True`` so the child
  survives the host's immediate exit.
* The optional ``browser_override`` must be an **absolute path** to an
  existing, executable regular file.

Logging
-------
* Off by default. No URL, no timestamp, no error trace is written by
  default.
* Debug logging is gated behind BOTH an environment variable
  (``PWA_ELH_DEBUG=1``) AND a sentinel file (default
  ``~/.local/share/pwa-elh/.debug``) being present. This dual-gating
  prevents accidental logging from a stray env var.
* URLs are NEVER written to the log, even in debug mode. The
  ``_log_event`` helper enforces this structurally via a permitted-field
  whitelist — any unrecognised field is silently dropped before
  serialisation.

Exit codes
----------
* 0 — normal completion (response was written, or no readable input,
  or protocol error / invalid request that produced a response).
* 1 — fatal I/O error before/while writing the response.
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import os
import platform
import struct
import subprocess
import sys
from pathlib import Path
from typing import Any, Final, Optional, TypedDict
from urllib.parse import urlsplit

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Conservative cap on incoming-request size (bytes). Platform limit is 64 MiB.
MAX_REQUEST_BYTES: Final[int] = 1 * 1024 * 1024  # 1 MiB

#: Conservative cap on outgoing-response size (bytes). Platform limit is 1 MiB.
MAX_RESPONSE_BYTES: Final[int] = 64 * 1024  # 64 KiB

#: Maximum accepted URL length.
MAX_URL: Final[int] = 8192

#: Native-messaging host name (must match the manifest's ``name`` field and
#: the value passed to ``chrome.runtime.sendNativeMessage``).
HOST_NAME: Final[str] = "com.aaharonov.pwa_elh"

#: Environment variable that gates debug logging (also requires the sentinel).
DEBUG_ENV_VAR: Final[str] = "PWA_ELH_DEBUG"

#: Allowed URL schemes.
ALLOWED_SCHEMES: Final[frozenset[str]] = frozenset({"http", "https"})

#: Sentinel URL accepted only when ``probe: true`` is set on the request.
PROBE_URL: Final[str] = "about:blank"

#: Permitted field names for ``_log_event``. Anything not in this set is
#: silently dropped before serialisation — a structural guard that
#: prevents URL content (or any other PII) from accidentally appearing in
#: the log, regardless of caller discipline.
ALLOWED_LOG_FIELDS: Final[frozenset[str]] = frozenset(
    {"ok", "error_class", "code", "errno", "scheme", "count"}
)


class Response(TypedDict, total=False):
    """Shape of the JSON response written to stdout."""

    ok: bool
    error: str
    probe: bool


# ---------------------------------------------------------------------------
# Platform-specific paths
# ---------------------------------------------------------------------------


def _user_data_dir() -> Path:
    """Return the per-user data directory for this host.

    Returns:
        Platform-appropriate writable data directory. The path is *not*
        created here — callers must create it on demand. The directory
        holds the debug sentinel file (``.debug``) and, when debug
        logging is enabled, the rotating log file (``host.log``). The
        sentinel and log MUST share this directory so that toggling
        debug mode does not silently divert the log to a different
        location.
    """
    system = platform.system()
    home = Path.home()
    if system == "Windows":
        base = os.environ.get("LOCALAPPDATA")
        if base:
            return Path(base) / "pwa-elh"
        return home / "AppData" / "Local" / "pwa-elh"
    if system == "Darwin":
        return home / "Library" / "Application Support" / "pwa-elh"
    # Linux / other POSIX: respect XDG_DATA_HOME if set.
    xdg = os.environ.get("XDG_DATA_HOME")
    if xdg:
        return Path(xdg) / "pwa-elh"
    return home / ".local" / "share" / "pwa-elh"


# ---------------------------------------------------------------------------
# Native messaging wire protocol
# ---------------------------------------------------------------------------


def read_message(stream: Any = None) -> Optional[dict[str, Any]]:
    """Read one length-prefixed JSON message from ``stream``.

    Args:
        stream: A binary-mode readable stream. Defaults to ``sys.stdin.buffer``.

    Returns:
        The decoded JSON object as a ``dict``, or ``None`` if the stream is
        closed (EOF before the length prefix is fully read), the payload is
        truncated, the declared length is zero or exceeds
        ``MAX_REQUEST_BYTES``, the payload is not valid UTF-8 JSON, or the
        decoded JSON is not an object.

    Notes:
        We deliberately return ``None`` on every protocol failure rather
        than raise — the caller must surface a structured response on
        stdout (or exit cleanly) rather than crash with a Python traceback
        that would corrupt the stdio channel.
    """
    if stream is None:
        stream = sys.stdin.buffer
    raw_len = stream.read(4)
    if len(raw_len) != 4:
        return None
    (length,) = struct.unpack("<I", raw_len)
    if length == 0 or length > MAX_REQUEST_BYTES:
        return None
    payload = stream.read(length)
    if len(payload) != length:
        return None
    try:
        decoded = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(decoded, dict):
        return None
    return decoded


def write_message(obj: Response, stream: Any = None) -> None:
    """Write one length-prefixed JSON message to ``stream``.

    Args:
        obj: The response object to serialise.
        stream: A binary-mode writable stream. Defaults to
            ``sys.stdout.buffer``.

    Raises:
        ValueError: If the serialised payload would exceed
            ``MAX_RESPONSE_BYTES``.

    Notes:
        ``ensure_ascii=False`` is fine because the payload is UTF-8 encoded
        on the wire. Chromium's native-messaging implementation reads UTF-8.
    """
    if stream is None:
        stream = sys.stdout.buffer
    data = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode(
        "utf-8"
    )
    if len(data) > MAX_RESPONSE_BYTES:
        raise ValueError(
            f"response too large: {len(data)} > {MAX_RESPONSE_BYTES}"
        )
    stream.write(struct.pack("<I", len(data)))
    stream.write(data)
    stream.flush()


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def validate_url(value: Any) -> Optional[str]:
    """Validate that ``value`` is a safe-to-open HTTP(S) URL.

    Args:
        value: The candidate URL (any type — must be a ``str``).

    Returns:
        The validated URL string, or ``None`` if ``value`` is not a
        string, is empty, exceeds ``MAX_URL`` characters, fails to parse,
        has a scheme other than ``http``/``https``, or has an empty
        network location.
    """
    if not isinstance(value, str):
        return None
    if not value or len(value) > MAX_URL:
        return None
    try:
        parts = urlsplit(value)
    except ValueError:
        return None
    if parts.scheme not in ALLOWED_SCHEMES:
        return None
    if not parts.netloc:
        return None
    return value


def validate_browser_override(value: Any) -> tuple[Optional[str], Optional[str]]:
    """Validate the ``browser_override`` field of a request.

    Args:
        value: The candidate browser-binary path. ``None`` or empty
            string means "no override; use OS default".

    Returns:
        A ``(path, error)`` pair. Exactly one of the two is non-``None``:

        * ``(None, None)`` — no override requested (caller must use OS
          default launcher).
        * ``(path, None)`` — validated **absolute** path to an existing,
          executable regular file.
        * ``(None, error)`` — a user-facing error string describing the
          validation failure.

    Notes:
        Relative paths are rejected outright. The host's CWD is set by
        the browser when it spawns us and is not a stable trust boundary
        — accepting a relative path would let a request silently
        resolve against an unexpected directory. Requiring absolute
        paths makes the spawn target unambiguous.
    """
    if value is None:
        return None, None
    if not isinstance(value, str):
        return None, "browser_override must be a string"
    if not value:
        return None, None
    if not os.path.isabs(value):
        return None, "browser_override must be an absolute path"
    if not os.path.isfile(value):
        return None, "browser_override not executable"
    if not os.access(value, os.X_OK):
        return None, "browser_override not executable"
    return value, None


# ---------------------------------------------------------------------------
# OS dispatch
# ---------------------------------------------------------------------------


def open_url(
    url: str,
    browser_override: Optional[str] = None,
    *,
    system: Optional[str] = None,
    popen: Any = None,
    startfile: Any = None,
) -> Response:
    """Open ``url`` via the OS default browser or an explicit binary.

    Args:
        url: A pre-validated HTTP(S) URL (see ``validate_url``).
        browser_override: A pre-validated executable path, or ``None`` to
            use the OS default-browser launcher.
        system: Override of ``platform.system()`` — used by tests.
        popen: Override of ``subprocess.Popen`` — used by tests.
        startfile: Override of ``os.startfile`` — used by tests.

    Returns:
        ``{"ok": True}`` on success, otherwise ``{"ok": False, "error":
        "..."}`` with a short user-facing reason.

    Notes:
        We never wait for the child process. The launched browser
        process must outlive the host's exit. On POSIX,
        ``start_new_session=True`` detaches it from the host's process
        group; on Windows, ``os.startfile`` returns immediately by design.
    """
    if system is None:
        system = platform.system()
    if popen is None:
        popen = subprocess.Popen

    # Explicit-override branch — works the same on all OSes because we
    # invoke the binary directly with the URL as the single argument.
    if browser_override:
        argv = [browser_override, url]
        return _spawn_detached(argv, system=system, popen=popen)

    # Default-launcher branch — OS-specific dispatch.
    if system == "Linux":
        return _spawn_detached(["xdg-open", url], system=system, popen=popen)
    if system == "Darwin":
        return _spawn_detached(["open", url], system=system, popen=popen)
    if system == "Windows":
        # os.startfile uses ShellExecuteEx — the canonical "open with default
        # handler" path on Windows. It avoids cmd.exe metacharacter
        # reparsing (URLs with '&', '%', '^', etc. otherwise break under
        # `cmd /c start`).
        if startfile is None:
            startfile = getattr(os, "startfile", None)
        if startfile is None:
            return {
                "ok": False,
                "error": "os.startfile unavailable on this platform",
            }
        try:
            startfile(url, "open")
        except OSError as exc:
            # exc.errno is None for some ShellExecuteEx failures; we still
            # emit the class name so callers get a stable error label, and
            # append the numeric errno when available for diagnostics.
            errno = getattr(exc, "errno", None)
            errno_str = f" errno={errno}" if errno is not None else ""
            return {
                "ok": False,
                "error": f"startfile failed: {exc.__class__.__name__}{errno_str}",
            }
        return {"ok": True}
    return {"ok": False, "error": f"unsupported platform: {system}"}


def _spawn_detached(argv: list[str], *, system: str, popen: Any) -> Response:
    """Spawn ``argv`` detached from the host process, no shell, no IO.

    Args:
        argv: Command vector. ``argv[0]`` is the program; the remaining
            elements are arguments. Never passed to a shell.
        system: Platform identifier (``"Linux"``, ``"Darwin"``, etc.).
        popen: Callable matching ``subprocess.Popen`` — injected for tests.

    Returns:
        ``{"ok": True}`` on successful spawn, or ``{"ok": False, "error":
        "..."}`` on a failure to launch.
    """
    try:
        popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            start_new_session=(system != "Windows"),
        )
    except FileNotFoundError:
        return {"ok": False, "error": f"{argv[0]} not found"}
    except PermissionError:
        return {"ok": False, "error": f"{argv[0]} not executable"}
    except OSError as exc:
        return {"ok": False, "error": f"spawn failed: {exc.__class__.__name__}"}
    return {"ok": True}


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------


def _debug_enabled() -> bool:
    """Decide whether debug logging is on.

    Returns:
        ``True`` iff *both* (a) the ``PWA_ELH_DEBUG=1`` environment
        variable is set AND (b) the debug sentinel file
        (``<user-data>/.debug``) exists. The dual gate prevents stray
        env vars from silently turning logging back on, and ensures the
        operator has taken a deliberate filesystem action.
    """
    if os.environ.get(DEBUG_ENV_VAR) != "1":
        return False
    sentinel = _user_data_dir() / ".debug"
    try:
        return sentinel.is_file()
    except OSError:
        return False


def _configure_logging(enabled: bool) -> logging.Logger:
    """Configure (or silence) the module logger.

    Args:
        enabled: Whether to install a rotating-file handler.

    Returns:
        The module logger, configured per ``enabled``.
    """
    logger = logging.getLogger("pwa_elh.host")
    # Clear any handlers from a previous call (relevant in tests; harmless
    # in single-shot production use).
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
    if not enabled:
        logger.addHandler(logging.NullHandler())
        logger.setLevel(logging.CRITICAL + 1)  # effectively silent
        return logger
    try:
        log_dir = _user_data_dir()
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / "host.log"
        handler = logging.handlers.RotatingFileHandler(
            log_path,
            maxBytes=256 * 1024,
            backupCount=5,
            encoding="utf-8",
        )
        handler.setFormatter(
            logging.Formatter(
                '{"ts":"%(asctime)s","level":"%(levelname)s","msg":%(message)s}'
            )
        )
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)
    except OSError:
        logger.addHandler(logging.NullHandler())
        logger.setLevel(logging.CRITICAL + 1)
    return logger


def _log_event(
    logger: logging.Logger, event: str, **fields: Any
) -> None:
    """Emit a structured debug event with structural URL exclusion.

    Args:
        logger: The configured module logger.
        event: A short event label (e.g. ``"open_ok"``, ``"validate_fail"``).
        **fields: Extra non-sensitive fields. ONLY keys in
            ``ALLOWED_LOG_FIELDS`` are serialised — every other key is
            silently dropped. This makes URL leakage structurally
            impossible regardless of caller discipline.

    Notes:
        ``None`` values are also dropped so log lines stay minimal.
        The dropped-keys discipline is enforced here so unit tests of
        callers don't need to repeat the negative assertion at every site.
    """
    safe: dict[str, Any] = {"event": event}
    for key, value in fields.items():
        if key in ALLOWED_LOG_FIELDS and value is not None:
            safe[key] = value
    logger.info(json.dumps(safe, ensure_ascii=False, separators=(",", ":")))


# ---------------------------------------------------------------------------
# Request handling
# ---------------------------------------------------------------------------


def handle_request(
    request: dict[str, Any],
    *,
    system: Optional[str] = None,
    popen: Any = None,
    startfile: Any = None,
) -> Response:
    """Validate and dispatch one request.

    Args:
        request: The decoded JSON request object.
        system: Override of ``platform.system()`` — used by tests.
        popen: Override of ``subprocess.Popen`` — used by tests.
        startfile: Override of ``os.startfile`` — used by tests.

    Returns:
        The response object to be written back over the wire. For a
        valid probe the response is ``{"ok": True, "probe": True}`` per
        design §3.5.4. For an invalid probe (``probe: true`` with any
        URL other than ``"about:blank"``) the response is ``{"ok":
        False, "error": "invalid probe"}`` — the probe path is not a
        covert no-spawn channel for arbitrary URLs.
    """
    raw_url = request.get("url")

    # Probe path FIRST (design §3.5.4). The probe contract requires both
    # ``probe === true`` AND ``url === "about:blank"``. Any other URL with
    # ``probe: true`` is rejected as malformed — before URL validation
    # could falsely reject ``about:blank`` for being non-http.
    if request.get("probe") is True:
        if raw_url != PROBE_URL:
            return {"ok": False, "error": "invalid probe"}
        return {"ok": True, "probe": True}

    url = validate_url(raw_url)
    if url is None:
        return {"ok": False, "error": "invalid url"}

    override, override_error = validate_browser_override(
        request.get("browser_override")
    )
    if override_error:
        return {"ok": False, "error": override_error}

    return open_url(url, override, system=system, popen=popen, startfile=startfile)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main(
    argv: Optional[list[str]] = None,
    *,
    stdin: Any = None,
    stdout: Any = None,
) -> int:
    """Run the native-host one-shot loop.

    Args:
        argv: Unused; accepted for symmetry with conventional entry points.
        stdin: Binary input stream. Defaults to ``sys.stdin.buffer``.
            Tests inject a ``BytesIO`` here.
        stdout: Binary output stream. Defaults to ``sys.stdout.buffer``.

    Returns:
        An OS exit code: ``0`` on normal completion (response delivered or
        no readable input), ``1`` on a fatal I/O failure while writing.
    """
    del argv  # Native messaging hosts ignore argv.

    if stdin is None:
        stdin = sys.stdin.buffer
    if stdout is None:
        stdout = sys.stdout.buffer

    logger = _configure_logging(_debug_enabled())

    try:
        request = read_message(stdin)
    except OSError:
        _log_event(logger, "read_io_error")
        return 1

    if request is None:
        # Nothing to do — Chromium closed the pipe or sent garbage. Exit
        # cleanly without writing anything (the channel may already be
        # half-closed).
        _log_event(logger, "no_request")
        return 0

    response = handle_request(request)

    try:
        write_message(response, stdout)
    except (OSError, ValueError) as exc:
        _log_event(logger, "write_error", error_class=exc.__class__.__name__)
        return 1

    error = response.get("error", "")
    error_class = error.split(":", 1)[0] if error else None
    _log_event(
        logger,
        "request_done",
        ok=bool(response.get("ok")),
        error_class=error_class,
    )
    return 0


if __name__ == "__main__":  # pragma: no cover - entry point shim
    sys.exit(main())
