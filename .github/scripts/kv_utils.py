"""
Azure Key Vault secret fetcher.

Authenticates via Azure CLI (pre-authenticated by azure/login OIDC step
using the KV UAMI). No credentials stored — GitHub OIDC issues a
short-lived token exchanged for an Azure access token by azure/login.

CLI commands:
  fetch-fabric           Fetches Fabric config (capacity ID) → writes to $GITHUB_ENV
  fetch-github-app       Fetches GitHub App config → writes to $GITHUB_ENV
  fetch-app-token-creds  Fetches App ID + PEM for actions/create-github-app-token → writes to $GITHUB_ENV

Required env var:
  AZURE_KEYVAULT_URL — Key Vault vault URI
"""

import argparse
import json
import os
import subprocess
import urllib.error
import urllib.request

KV_API_VERSION = "7.4"


def _get_kv_token() -> str:
    result = subprocess.run(
        ["az", "account", "get-access-token", "--resource", "https://vault.azure.net"],
        capture_output=True, text=True, check=True,
    )
    return json.loads(result.stdout)["accessToken"]


def get_secret(secret_name: str) -> str:
    """Fetch a single secret value from Key Vault by name."""
    vault_url = os.environ["AZURE_KEYVAULT_URL"].rstrip("/")
    token = _get_kv_token()
    url = f"{vault_url}/secrets/{secret_name}?api-version={KV_API_VERSION}"
    req = urllib.request.Request(url)
    req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read())["value"]
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        raise RuntimeError(f"Failed to fetch KV secret '{secret_name}': HTTP {e.code} — {body}") from e


def write_env(key: str, value: str):
    """Write a key=value pair to $GITHUB_ENV for use in subsequent steps."""
    env_file = os.environ.get("GITHUB_ENV")
    if env_file:
        with open(env_file, "a") as f:
            f.write(f"{key}={value}\n")
    else:
        print(f"[kv_utils] {key}={value}")


def write_env_multiline(key: str, value: str, delimiter: str = "EOF_KV_ML"):
    """Write a multiline value to $GITHUB_ENV using heredoc syntax."""
    env_file = os.environ.get("GITHUB_ENV")
    if env_file:
        with open(env_file, "a") as f:
            f.write(f"{key}<<{delimiter}\n{value}\n{delimiter}\n")
    else:
        print(f"[kv_utils] {key}=<multiline>")


def mask_value(value: str):
    """Instruct the GitHub Actions runner to mask this value in all log output."""
    print(f"::add-mask::{value}", flush=True)


def normalize_pem(pem: str) -> str:
    """Normalize a PEM string for Node.js createPrivateKey compatibility.

    Handles several Key Vault storage artefacts that cause ERR_OSSL_UNSUPPORTED:
    - UTF-8 BOM prepended to the value
    - Literal \\n escape sequences (backslash-n) instead of real newlines
    - CRLF or bare \\r line endings
    - Trailing whitespace on individual lines
    """
    pem = pem.lstrip("﻿")             # strip UTF-8 BOM
    pem = pem.replace("\\n", "\n")        # literal \n → real newlines
    pem = pem.replace("\r\n", "\n").replace("\r", "\n")  # CRLF / bare CR → LF
    pem = "\n".join(line.rstrip() for line in pem.split("\n"))  # strip trailing spaces per line
    return pem


def log_pem_info(pem: str) -> None:
    """Print non-sensitive PEM metadata to help diagnose format issues in CI logs."""
    lines = pem.splitlines()
    header = lines[0] if lines else "<empty>"
    print(f"[kv_utils] PEM header : {header}", flush=True)
    print(f"[kv_utils] PEM lines  : {len(lines)}  chars: {len(pem)}", flush=True)
    anomalies = {
        "BOM": pem.startswith("﻿"),
        "CRLF": "\r\n" in pem,
        "bare-CR": "\r" in pem,
        "literal-backslash-n": "\\n" in pem,
        "trailing-space": any(l != l.rstrip() for l in lines),
    }
    flagged = [k for k, v in anomalies.items() if v]
    if flagged:
        print(f"[kv_utils] PEM anomalies (pre-normalize): {', '.join(flagged)}", flush=True)
    else:
        print("[kv_utils] PEM anomalies (pre-normalize): none", flush=True)


def cmd_fetch_fabric():
    """Fetch Fabric capacity ID and write to GITHUB_ENV."""
    secret_name = os.environ.get("FABRIC_CAPACITY_ID_KV_NAME") or "vibedata-fabric-capacity-id"
    capacity_id = get_secret(secret_name)
    write_env("FABRIC_CAPACITY_ID", capacity_id)
    print("Fetched: FABRIC_CAPACITY_ID", flush=True)


def cmd_fetch_github_app():
    """Fetch GitHub App ID and installation ID and write to GITHUB_ENV.
    The PEM secret name is passed through as-is — the Fabric notebook
    fetches the actual PEM at runtime using its own KV access.
    """
    app_id_secret_name = os.environ.get("GH_APP_ID_KV_NAME") or "vibedata-github-app-id"
    install_id_secret_name = os.environ.get("GH_INSTALLATION_ID_KV_NAME") or "vibedata-github-installation-id"

    app_id = get_secret(app_id_secret_name)
    installation_id = get_secret(install_id_secret_name)

    write_env("GH_APP_ID_KV_NAME", app_id)
    write_env("GH_INSTALLATION_ID_KV_NAME", installation_id)
    print("Fetched: GH_APP_ID_KV_NAME, GH_INSTALLATION_ID_KV_NAME", flush=True)


def cmd_fetch_app_token_creds():
    """Fetch GitHub App ID and PEM for actions/create-github-app-token.

    Writes GH_APP_ID_VALUE (plain) and GH_APP_PEM_VALUE (multiline heredoc)
    to $GITHUB_ENV. Masks the PEM so it never appears in log output.

    Required env vars:
      GH_APP_ID_KV_NAME  — Key Vault secret name holding the App ID
      GH_APP_PEM_KV_NAME — Key Vault secret name holding the PEM
    """
    app_id_secret_name = os.environ.get("GH_APP_ID_KV_NAME", "vibedata-github-app-id")
    pem_secret_name = os.environ.get("GH_APP_PEM_KV_NAME", "vibedata-github-app-pem")

    app_id = get_secret(app_id_secret_name)
    raw_pem = get_secret(pem_secret_name)
    log_pem_info(raw_pem)
    pem = normalize_pem(raw_pem)

    mask_value(pem)
    write_env("GH_APP_ID_VALUE", app_id)
    write_env_multiline("GH_APP_PEM_VALUE", pem)
    print("Fetched: GH_APP_ID_VALUE, GH_APP_PEM_VALUE (PEM masked)", flush=True)


def main():
    parser = argparse.ArgumentParser(description="Fetch secrets from Azure Key Vault")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("fetch-fabric", help="Fetch Fabric capacity ID → GITHUB_ENV")
    sub.add_parser("fetch-github-app", help="Fetch GitHub App ID + installation ID → GITHUB_ENV")
    sub.add_parser("fetch-app-token-creds", help="Fetch App ID + PEM for create-github-app-token → GITHUB_ENV")
    args = parser.parse_args()

    if args.command == "fetch-fabric":
        cmd_fetch_fabric()
    elif args.command == "fetch-github-app":
        cmd_fetch_github_app()
    elif args.command == "fetch-app-token-creds":
        cmd_fetch_app_token_creds()


if __name__ == "__main__":
    main()
