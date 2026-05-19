"""Regression tests for the Linux/macOS installer (install.sh)."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="install.sh is a bash script — Linux/macOS only",
)


_REPO_ROOT = Path(__file__).resolve().parent.parent
_INSTALL_SH = _REPO_ROOT / "install.sh"


def _run_install(
    *,
    prefix: Path,
    home: Path,
    chrome_id: str = "abcdefghijklmnopabcdefghijklmnop",
    edge_id: str = "ponmlkjihgfedcbaponmlkjihgfedcba",
    uninstall: bool = False,
) -> subprocess.CompletedProcess[str]:
    """Run install.sh with isolated HOME and PWA_ELH_PREFIX."""
    env = os.environ.copy()
    env["HOME"] = str(home)
    env["PWA_ELH_PREFIX"] = str(prefix)
    env["CHROME_WEB_STORE_ID"] = chrome_id
    env["EDGE_ADDONS_ID"] = edge_id
    args = [str(_INSTALL_SH)]
    if uninstall:
        args.append("--uninstall")
    return subprocess.run(
        args,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.fixture()
def sandbox(tmp_path: Path) -> tuple[Path, Path]:
    """Return (home_dir, install_prefix) for a sandboxed install run."""
    home = tmp_path / "home"
    home.mkdir()
    prefix = home / ".local" / "share" / "pwa-elh"
    return home, prefix


class TestInstallerRendering:
    def test_install_succeeds(self, sandbox: tuple[Path, Path]) -> None:
        home, prefix = sandbox
        result = _run_install(prefix=prefix, home=home)
        assert result.returncode == 0, result.stderr
        assert (prefix / "host.py").is_file()

    def test_rendered_manifest_is_valid_json(
        self, sandbox: tuple[Path, Path]
    ) -> None:
        home, prefix = sandbox
        result = _run_install(prefix=prefix, home=home)
        assert result.returncode == 0
        manifest = (
            home
            / ".config"
            / "google-chrome"
            / "NativeMessagingHosts"
            / "com.aaharonov.pwa_elh.json"
        )
        assert manifest.is_file()
        data = json.loads(manifest.read_text(encoding="utf-8"))
        assert data["name"] == "com.aaharonov.pwa_elh"
        assert data["type"] == "stdio"
        assert data["path"].endswith("/host.py")
        assert data["allowed_origins"] == [
            "chrome-extension://abcdefghijklmnopabcdefghijklmnop/"
        ]
        assert "description" in data

    def test_edge_manifest_has_edge_id(
        self, sandbox: tuple[Path, Path]
    ) -> None:
        home, prefix = sandbox
        result = _run_install(prefix=prefix, home=home)
        assert result.returncode == 0
        manifest = (
            home
            / ".config"
            / "microsoft-edge"
            / "NativeMessagingHosts"
            / "com.aaharonov.pwa_elh.json"
        )
        assert manifest.is_file()
        data = json.loads(manifest.read_text(encoding="utf-8"))
        assert data["allowed_origins"] == [
            "chrome-extension://ponmlkjihgfedcbaponmlkjihgfedcba/"
        ]

    def test_brave_manifest_has_chrome_id(
        self, sandbox: tuple[Path, Path]
    ) -> None:
        """Brave reuses Chrome Web Store distribution, so it gets the Chrome ID."""
        home, prefix = sandbox
        result = _run_install(prefix=prefix, home=home)
        assert result.returncode == 0
        manifest = (
            home
            / ".config"
            / "BraveSoftware"
            / "Brave-Browser"
            / "NativeMessagingHosts"
            / "com.aaharonov.pwa_elh.json"
        )
        assert manifest.is_file()
        data = json.loads(manifest.read_text(encoding="utf-8"))
        assert "abcdefghijklmnopabcdefghijklmnop" in data["allowed_origins"][0]

    def test_rerun_is_byte_identical(
        self, sandbox: tuple[Path, Path]
    ) -> None:
        """Rendering the manifest twice produces the same bytes."""
        home, prefix = sandbox
        assert _run_install(prefix=prefix, home=home).returncode == 0
        manifest = (
            home
            / ".config"
            / "google-chrome"
            / "NativeMessagingHosts"
            / "com.aaharonov.pwa_elh.json"
        )
        first = manifest.read_bytes()
        assert _run_install(prefix=prefix, home=home).returncode == 0
        second = manifest.read_bytes()
        assert first == second


class TestInstallerInputValidation:
    def test_rejects_invalid_chrome_id(
        self, sandbox: tuple[Path, Path]
    ) -> None:
        home, prefix = sandbox
        result = _run_install(
            prefix=prefix,
            home=home,
            chrome_id="not-a-valid-id",
        )
        assert result.returncode != 0
        assert "invalid" in result.stderr.lower()

    def test_rejects_unsafe_install_path(
        self, tmp_path: Path
    ) -> None:
        home = tmp_path / "home"
        home.mkdir()
        # A path containing a double-quote is rejected by the safe-charset
        # check used to keep heredoc-based manifest rendering safe.
        evil_prefix = tmp_path / 'evil"path'
        result = _run_install(prefix=evil_prefix, home=home)
        assert result.returncode != 0
        assert "unsafe" in result.stderr.lower() or "invalid" in result.stderr.lower()

    def test_accepts_placeholder_ids(
        self, sandbox: tuple[Path, Path]
    ) -> None:
        home, prefix = sandbox
        result = _run_install(
            prefix=prefix,
            home=home,
            chrome_id="REPLACE_WITH_CHROME_WEB_STORE_ID",
            edge_id="REPLACE_WITH_EDGE_ADDONS_ID",
        )
        assert result.returncode == 0, result.stderr


class TestInstallerUninstall:
    def test_uninstall_removes_manifest(
        self, sandbox: tuple[Path, Path]
    ) -> None:
        home, prefix = sandbox
        _run_install(prefix=prefix, home=home)
        manifest = (
            home
            / ".config"
            / "google-chrome"
            / "NativeMessagingHosts"
            / "com.aaharonov.pwa_elh.json"
        )
        assert manifest.is_file()
        result = _run_install(prefix=prefix, home=home, uninstall=True)
        assert result.returncode == 0
        assert not manifest.exists()


class TestSnapChromiumDetection:
    def test_snap_chromium_path_added_when_snap_dir_exists(
        self, sandbox: tuple[Path, Path]
    ) -> None:
        if sys.platform != "linux":
            pytest.skip("Snap-Chromium is Linux-only")
        home, prefix = sandbox
        (home / "snap" / "chromium").mkdir(parents=True)
        result = _run_install(prefix=prefix, home=home)
        assert result.returncode == 0
        snap_manifest = (
            home
            / "snap"
            / "chromium"
            / "common"
            / "chromium"
            / "NativeMessagingHosts"
            / "com.aaharonov.pwa_elh.json"
        )
        assert snap_manifest.is_file()

    def test_snap_chromium_path_skipped_when_dir_absent(
        self, sandbox: tuple[Path, Path]
    ) -> None:
        if sys.platform != "linux":
            pytest.skip("Snap-Chromium is Linux-only")
        home, prefix = sandbox
        result = _run_install(prefix=prefix, home=home)
        assert result.returncode == 0
        # The Snap directory must not have been created as an orphan.
        assert not (home / "snap").exists()

    def test_snap_chromium_orphan_manifest_cleaned_on_uninstall(
        self, sandbox: tuple[Path, Path]
    ) -> None:
        """If the user removes Snap-Chromium after install, the orphan
        manifest must still be cleaned up by `--uninstall`."""
        if sys.platform != "linux":
            pytest.skip("Snap-Chromium is Linux-only")
        home, prefix = sandbox
        (home / "snap" / "chromium").mkdir(parents=True)
        _run_install(prefix=prefix, home=home)
        snap_manifest = (
            home
            / "snap"
            / "chromium"
            / "common"
            / "chromium"
            / "NativeMessagingHosts"
            / "com.aaharonov.pwa_elh.json"
        )
        assert snap_manifest.is_file()
        # Simulate the user removing the Snap-Chromium app, leaving only
        # the NativeMessagingHosts dir and our manifest behind.
        keep = snap_manifest.parent.parent
        for child in list((home / "snap" / "chromium").iterdir()):
            if child != keep.parent:
                if child.is_dir():
                    shutil.rmtree(child)
                else:
                    child.unlink()
        result = _run_install(prefix=prefix, home=home, uninstall=True)
        assert result.returncode == 0
        assert not snap_manifest.exists()


class TestTildeExpansion:
    def test_tilde_in_prefix_is_expanded(
        self, tmp_path: Path
    ) -> None:
        """A literal leading ~/ in PWA_ELH_PREFIX is expanded to $HOME
        instead of failing the safe-charset check."""
        home = tmp_path / "home"
        home.mkdir()
        env = os.environ.copy()
        env["HOME"] = str(home)
        # Quoted tilde so bash does not pre-expand it.
        env["PWA_ELH_PREFIX"] = "~/install-target"
        env["CHROME_WEB_STORE_ID"] = "abcdefghijklmnopabcdefghijklmnop"
        env["EDGE_ADDONS_ID"] = "ponmlkjihgfedcbaponmlkjihgfedcba"
        result = subprocess.run(
            [str(_INSTALL_SH)],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        # Should resolve to $HOME/install-target, not a literal ~ dir.
        assert (home / "install-target" / "host.py").is_file()
        assert not (home / "~").exists()
