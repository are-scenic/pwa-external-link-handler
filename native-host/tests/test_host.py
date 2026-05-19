"""Unit tests for the PWA External Link Handler native host."""

from __future__ import annotations

import io
import json
import logging
import os
import struct
import subprocess
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

import host


def _encode(payload: Any) -> bytes:
    """Serialise ``payload`` as a Chrome native-messaging frame."""
    body = json.dumps(payload).encode("utf-8")
    return struct.pack("<I", len(body)) + body


def _decode(frame: bytes) -> Any:
    """Decode a single Chrome native-messaging frame."""
    (length,) = struct.unpack("<I", frame[:4])
    body = frame[4 : 4 + length]
    return json.loads(body.decode("utf-8"))


class TestReadMessage:
    def test_read_message_valid_object_returns_dict(self) -> None:
        stream = io.BytesIO(_encode({"url": "https://example.com"}))
        assert host.read_message(stream) == {"url": "https://example.com"}

    def test_read_message_eof_before_length_returns_none(self) -> None:
        assert host.read_message(io.BytesIO(b"")) is None

    def test_read_message_short_length_prefix_returns_none(self) -> None:
        assert host.read_message(io.BytesIO(b"\x01")) is None

    def test_read_message_zero_length_returns_none(self) -> None:
        stream = io.BytesIO(struct.pack("<I", 0))
        assert host.read_message(stream) is None

    def test_read_message_length_exceeds_cap_returns_none(self) -> None:
        stream = io.BytesIO(struct.pack("<I", host.MAX_REQUEST_BYTES + 1))
        assert host.read_message(stream) is None

    def test_read_message_truncated_body_returns_none(self) -> None:
        body = b'{"url":"https://example.com"}'
        # Claim more bytes than provided.
        stream = io.BytesIO(struct.pack("<I", len(body) + 50) + body)
        assert host.read_message(stream) is None

    def test_read_message_invalid_utf8_returns_none(self) -> None:
        body = b"\xff\xfe\xfd"
        stream = io.BytesIO(struct.pack("<I", len(body)) + body)
        assert host.read_message(stream) is None

    def test_read_message_invalid_json_returns_none(self) -> None:
        body = b"{not json"
        stream = io.BytesIO(struct.pack("<I", len(body)) + body)
        assert host.read_message(stream) is None

    def test_read_message_non_object_json_returns_none(self) -> None:
        body = b"[1,2,3]"
        stream = io.BytesIO(struct.pack("<I", len(body)) + body)
        assert host.read_message(stream) is None


class TestWriteMessage:
    def test_write_message_emits_length_prefix_and_payload(self) -> None:
        stream = io.BytesIO()
        host.write_message({"ok": True}, stream)
        frame = stream.getvalue()
        assert _decode(frame) == {"ok": True}

    def test_write_message_too_large_raises_value_error(self) -> None:
        stream = io.BytesIO()
        huge = {"error": "x" * (host.MAX_RESPONSE_BYTES + 1)}
        with pytest.raises(ValueError, match="too large"):
            host.write_message(huge, stream)

    def test_write_message_round_trips_through_read(self) -> None:
        out = io.BytesIO()
        host.write_message({"ok": False, "error": "invalid url"}, out)
        in_stream = io.BytesIO(out.getvalue())
        assert host.read_message(in_stream) == {
            "ok": False,
            "error": "invalid url",
        }


class TestValidateUrl:
    @pytest.mark.parametrize(
        "url",
        [
            "http://example.com",
            "https://example.com/",
            "https://example.com/path?q=1&r=2",
            "https://user@example.com:8080/x",
            "https://example.com/#frag",
        ],
    )
    def test_validate_url_accepts_http_https(self, url: str) -> None:
        assert host.validate_url(url) == url

    @pytest.mark.parametrize(
        "url",
        [
            "ftp://example.com",
            "file:///etc/passwd",
            "mailto:a@b.c",
            "javascript:alert(1)",
            "data:text/plain,hi",
            "ws://example.com",
            "chrome://settings",
            "about:blank",  # the probe URL is not a valid open target
        ],
    )
    def test_validate_url_rejects_other_schemes(self, url: str) -> None:
        assert host.validate_url(url) is None

    def test_validate_url_rejects_empty_string(self) -> None:
        assert host.validate_url("") is None

    def test_validate_url_rejects_oversized(self) -> None:
        long_url = "https://example.com/" + ("a" * host.MAX_URL)
        assert host.validate_url(long_url) is None

    def test_validate_url_accepts_at_cap(self) -> None:
        prefix = "https://example.com/"
        url = prefix + ("a" * (host.MAX_URL - len(prefix)))
        assert host.validate_url(url) == url

    @pytest.mark.parametrize(
        "value",
        [None, 42, [], {}, b"https://example.com", 3.14],
    )
    def test_validate_url_rejects_non_string(self, value: Any) -> None:
        assert host.validate_url(value) is None

    def test_validate_url_rejects_no_netloc(self) -> None:
        assert host.validate_url("http:///path") is None

    def test_validate_url_rejects_scheme_only(self) -> None:
        assert host.validate_url("http://") is None


class TestValidateBrowserOverride:
    def test_none_returns_none_none(self) -> None:
        assert host.validate_browser_override(None) == (None, None)

    def test_empty_string_returns_none_none(self) -> None:
        assert host.validate_browser_override("") == (None, None)

    def test_non_string_returns_error(self) -> None:
        path, err = host.validate_browser_override(42)
        assert path is None and err is not None and "string" in err

    def test_relative_path_rejected(self, tmp_path: Path) -> None:
        # An executable file at a relative path is still rejected — the
        # host's CWD is not a trust boundary.
        binary = tmp_path / "bin"
        binary.write_text("#!/bin/sh\n")
        os.chmod(binary, 0o755)
        path, err = host.validate_browser_override("bin")
        assert path is None
        assert err == "browser_override must be an absolute path"

    def test_missing_file_returns_error(self, tmp_path: Path) -> None:
        path, err = host.validate_browser_override(str(tmp_path / "nope"))
        assert path is None
        assert err == "browser_override not executable"

    def test_non_executable_returns_error(self, tmp_path: Path) -> None:
        bin_path = tmp_path / "bin"
        bin_path.write_text("not a binary")
        os.chmod(bin_path, 0o644)
        path, err = host.validate_browser_override(str(bin_path))
        assert path is None
        assert err == "browser_override not executable"

    @pytest.mark.skipif(
        sys.platform == "win32",
        reason="POSIX-specific executable bit semantics",
    )
    def test_executable_absolute_file_returns_path(
        self, tmp_path: Path
    ) -> None:
        bin_path = tmp_path / "bin"
        bin_path.write_text("#!/bin/sh\n")
        os.chmod(bin_path, 0o755)
        path, err = host.validate_browser_override(str(bin_path))
        assert path == str(bin_path)
        assert err is None


class TestOpenUrlPosix:
    def test_linux_uses_xdg_open(self) -> None:
        popen = MagicMock()
        result = host.open_url(
            "https://example.com", None, system="Linux", popen=popen
        )
        assert result == {"ok": True}
        argv = popen.call_args.args[0]
        assert argv == ["xdg-open", "https://example.com"]
        kw = popen.call_args.kwargs
        assert kw["stdin"] == subprocess.DEVNULL
        assert kw["stdout"] == subprocess.DEVNULL
        assert kw["stderr"] == subprocess.DEVNULL
        assert kw["close_fds"] is True
        assert kw["start_new_session"] is True

    def test_macos_uses_open(self) -> None:
        popen = MagicMock()
        result = host.open_url(
            "https://example.com", None, system="Darwin", popen=popen
        )
        assert result == {"ok": True}
        assert popen.call_args.args[0] == ["open", "https://example.com"]
        assert popen.call_args.kwargs["start_new_session"] is True

    def test_unsupported_platform_returns_error(self) -> None:
        result = host.open_url("https://example.com", None, system="Plan9")
        assert result["ok"] is False
        assert "unsupported platform" in result["error"]

    def test_file_not_found_returns_error(self) -> None:
        popen = MagicMock(side_effect=FileNotFoundError("xdg-open"))
        result = host.open_url(
            "https://example.com", None, system="Linux", popen=popen
        )
        assert result == {"ok": False, "error": "xdg-open not found"}

    def test_permission_error_returns_error(self) -> None:
        popen = MagicMock(side_effect=PermissionError("denied"))
        result = host.open_url(
            "https://example.com", None, system="Linux", popen=popen
        )
        assert result == {
            "ok": False,
            "error": "xdg-open not executable",
        }

    def test_oserror_returns_generic_error(self) -> None:
        popen = MagicMock(side_effect=OSError("boom"))
        result = host.open_url(
            "https://example.com", None, system="Linux", popen=popen
        )
        assert result["ok"] is False
        assert "spawn failed" in result["error"]


class TestOpenUrlOverride:
    def test_override_invoked_directly(self) -> None:
        popen = MagicMock()
        result = host.open_url(
            "https://example.com",
            "/usr/bin/firefox",
            system="Linux",
            popen=popen,
        )
        assert result == {"ok": True}
        assert popen.call_args.args[0] == [
            "/usr/bin/firefox",
            "https://example.com",
        ]

    def test_override_on_windows_uses_popen_not_startfile(self) -> None:
        # Explicit override should bypass os.startfile and use Popen so the
        # user's chosen binary is invoked directly without shell parsing.
        popen = MagicMock()
        startfile = MagicMock()
        result = host.open_url(
            "https://example.com",
            r"C:\Program Files\Firefox\firefox.exe",
            system="Windows",
            popen=popen,
            startfile=startfile,
        )
        assert result == {"ok": True}
        popen.assert_called_once()
        startfile.assert_not_called()
        # start_new_session is POSIX-only — must be False on Windows.
        assert popen.call_args.kwargs["start_new_session"] is False


class TestOpenUrlWindows:
    def test_windows_uses_startfile(self) -> None:
        startfile = MagicMock()
        popen = MagicMock()
        result = host.open_url(
            "https://example.com?a=1&b=2",
            None,
            system="Windows",
            popen=popen,
            startfile=startfile,
        )
        assert result == {"ok": True}
        startfile.assert_called_once_with("https://example.com?a=1&b=2", "open")
        popen.assert_not_called()

    def test_windows_startfile_oserror_returns_error(self) -> None:
        startfile = MagicMock(side_effect=OSError("no association"))
        result = host.open_url(
            "https://example.com",
            None,
            system="Windows",
            popen=MagicMock(),
            startfile=startfile,
        )
        assert result["ok"] is False
        assert "startfile failed" in result["error"]

    def test_windows_startfile_oserror_with_errno_includes_errno(self) -> None:
        exc = OSError("denied")
        exc.errno = 5
        startfile = MagicMock(side_effect=exc)
        result = host.open_url(
            "https://example.com",
            None,
            system="Windows",
            popen=MagicMock(),
            startfile=startfile,
        )
        assert result["ok"] is False
        assert "errno=5" in result["error"]

    def test_windows_without_startfile_returns_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Simulate a Python build with no os.startfile (i.e. POSIX). The
        # host must surface a clear error rather than raise.
        if hasattr(os, "startfile"):
            monkeypatch.delattr(os, "startfile", raising=False)
        result = host.open_url(
            "https://example.com",
            None,
            system="Windows",
            popen=MagicMock(),
        )
        assert result["ok"] is False
        assert "os.startfile" in result["error"]


class TestLogging:
    def test_debug_disabled_by_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(host.DEBUG_ENV_VAR, raising=False)
        assert host._debug_enabled() is False

    def test_debug_requires_both_env_and_sentinel(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        monkeypatch.setenv(host.DEBUG_ENV_VAR, "1")
        monkeypatch.setattr(host, "_user_data_dir", lambda: tmp_path)
        # Env var alone is not enough.
        assert host._debug_enabled() is False
        (tmp_path / ".debug").write_text("")
        assert host._debug_enabled() is True

    def test_debug_sentinel_only_is_not_enough(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        monkeypatch.delenv(host.DEBUG_ENV_VAR, raising=False)
        monkeypatch.setattr(host, "_user_data_dir", lambda: tmp_path)
        (tmp_path / ".debug").write_text("")
        assert host._debug_enabled() is False

    def test_configure_logging_disabled_is_silent(self) -> None:
        logger = host._configure_logging(False)
        assert logger.level > logging.CRITICAL

    def test_configure_logging_enabled_writes_to_file(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        monkeypatch.setattr(host, "_user_data_dir", lambda: tmp_path)
        logger = host._configure_logging(True)
        host._log_event(logger, "test_event", code=1)
        for h in list(logger.handlers):
            h.close()
            logger.removeHandler(h)
        log_path = tmp_path / "host.log"
        assert log_path.is_file()
        text = log_path.read_text(encoding="utf-8")
        assert "test_event" in text
        assert '"code": 1' in text or '"code":1' in text

    def test_log_event_drops_disallowed_field_url(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """Passing url= to _log_event must not emit URL content.

        The field whitelist drops it before serialisation even if a future
        caller passes a URL by mistake.
        """
        monkeypatch.setattr(host, "_user_data_dir", lambda: tmp_path)
        logger = host._configure_logging(True)
        host._log_event(
            logger,
            "open_attempt",
            url="https://secret.example.com/path?token=abc",
            error_class="FileNotFoundError",
        )
        for h in list(logger.handlers):
            h.close()
            logger.removeHandler(h)
        text = (tmp_path / "host.log").read_text(encoding="utf-8")
        assert "https://" not in text
        assert "secret.example.com" not in text
        assert "token" not in text
        assert "FileNotFoundError" in text

    def test_log_event_drops_none_values(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        monkeypatch.setattr(host, "_user_data_dir", lambda: tmp_path)
        logger = host._configure_logging(True)
        host._log_event(logger, "ev", error_class=None, ok=True)
        for h in list(logger.handlers):
            h.close()
            logger.removeHandler(h)
        text = (tmp_path / "host.log").read_text(encoding="utf-8")
        assert "error_class" not in text
        assert '"ok": true' in text or '"ok":true' in text


class TestProbeContract:
    def test_probe_about_blank_returns_ok_probe_true(self) -> None:
        popen = MagicMock()
        response = host.handle_request(
            {"url": "about:blank", "browser_override": None, "probe": True},
            system="Linux",
            popen=popen,
        )
        assert response == {"ok": True, "probe": True}
        popen.assert_not_called()

    def test_probe_with_other_url_rejected(self) -> None:
        """Probe path must not double as a covert no-spawn channel."""
        popen = MagicMock()
        response = host.handle_request(
            {"url": "https://example.com", "probe": True},
            system="Linux",
            popen=popen,
        )
        assert response == {"ok": False, "error": "invalid probe"}
        popen.assert_not_called()

    def test_probe_with_no_url_rejected(self) -> None:
        response = host.handle_request({"probe": True})
        assert response == {"ok": False, "error": "invalid probe"}

    def test_probe_false_with_about_blank_rejected_as_invalid_url(self) -> None:
        """about:blank without probe=True is rejected by URL validation."""
        response = host.handle_request({"url": "about:blank"})
        assert response == {"ok": False, "error": "invalid url"}

    def test_probe_field_must_be_strictly_true(self) -> None:
        # Truthy-but-not-True values must not trigger the probe path.
        for value in [1, "true", "yes", [1]]:
            response = host.handle_request(
                {"url": "about:blank", "probe": value}
            )
            assert response == {"ok": False, "error": "invalid url"}


class TestHandleRequest:
    def test_valid_request_calls_open_url(self) -> None:
        popen = MagicMock()
        response = host.handle_request(
            {"url": "https://example.com", "browser_override": None},
            system="Linux",
            popen=popen,
        )
        assert response == {"ok": True}
        popen.assert_called_once()

    def test_missing_url_returns_invalid(self) -> None:
        response = host.handle_request({})
        assert response == {"ok": False, "error": "invalid url"}

    def test_bad_scheme_returns_invalid(self) -> None:
        response = host.handle_request({"url": "file:///etc/passwd"})
        assert response == {"ok": False, "error": "invalid url"}

    def test_bad_browser_override_returns_error(self) -> None:
        response = host.handle_request(
            {
                "url": "https://example.com",
                "browser_override": "/nonexistent/binary",
            },
            system="Linux",
        )
        assert response == {
            "ok": False,
            "error": "browser_override not executable",
        }

    def test_relative_browser_override_rejected(self) -> None:
        response = host.handle_request(
            {
                "url": "https://example.com",
                "browser_override": "firefox",
            },
            system="Linux",
        )
        assert response == {
            "ok": False,
            "error": "browser_override must be an absolute path",
        }

    def test_valid_absolute_browser_override_used(
        self, tmp_path: Path
    ) -> None:
        binary = tmp_path / "fake-browser"
        binary.write_text("#!/bin/sh\n")
        os.chmod(binary, 0o755)
        popen = MagicMock()
        response = host.handle_request(
            {
                "url": "https://example.com",
                "browser_override": str(binary),
            },
            system="Linux",
            popen=popen,
        )
        assert response == {"ok": True}
        assert popen.call_args.args[0][0] == str(binary)


class TestMain:
    def test_main_no_input_returns_zero(self) -> None:
        rc = host.main(stdin=io.BytesIO(b""), stdout=io.BytesIO())
        assert rc == 0

    def test_main_invalid_url_writes_error_response(self) -> None:
        stdin = io.BytesIO(_encode({"url": "ftp://example.com"}))
        stdout = io.BytesIO()
        rc = host.main(stdin=stdin, stdout=stdout)
        assert rc == 0
        assert _decode(stdout.getvalue()) == {
            "ok": False,
            "error": "invalid url",
        }

    def test_main_valid_url_dispatches(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        stdin = io.BytesIO(
            _encode({"url": "https://example.com", "browser_override": None})
        )
        stdout = io.BytesIO()
        fake_popen = MagicMock()
        monkeypatch.setattr(host.subprocess, "Popen", fake_popen)
        monkeypatch.setattr(host.platform, "system", lambda: "Linux")
        rc = host.main(stdin=stdin, stdout=stdout)
        assert rc == 0
        assert _decode(stdout.getvalue()) == {"ok": True}
        assert fake_popen.call_args.args[0] == [
            "xdg-open",
            "https://example.com",
        ]

    def test_main_truncated_input_returns_zero(self) -> None:
        # Length prefix promises more bytes than supplied — the host must
        # exit cleanly without writing a response.
        stdin = io.BytesIO(struct.pack("<I", 100) + b"x")
        stdout = io.BytesIO()
        rc = host.main(stdin=stdin, stdout=stdout)
        assert rc == 0
        assert stdout.getvalue() == b""

    def test_main_probe_returns_ok_probe_true(self) -> None:
        stdin = io.BytesIO(
            _encode({"url": "about:blank", "probe": True})
        )
        stdout = io.BytesIO()
        rc = host.main(stdin=stdin, stdout=stdout)
        assert rc == 0
        assert _decode(stdout.getvalue()) == {"ok": True, "probe": True}
