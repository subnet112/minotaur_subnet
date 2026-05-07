# Contributing

Thanks for your interest in Subnet 112 (Minotaur). This guide covers the basics — how to file an issue, how to run the tests locally, and what to expect from the review process.

## Reporting issues

- **Bugs**: open a GitHub issue with a minimal reproduction (commands run, expected vs. actual). Include version info — git commit hash, Python/Node/Foundry versions, OS.
- **Security vulnerabilities**: do **not** open a public issue. See [SECURITY.md](./SECURITY.md) for the responsible-disclosure path.
- **Feature requests / design discussion**: open an issue describing the use case and the constraint that motivates it. We're more likely to accept feature work that's grounded in a concrete user need than a pure idea.

## Pull requests

1. Fork the repo and create a topic branch off `main`.
2. Keep PRs focused — one logical change per PR. Drive-by formatting fixes belong in their own PR.
3. Run the test suite locally before pushing:
   
   ```bash
   make test          # unit + app tests (fast, no Docker)
   make test-forge    # Solidity contract tests (Foundry)
   make test-testnet  # full local_testnet smoke (Docker, slow)
   ```
4. Open the PR with a description that covers: what the change does, why it's needed, what was tested.
5. CI runs on every PR; failing checks block merge.

## Code style

- Match the surrounding code's style — formatting, naming conventions, comment density.
- New public APIs should have docstrings explaining the contract, not just the implementation.
- Don't add comments that narrate what the code is doing; reserve comments for *why* a decision was made when it's non-obvious.

## Sign-off

By submitting a PR you agree to license your contribution under the project's [LICENSE](./LICENSE) (MIT).

## Getting in touch

For day-to-day questions that don't fit a GitHub issue, the project lives in the broader [Subnet 112 (Minotaur)](https://github.com/subnet112/minotaur_subnet) ecosystem — see that repo's README for community channels.
