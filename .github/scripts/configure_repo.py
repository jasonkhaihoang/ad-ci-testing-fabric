"""
Idempotent branch protection and label provisioning script.

Applies required status checks to a domain repo's main branch via gh api PUT,
and upserts CI-required labels via gh label create --force.
Re-running against an already-configured repo is safe and exits 0.

Usage:
    configure_repo.py --repo <owner>/<repo> [--branch main] [--skip-labels]
"""

import argparse
import json
import subprocess
import sys


FABRIC_LABELS = []

_FABRIC_CONTEXTS = [
    "ci/preflight",
    "ci/provision",
    "ci/static-check",
    "ci/state-modified+",
    "ci/design-drift",
    "ci/run",
    "ci/unit-tests",
    "ci/data-tests",
]

_MOTHERDUCK_CONTEXTS = [
    "ci/preflight",
    "ci/static-check",
    "ci/state-modified+",
    "ci/design-drift",
    "ci/run",
    "ci/unit-tests",
    "ci/data-tests",
]


def build_payload(platform: str = "fabric") -> dict:
    if platform == "fabric":
        contexts = _FABRIC_CONTEXTS
    elif platform == "motherduck":
        contexts = _MOTHERDUCK_CONTEXTS
    else:
        raise ValueError(f"unknown platform: {platform}")
    return {
        "required_status_checks": {"strict": True, "contexts": contexts},
        "enforce_admins": False,
        "required_pull_request_reviews": None,
        "restrictions": None,
    }


def configure_repo(repo: str, branch: str, platform: str = "fabric") -> None:
    payload = json.dumps(build_payload(platform=platform))
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


def configure_labels(repo: str, platform: str = "fabric") -> None:
    """Upsert CI-required labels into the domain repo."""
    if platform != "fabric":
        return
    for label in FABRIC_LABELS:
        subprocess.run(
            [
                "gh", "label", "create", label["name"],
                "--repo", repo,
                "--color", label["color"],
                "--description", label["description"],
                "--force",
            ],
            check=True,
        )
        print(f"Label upserted: {label['name']} ({repo})")


def main():
    parser = argparse.ArgumentParser(
        description="Apply required status checks and labels to a domain repo."
    )
    parser.add_argument("--repo", required=True, metavar="OWNER/REPO")
    parser.add_argument("--branch", default="main")
    parser.add_argument("--platform", default="fabric", choices=["fabric", "motherduck"])
    parser.add_argument("--skip-labels", action="store_true",
                        help="Skip label provisioning (use when caller lacks issues:write scope)")
    args = parser.parse_args()
    configure_repo(args.repo, args.branch, platform=args.platform)
    if not args.skip_labels:
        configure_labels(args.repo, platform=args.platform)


if __name__ == "__main__":
    main()
