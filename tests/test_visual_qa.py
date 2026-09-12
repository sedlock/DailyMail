"""Browser-based rendering QA. Opt-in, and deliberately outside the normal run.

    DAILYMAIL_VISUAL_QA=1 uv run pytest tests/test_visual_qa.py -v

Two reasons this is not part of the hermetic suite. It launches a real browser,
which the other 900-odd tests must never need; and the production path has no
browser dependency and must not acquire one, so keeping the only browser code
behind an explicit flag is what makes that separation checkable rather than
merely intended.

What it protects is the class of defect a string assertion cannot see. On
1 September 2026 the digest's HTML was valid, its sanitizer was working and its
tests were green, and the reader still got an announcement rendered entirely in
the headline colour -- because the defect was in what the markup *computed to*
after inheritance, in a narrow viewport, under Outlook's dark-mode inversion.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
QA_SCRIPT = REPO / "tools" / "qa" / "visual-regression.mjs"
PAGE_BUILDER = REPO / "tools" / "qa" / "build_page.py"
FIXTURE = REPO / "artifacts" / "qa" / "fixtures" / "render-regression.json"

ENABLED = os.environ.get("DAILYMAIL_VISUAL_QA") == "1"

pytestmark = pytest.mark.skipif(
    not ENABLED,
    reason="browser QA is opt-in: set DAILYMAIL_VISUAL_QA=1",
)


@pytest.fixture
def qa_page(tmp_path, settings_obj):
    """Build the regression page from committed fixtures, in a scratch home.

    `settings_obj` is requested so the builder finds a config file; `conftest`'s
    autouse isolation is what redirects XDG data and state, so the scratch
    database it writes never touches the production one.
    """
    out = tmp_path / "qa-page"
    result = subprocess.run(
        [sys.executable, str(PAGE_BUILDER), "--out", str(out)],
        capture_output=True,
        text=True,
        cwd=str(REPO),
        env={**os.environ, "PYTHONPATH": str(REPO / "src")},
        timeout=300,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return out


def test_the_fixture_set_covers_every_reported_rendering_case():
    """The fixtures are the regression; losing one silently would be the defect."""
    doc = json.loads(FIXTURE.read_text(encoding="utf-8"))
    kinds = {entry["kind"] for entry in doc["announcements"]}
    assert kinds == {
        "color-leak",
        "color-control",
        "multi-session",
        "single-session-control",
    }
    by_id = {str(entry["submission_id"]): entry for entry in doc["announcements"]}
    # The exact production markup that caused the peach card.
    assert "color:rgb(90,19,0)" in by_id["6612"]["full_body"]
    # The control carries no colour declaration at all.
    assert "color:" not in by_id["6736"]["full_body"]
    # No contact block, and no address that could identify a person.
    for entry in doc["announcements"]:
        assert "contact_email" not in entry
        assert "@rowan.edu" not in entry["full_body"]


def test_browser_invariants_hold_at_every_viewport(qa_page, tmp_path):
    """The whole Playwright suite, as one pass/fail with its output attached."""
    node = shutil.which("node")
    assert node, "node is required for browser QA"
    result = subprocess.run(
        [
            node,
            str(QA_SCRIPT),
            "--page",
            str(qa_page),
            "--screenshots",
            str(tmp_path / "screenshots"),
        ],
        capture_output=True,
        text=True,
        cwd=str(REPO),
        timeout=600,
    )
    output = result.stdout + result.stderr
    if result.returncode == 2:
        pytest.skip(f"Playwright is not installed on this host:\n{output}")
    assert result.returncode == 0, output
    assert "VISUAL QA OK" in output
    # Every viewport must actually have been exercised.
    for width in ("390x844", "430x932", "1280x900"):
        assert width in output or "invariant(s) held" in output


def test_the_qa_suite_fails_when_the_colour_policy_is_disabled(
    tmp_path, settings_obj
):
    """A regression check that cannot fail is not a regression check.

    Rebuilds the page with the render-time colour policy switched off -- which is
    exactly the state production was in on 1 September -- and requires the
    browser suite to reject it. Like every test here it inherits `conftest`'s
    autouse XDG isolation, so the rebuild writes to a scratch database.
    """
    node = shutil.which("node")
    assert node, "node is required for browser QA"

    builder = (
        "import sys, dataclasses;"
        f"sys.path.insert(0, {str(REPO / 'src')!r});"
        f"sys.path.insert(0, {str(REPO / 'tools' / 'qa')!r});"
        "from dailymail import render;"
        "render.BODY_COLOR_POLICY = dataclasses.replace("
        "render.BODY_COLOR_POLICY, strip_source_colors=False);"
        "import build_page;"
        f"build_page.build(__import__('pathlib').Path({str(tmp_path / 'page')!r}))"
    )
    built = subprocess.run(
        [sys.executable, "-c", builder],
        capture_output=True, text=True, cwd=str(REPO), timeout=300,
    )
    assert built.returncode == 0, built.stdout + built.stderr

    result = subprocess.run(
        [node, str(QA_SCRIPT), "--page", str(tmp_path / "page"), "--screenshots-off"],
        capture_output=True, text=True, cwd=str(REPO), timeout=600,
    )
    output = result.stdout + result.stderr
    if result.returncode == 2:
        pytest.skip(f"Playwright is not installed on this host:\n{output}")
    assert result.returncode == 1, f"the QA suite accepted the known defect:\n{output}"
    # The exact colour production shipped.
    assert "rgb(90, 19, 0)" in output
