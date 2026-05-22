"""
GitHub commit status emitter.

Posts a commit status to the GitHub Statuses API via gh CLI.
Reads GITHUB_REPOSITORY and GITHUB_SHA from environment.

Usage:
    emit_status.py --context ci/static-check --state pending \
        --description "Running..." --target-url URL
"""

import argparse
import json
import os
import subprocess
import sys


def emit_status(repo: str, sha: str, context: str, state: str, description: str, target_url: str) -> None:
    payload = json.dumps({
        "state": state,
        "context": context,
        "description": description[:140],
        "target_url": target_url,
    })
    result = subprocess.run(
        [
            "gh", "api", "--method", "POST",
            f"repos/{repo}/statuses/{sha}",
            "--input", "-",
        ],
        input=payload,
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print(f"Failed to post commit status: {result.stdout} {result.stderr}", file=sys.stderr)
        sys.exit(1)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--context", required=True)
    parser.add_argument("--state", required=True, choices=["pending", "success", "failure", "error"])
    parser.add_argument("--description", required=True)
    parser.add_argument("--target-url", default="")
    args = parser.parse_args()

    repo = os.environ.get("GITHUB_REPOSITORY", "")
    sha = os.environ.get("GITHUB_SHA", "")

    if not repo or not sha:
        print("GITHUB_REPOSITORY and GITHUB_SHA env vars are required", file=sys.stderr)
        sys.exit(1)

    emit_status(repo, sha, args.context, args.state, args.description, args.target_url)
    print(f"Status posted: {args.context} -> {args.state}")


if __name__ == "__main__":
    main()
