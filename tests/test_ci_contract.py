"""Regression tests for the repository's mandatory CI/release quality contract."""

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _workflow_text(name: str) -> str:
    return (ROOT / ".github" / "workflows" / name).read_text(encoding="utf-8")


def test_push_ci_is_not_limited_to_historical_feature_branches():
    for workflow in ("ci.yml", "gui-browser.yml"):
        text = _workflow_text(workflow)
        push_section = text.split("  push:\n", 1)[1].split("  workflow_dispatch:", 1)[0]
        assert "branches:" not in push_section


def test_quality_job_uses_shared_preflight_contract():
    text = _workflow_text("ci.yml")

    assert "python -m pip install -e '.[test,quality]'" in text
    assert "python tools/preflight.py" in text
    # Architecture, Ruff, compilation, and focused pytest live in the shared
    # runner so local and CI command lists cannot diverge independently.
    assert "run: ruff check src tests tools" not in text
    assert "run: python tools/check_architecture.py" not in text


def test_test_jobs_use_module_invocation_and_do_not_duplicate_template_suite():
    text = _workflow_text("ci.yml")

    assert "python -m pytest -q --ignore=tests/test_gui_browser.py" in text
    assert "python -m pytest -q tests/test_installable_control_plane.py" in text
    assert "generated-templates:" not in text


def test_browser_workflow_installs_browser_and_uses_module_pytest():
    text = _workflow_text("gui-browser.yml")

    assert "python -m playwright install --with-deps chromium" in text
    assert "python -m pytest -q" in text


def test_release_workflow_builds_smoke_tests_and_publishes_tagged_artifacts():
    text = _workflow_text("release.yml")

    assert 'tags:\n      - "v*"' in text
    assert "python -m build" in text
    assert "sha256sum dist/* > dist/SHA256SUMS" in text
    assert "/tmp/execraft-release/bin/execraft --help" in text
    assert "/tmp/execraft-release/bin/execraft doctor" in text
    assert "softprops/action-gh-release@v2" in text
    assert "dist/*.whl" in text
    assert "dist/*.tar.gz" in text
    assert "dist/SHA256SUMS" in text
