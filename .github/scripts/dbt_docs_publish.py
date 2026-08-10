"""dbt Docs release publish command builder.

Pure thin interface (CLAUDE.md functional-core rule): given whether the `dbt-docs-latest`
GitHub Release already exists, returns the ordered `gh` argv command sequence to publish
`static_index.html` to it. No I/O — the caller resolves release-existence (e.g. via
`gh release view RELEASE_TAG`) and executes the returned commands.

RELEASE_TAG and ASSET_NAME are exported so the caller's own existence-check command targets
the same literals this module publishes to, instead of duplicating the strings.

Public surface:
  build_release_publish_commands(release_exists)
  RELEASE_TAG
  ASSET_NAME
"""

RELEASE_TAG = "dbt-docs-latest"
ASSET_NAME = "static_index.html"


def build_release_publish_commands(release_exists: bool) -> list[list[str]]:
    """Return the gh command sequence to publish the dbt Docs release asset.

    When the release is absent, the release must be created before the asset can be
    uploaded to it, so the create command is returned first. When the release already
    exists, only the clobber-upload command runs.
    """
    upload_cmd = ["gh", "release", "upload", RELEASE_TAG, ASSET_NAME, "--clobber"]
    if release_exists:
        return [upload_cmd]

    create_cmd = ["gh", "release", "create", RELEASE_TAG, "--title", RELEASE_TAG, "--notes", ""]
    return [create_cmd, upload_cmd]
