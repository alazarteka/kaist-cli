# Release Workflow

This project ships managed standalone CLI bundles through GitHub Releases.

## Asset Contract

Each release tag (for example `v0.2.0`) publishes:

- `kaist-v0.2.0-darwin-arm64.tar.gz`
- `kaist-v0.2.0-darwin-x86_64.tar.gz`
- `kaist-v0.2.0-linux-x86_64-gnu.tar.gz`
- `checksums.txt`

The archive must unpack to:

- `bundle.json`
- `bin/kaist` or an onedir runtime tree whose executable is addressed by `bundle.json.binary_relpath`
- `skills/kaist-cli/SKILL.md`
- `skills/kaist-cli/agents/openai.yaml`

`bundle.json` must include:

- `version`
- `repo`
- `target`
- `binary_relpath`
- `skill_relpath`

`checksums.txt` must include the SHA256 digest for the archive.

## CI Workflow

The GitHub Actions workflow at `.github/workflows/release.yml`:

1. Builds `kaist` with PyInstaller `onedir` on `macos-14` for Apple silicon, `macos-15-intel` for Intel, and `ubuntu-22.04` for Linux glibc.
2. Calls `scripts/build_release_bundle.sh` to package one managed bundle archive per target.
3. Generates a combined `checksums.txt` covering all release archives.
4. Uploads all archives plus `checksums.txt` to the GitHub release for the tag.

## Installer Layout

`install.sh` installs releases into:

- `~/.local/share/kaist-cli/versions/vX.Y.Z/`
- `~/.local/share/kaist-cli/current`
- `~/.local/share/kaist-cli/previous`
- `~/.local/bin/kaist`

Linux target detection:

- macOS installs auto-detect `darwin-arm64` or `darwin-x86_64`
- Linux installs auto-detect `linux-x86_64-gnu` on glibc hosts
- musl/Alpine is rejected explicitly

Retention policy:

- keep `current`
- keep `previous`
- prune everything older after successful install or update

The bundled agent skill lives at:

- `~/.local/share/kaist-cli/current/skills/kaist-cli`

## Update Command Requirements

`kaist update` expects:

- a standalone binary runtime (`sys.frozen == True`)
- a managed install created by `install.sh`
- releases published to `alazarteka/kaist-cli`
- asset names and bundle layout matching the contract above
- self-update is available on the published macOS and Linux glibc standalone bundles
