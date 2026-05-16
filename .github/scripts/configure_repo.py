"""
Idempotent branch protection provisioning script.

Applies required status checks to a domain repo's main branch via gh api PUT.
Re-running against an already-configured repo is safe and exits 0.

Usage:
    configure_repo.py --repo <owner>/<repo> [--branch main]
"""

import argparse
import json
import subprocess
import sys


def build_payload() -> dict:
    return {
        "required_status_checks": {
            "strict": True,
            "contexts": [
                "ci/preflight",
                "ci/provision",
                "ci/static-check",
                "ci/state-modified+",
                "ci/run",
                "ci/unit-tests",
                "ci/data-tests",
                "ci/data-diff",
            ],
        },
        "enforce_admins": False,
        "required_pull_request_reviews": None,
        "restrictions": None,
    }


def configure_repo(repo: str, branch: str) -> None:
    payload = json.dumps(build_payload())
    try:
        result = subprocess.run(
            [
                "gh", "api", "--method", "PUT",
                f"/repos/{repo}/branches/{branch}/protection",
                "--input", "-",
            ],
            input=payload,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        print("gh: command not found. Install the GitHub CLI: https://cli.github.com", file=sys.stderr)
        sys.exit(1)

    if result.returncode != 0:
        stderr = result.stderr
        if "403" in stderr or "Forbidden" in stderr or "admin rights" in stderr:
            print(f"{repo}: insufficient permissions (requires repo admin + `repo` scope or `administration:write`)", file=sys.stderr)
        elif "404" in stderr or "Not Found" in stderr:
            print(f"repository not found: {repo}", file=sys.stderr)
        else:
            print(stderr, file=sys.stderr)
        sys.exit(1)

    print(f"Branch protection applied: {repo}:{branch}")


def main():
    parser = argparse.ArgumentParser(
        description="Apply required status checks to a domain repo branch."
    )
    parser.add_argument("--repo", required=True, metavar="OWNER/REPO")
    parser.add_argument("--branch", default="main")
    args = parser.parse_args()
    configure_repo(args.repo, args.branch)


if __name__ == "__main__":
    main()
