# Image pinning and verification (operator guide)

The validator image is published to GHCR under
`ghcr.io/subnet112/minotaur-validator`. Tags:

- `:stable` — operator-facing rolling tag. Moved by a gated workflow
  (`promote-stable.yml`) after manual maintainer approval. **This is
  the tag third-party validators should track.**
- `:latest` — latest commit on `main`. May contain unstable changes.
  Not recommended for production.
- `:sha-<short>` — immutable per-commit tag. Useful for pinning to a
  specific build during an investigation.
- `:vX.Y.Z` — immutable release tag pushed alongside git tags.

## Verifying image signatures (cosign)

Every image pushed by CI is signed with [cosign](https://docs.sigstore.dev/)
keyless signing via GitHub Actions OIDC. Operators should verify the
signature **before** pulling and running the image on a production
validator. This guarantees the manifest you pulled was built by this
repository's CI workflow and was not pushed by an attacker who
compromised a GHCR credential.

Install cosign:

```bash
# macOS
brew install cosign

# Linux (binary release)
curl -sSLO https://github.com/sigstore/cosign/releases/latest/download/cosign-linux-amd64
sudo install -m 0755 cosign-linux-amd64 /usr/local/bin/cosign
```

Verify the `:stable` tag:

```bash
cosign verify ghcr.io/subnet112/minotaur-validator:stable \
  --certificate-identity-regexp '^https://github.com/subnet112/minotaur_subnet/' \
  --certificate-oidc-issuer 'https://token.actions.githubusercontent.com'
```

A successful verification prints the signed payload (digest, claims,
certificate identity). A failed verification exits non-zero — **do
not pull the image** in that case; report it to the maintainers.

You can verify any tag the same way (replace `:stable` with `:sha-...`
or `:vX.Y.Z`).

## SBOM

A CycloneDX SBOM is generated for every push and uploaded as a
workflow artifact on the `Build and Publish Validator Image` run. To
fetch it for a given commit:

```bash
gh run download <run-id> --repo subnet112/minotaur_subnet \
  --name sbom-cyclonedx-json
```

## CVE scanning (deferred follow-up)

A Trivy HIGH/CRITICAL CVE scan with `exit-code: '1'` will land in a
follow-up PR once the current image's baseline CVEs have been
triaged into a `.trivyignore` policy or upgraded away. Until then,
operators who want to scan can run Trivy themselves against the
published digest:

```bash
trivy image --severity HIGH,CRITICAL \
  ghcr.io/subnet112/minotaur-validator:stable
```
