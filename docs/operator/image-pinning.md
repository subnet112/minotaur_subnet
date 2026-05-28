# Image pinning and verification (operator guide)

The validator image is published to GHCR under
`ghcr.io/subnet112/minotaur-validator`. Tags:

- `:stable` — operator-facing rolling tag, built from the **`main`**
  branch. `main` only advances via a verified merge from `develop`
  (after the subnet team has soaked the build on its own validator),
  so `:stable` is always vetted. **This is the tag third-party
  validators should track.**
- `:latest` — built from the **`develop`** branch (the integration
  line). May contain unstable changes; runs on the subnet team's own
  validator for verification. Not recommended for production.
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

---

## Pinning by digest (defense-in-depth against tag mutation)

## Why `:stable` is not a security boundary

The default `MINOTAUR_IMAGE_TAG=stable` in `platform/validator/docker-compose.yml`
is a **mutable tag**. The subnet team can — and routinely does — re-point
`:stable` at a new image SHA when a release is promoted. That's fine for
the auto-update flow Watchtower runs, but it means:

- Anyone who can push to `ghcr.io/subnet112/minotaur-validator` can change
  what your validator runs without your validator's image ID changing on
  paper.
- A compromised GHCR token at the subnet org could swap `:stable` for a
  malicious image; Watchtower would pull and run it within one poll cycle
  (default 1 hour).
- Forensics after an incident are harder: `docker inspect` shows the tag,
  not the SHA of the image you booted three weeks ago.

Pinning by digest closes all three.

## How to pin

In your `.env` (in the same directory as `docker-compose.yml`):

```bash
# Look up the current digest of :stable
docker manifest inspect ghcr.io/subnet112/minotaur-validator:stable \
  | jq -r '.manifests[] | select(.platform.architecture=="amd64") | .digest'
# → sha256:abcdef0123456789...

# Pin in .env
MINOTAUR_IMAGE_TAG=stable@sha256:abcdef0123456789...
```

Then `docker compose up -d` will pull and lock to exactly that image.
The compose `image:` line is already templated as
`${MINOTAUR_IMAGE_TAG:-stable}`, so this Just Works.

## What about Watchtower

When `MINOTAUR_IMAGE_TAG` includes a digest, Watchtower **stops auto-
updating**: the digest is the version, and only an explicit `.env` edit
+ `docker compose up -d` will move it. That is the intended behaviour
for security-conscious operators. If you want Watchtower back on, drop
the digest and go back to `MINOTAUR_IMAGE_TAG=stable`.

## Verifying against the cosign signature (after PR-5)

Once PR-5 lands, every `:stable` image is signed by the subnet team's
cosign key and the signature is attached to the GHCR registry. To
verify before pulling:

```bash
cosign verify \
  --certificate-identity-regexp 'https://github.com/subnet112/minotaur_subnet/\.github/workflows/.*' \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com \
  ghcr.io/subnet112/minotaur-validator:stable
```

A successful verify prints the signed digest. Cross-check that against
the digest you're about to pin. Mismatch → do not deploy; raise on the
subnet-team operations channel.

## Updating to a new release

1. Subnet team announces a new `:stable` digest on the operations
   channel.
2. You: `docker manifest inspect ghcr.io/subnet112/minotaur-validator:stable`
   and confirm the digest matches the announcement.
3. (Recommended) `cosign verify` (see above).
4. Edit `.env`, replace the digest portion of `MINOTAUR_IMAGE_TAG`.
5. `docker compose pull validator api && docker compose up -d validator api`.
6. `docker compose logs -f validator api` until both pass healthcheck.

## Related audit findings

- F-20 (mutable base tag) — addressed by digest-pinning the base image
  in `minotaur_subnet/Dockerfile`.
- H10 (Watchtower auto-pull amplifies a registry compromise) —
  addressed by this doc + by digest-pinning Watchtower itself in
  `docker-compose.yml`.
- H11 (operator can't tell which image was running last week) —
  addressed by encouraging digest-pinned `.env` files committed to the
  operator's own private infra repo.
