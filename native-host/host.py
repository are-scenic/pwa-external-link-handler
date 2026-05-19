#!/usr/bin/env python3
"""Chrome Native Messaging host for the PWA External Link Handler extension.

Reads a single length-prefixed JSON request from stdin, opens the requested
HTTP(S) URL via the OS default-browser launcher (or an optionally configured
browser binary), writes a length-prefixed JSON response to stdout, and exits.
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


# Chromium caps extension -> host messages at 64 MiB; we apply a much smaller
# cap defensively. Responses are tiny ({"ok": true} shape).
MAX_REQUEST_BYTES: Final[int] = 1 * 1024 * 1024
MAX_RESPONSE_BYTES: Final[int] = 64 * 1024

MAX_URL: Final[int] = 8192

# Must match the manifest's "name" field and the argument passed to
# chrome.runtime.sendNativeMessage.
HOST_NAME: Final[str] = "com.aaharonov.pwa_elh"

DEBUG_ENV_VAR: Final[str] = "PWA_ELH_DEBUG"

ALLOWED_SCHEMES: Final[frozenset[str]] = frozenset({"http", "https"})

# Sentinel URL accepted only when the request also sets probe=True.
PROBE_URL: Final[str] = "about:blank"

# Whitelist of field names allowed in debug log records. Everything else is
# dropped before serialisation so URL content cannot leak into logs even if a
# future caller passes it by mistake.
ALLOWED_LOG_FIELDS: Final[frozenset[str]] = frozenset(
    {"ok", "error_class", "code", "errno", "scheme", "count"}
)


class Response(TypedDict, total=False):
    """Shape of the JSON response written to stdout."""

    ok: bool
    error: str
    probe: bool


def _user_data_dir() -> Path:
    """Return the per-user data directory for this host.

    The directory is not created here — callers create it on demand. It
    holds the debug sentinel file and, when debug logging is enabled, the
    rotating log file. Both must share this directory so that toggling
    debug mode does not silently divert the log elsewhere.
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
    xdg = os.environ.get("XDG_DATA_HOME")
    if xdg:
        return Path(xdg) / "pwa-elh"
    return home / ".local" / "share" / "pwa-elh"


def read_message(stream: Any = None) -> Optional[dict[str, Any]]:
    """Read one length-prefixed JSON message from ``stream``.

    Returns the decoded object, or ``None`` on any protocol failure (EOF,
    truncated payload, oversized length, invalid UTF-8/JSON, non-object
    payload). We deliberately return ``None`` rather than raise so the
    caller can respond cleanly on stdout instead of crashing with a
    traceback that would corrupt the stdio channel.
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

    Raises:
        ValueError: If the serialised payload would exceed
            ``MAX_RESPONSE_BYTES``.
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


def validate_url(value: Any) -> Optional[str]:
    """Validate that ``value`` is a safe-to-open HTTP(S) URL.

    Returns the URL on success, ``None`` if it is not a string, is empty,
    exceeds ``MAX_URL`` characters, fails to parse, uses a scheme other
    than ``http``/``https``, or has an empty network location.
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

    Returns a ``(path, error)`` pair where exactly one element is non-None.
    ``(None, None)`` means no override was requested.

    Relative paths are rejected: the host's CWD is set by the browser when
    it spawns us and is not a stable trust boundary. Requiring absolute
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


def open_url(
    url: str,
    browser_override: Optional[str] = None,
    *,
    system: Optional[str] = None,
    popen: Any = None,
    startfile: Any = None,
) -> Response:
    """Open ``url`` via the OS default browser or an explicit binary.

    We never wait for the child. On POSIX, ``start_new_session=True``
    detaches it from the host's process group so it survives our exit;
    on Windows, ``os.startfile`` returns immediately by design.
    """
    if system is None:
        system = platform.system()
    if popen is None:
        popen = subprocess.Popen

    if browser_override:
        argv = [browser_override, url]
        return _spawn_detached(argv, system=system, popen=popen)

    if system == "Linux":
        return _spawn_detached(["xdg-open", url], system=system, popen=popen)
    if system == "Darwin":
        return _spawn_detached(["open", url], system=system, popen=popen)
    if system == "Windows":
        # os.startfile uses ShellExecuteEx, the canonical "open with default
        # handler" path on Windows. Using `cmd /c start` instead would
        # subject the URL to cmd.exe metacharacter reparsing — every URL
        # containing '&', '%', '^', etc. would be mangled.
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
            # exc.errno is None for some ShellExecuteEx failures; emit the
            # class name unconditionally and append the numeric errno when
            # available.
            errno = getattr(exc, "errno", None)
            errno_str = f" errno={errno}" if errno is not None else ""
            return {
                "ok": False,
                "error": f"startfile failed: {exc.__class__.__name__}{errno_str}",
            }
        return {"ok": True}
    return {"ok": False, "error": f"unsupported platform: {system}"}


def _spawn_detached(argv: list[str], *, system: str, popen: Any) -> Response:
    """Spawn ``argv`` detached from the host process, no shell, no IO."""
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


def _debug_enabled() -> bool:
    """Return True iff both the env var and the sentinel file are present.

    The dual gate prevents a stray ``PWA_ELH_DEBUG=1`` in an inherited
    environment from silently re-enabling logging — the operator must
    also create the sentinel file on disk.
    """
    if os.environ.get(DEBUG_ENV_VAR) != "1":
        return False
    sentinel = _user_data_dir() / ".debug"
    try:
        return sentinel.is_file()
    except OSError:
        return False


def _configure_logging(enabled: bool) -> logging.Logger:
    """Configure (or silence) the module logger."""
    logger = logging.getLogger("pwa_elh.host")
    # Clear handlers from any previous call (matters in tests).
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
    if not enabled:
        logger.addHandler(logging.NullHandler())
        logger.setLevel(logging.CRITICAL + 1)
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
    """Emit a structured debug event.

    Only keys in ``ALLOWED_LOG_FIELDS`` are serialised; everything else is
    dropped. This makes URL leakage into logs structurally impossible
    regardless of caller discipline. ``None`` values are also dropped to
    keep log lines minimal.
    """
    safe: dict[str, Any] = {"event": event}
    for key, value in fields.items():
        if key in ALLOWED_LOG_FIELDS and value is not None:
            safe[key] = value
    logger.info(json.dumps(safe, ensure_ascii=False, separators=(",", ":")))


def handle_request(
    request: dict[str, Any],
    *,
    system: Optional[str] = None,
    popen: Any = None,
    startfile: Any = None,
) -> Response:
    """Validate and dispatch one request."""
    raw_url = request.get("url")

    # Probe path is checked first: it requires both probe=True AND
    # url=="about:blank". Any other URL with probe=True is rejected so the
    # probe path cannot be used as a covert no-spawn channel.
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


def main(
    argv: Optional[list[str]] = None,
    *,
    stdin: Any = None,
    stdout: Any = None,
) -> int:
    """Run the native-host one-shot loop.

    Returns 0 on normal completion (response delivered or no readable
    input) and 1 on a fatal I/O failure while writing.
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
        # Chromium closed the pipe or sent garbage. Exit cleanly without
        # writing anything — the channel may already be half-closed.
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


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
