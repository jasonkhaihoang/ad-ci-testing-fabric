"""Marker-based PR comment upsert.

Single owner of the `gh api` / `gh pr comment` shell calls and the tempfile
dance shared by every CI gate that posts a sticky comment. Marker constants
live with their renderers; this module is purely transport.

Public surface:
    find_by_marker(marker, pr_number, repo) -> str | None
    upsert(marker, body, pr_number, repo) -> None
"""

import os
import subprocess
import sys
import tempfile


def find_by_marker(marker: str, pr_number: str, repo: str) -> str | None:
    """Return the id of the first PR comment containing `marker`, or None."""
    result = subprocess.run(
        ["gh", "api", f"repos/{repo}/issues/{pr_number}/comments",
         "--jq", f'.[] | select(.body | contains("{marker}")) | .id'],
        capture_output=True, text=True,
    )
    stdout = result.stdout.strip()
    return stdout.splitlines()[0] if stdout else None


def upsert(marker: str, body: str, pr_number: str, repo: str) -> None:
    """Create or update the PR comment identified by `marker`.

    Exits the process with non-zero status if the underlying `gh` call fails.
    """
    comment_id = find_by_marker(marker, pr_number, repo)

    with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as tmp:
        tmp.write(body)
        tmp_path = tmp.name

    try:
        if comment_id:
            result = subprocess.run(
                ["gh", "api", "--method", "PATCH",
                 f"repos/{repo}/issues/comments/{comment_id}",
                 "--field", f"body=@{tmp_path}"],
                capture_output=True, text=True,
            )
        else:
            result = subprocess.run(
                ["gh", "pr", "comment", pr_number,
                 "--repo", repo,
                 "--body-file", tmp_path],
                capture_output=True, text=True,
            )
        if result.returncode != 0:
            print(f"Failed to post PR comment: {result.stderr}", file=sys.stderr)
            sys.exit(1)
    finally:
        os.unlink(tmp_path)
