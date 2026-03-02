# Release Workflow

This project ships standalone unsigned CLI binaries through GitHub Releases.

## Asset Contract

Each release tag (for example `v0.2.0`) should publish:

- `kaist-v0.2.0-darwin-arm64.tar.gz`
- `checksums.txt`

`checksums.txt` must include SHA256 entries for each release archive.

## CI Workflow

The GitHub Actions workflow at `.github/workflows/release.yml`:

1. Builds `kaist` with PyInstaller on `macos-14`.
2. Packages the binary into `kaist-<tag>-darwin-arm64.tar.gz`.
3. Generates `checksums.txt`.
4. Uploads assets to the GitHub release for the tag.

## Update Command Requirements

`kaist update` expects:

- A standalone binary runtime (`sys.frozen == True`).
- Releases published to `alazarteka/kaist-cli`.
- Asset names matching the contract above.
