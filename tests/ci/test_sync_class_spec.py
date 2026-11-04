"""Contract for the fork sync-class document."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SPEC_PATH = REPO_ROOT / "docs" / "fork-sync-classes.json"


def load_sync_classes(text: str) -> dict:
    """Parse a sync-class document and require a version-1 object."""
    loaded = json.loads(text)
    if not isinstance(loaded, dict) or loaded.get("version") != 1:
        raise ValueError("sync class document must be a dict with version 1")
    return loaded


def test_fork_sync_classes_declares_version_one_classes() -> None:
    document = load_sync_classes(SPEC_PATH.read_text(encoding="utf-8"))
    assert document["version"] == 1
    assert document["generated_unspecified"]["paths"] == [
        "uv.lock",
        "pm/uv.lock",
        "pm/lock.json",
        "flake.lock",
        "nix/pythonLock.nix",
        "nix/npm-pinned.nix",
    ]
    assert document["build_output"]["paths"] == [
        "plugins/kanban/dashboard/dist/index.js",
        "plugins/kanban/dashboard/dist/style.css",
    ]
    assert document["build_output"]["generator"] is None
    assert document["auth_hold"]["paths"] == [
        ".github/workflows/README.md",
        ".github/workflows/archive-inputs.yml",
        ".github/workflows/blob-size-check.yml",
        ".github/workflows/bootstrap-installer-build.yml",
        ".github/workflows/bootstrap-installer.yml",
        ".github/workflows/canary-release.yml",
        ".github/workflows/case-collision-check.yml",
        ".github/workflows/catalog-tests.yml",
        ".github/workflows/ci-review-comment.yml",
        ".github/workflows/ci.yaml",
        ".github/workflows/contributor-check.yml",
        ".github/workflows/deploy-site.yml",
        ".github/workflows/desktop-bundle-smoke.yml",
        ".github/workflows/desktop-bundled-release.yml",
        ".github/workflows/docker-lint.yml",
        ".github/workflows/docker.yml",
        ".github/workflows/docs-search-monitor.yml",
        ".github/workflows/docs-site-checks.yml",
        ".github/workflows/e2e-desktop-core.yml",
        ".github/workflows/e2e-desktop-update.yml",
        ".github/workflows/e2e-desktop.yml",
        ".github/workflows/history-check.yml",
        ".github/workflows/icons-freshness-check.yml",
        ".github/workflows/infographic-check.yml",
        ".github/workflows/install-e2e-macos-run.yml",
        ".github/workflows/install-e2e-red.yml",
        ".github/workflows/install-e2e-run.yml",
        ".github/workflows/install-e2e-windows-run.yml",
        ".github/workflows/install-e2e.yml",
        ".github/workflows/js-autofix.yml",
        ".github/workflows/js-tests.yml",
        ".github/workflows/label-rerun.yml",
        ".github/workflows/lazy-deps-guard.yml",
        ".github/workflows/lint.yml",
        ".github/workflows/live-providers.yml",
        ".github/workflows/lockfile-diff.yml",
        ".github/workflows/nix.yml",
        ".github/workflows/osv-scanner.yml",
        ".github/workflows/plugin-catalog-ci.yml",
        ".github/workflows/pm-bundle.yml",
        ".github/workflows/profile-artifact-check.yml",
        ".github/workflows/prompt-surface-diff.yml",
        ".github/workflows/remote-backends.yml",
        ".github/workflows/rust-tests.yml",
        ".github/workflows/sandbox-image.yml",
        ".github/workflows/skills-index-freshness.yml",
        ".github/workflows/skills-index.yml",
        ".github/workflows/stable-release-publication.yml",
        ".github/workflows/stable-release.yml",
        ".github/workflows/supply-chain-audit.yml",
        ".github/workflows/termux-verify.yml",
        ".github/workflows/tests-os.yml",
        ".github/workflows/tests.yml",
        ".github/workflows/uv-lockfile-check.yml",
        ".github/workflows/windows-bundle-sdk.yml",
        ".github/workflows/windows-desktop-attach-e2e.yml",
        ".github/workflows/windows-install-update-e2e.yml",
        ".github/workflows/windows-venv-e2e.yml",
    ]
    assert document["auth_hold"]["ledger_entry"] == "G-ORIGIN-WORKFLOWS"
    assert document["auth_hold"]["reason"] == "token cannot update workflow files"
    assert document["structured_pin_contract"] == [
        {
            "manifest": "apps/desktop/package.json",
            "package": "electron-builder",
            "field": "devDependencies",
            "constants": [
                "apps/desktop/scripts/builder-arch-targets.mjs",
                "apps/desktop/scripts/builder-config-26.mjs",
            ],
        }
    ]


def test_load_sync_classes_rejects_version_two(tmp_path: Path) -> None:
    document_path = tmp_path / "fork-sync-classes.json"
    document_path.write_text(
        json.dumps({"version": 2}),
        encoding="utf-8",
    )
    with pytest.raises(ValueError):
        load_sync_classes(document_path.read_text(encoding="utf-8"))
