"""AWS Secrets Manager loader for signing keys.

At API boot we hydrate the process environment with secret values read
from SM (if available), letting every existing code path that reads
``os.environ["VALIDATOR_KEY_0"]`` keep working unchanged. If SM is
unreachable or the secrets aren't configured, we fall back silently to
whatever is already in the environment — this preserves the existing
dev/local workflow that keeps `.env.keys` files next to compose.

The hydration is one-way: we never write back to SM from here, and we
never log secret values.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass

logger = logging.getLogger(__name__)


# Map of (secret_id, field_in_secret_json) → env var to populate.
# Static set — always fetched on the API/validator host (never fails open
# unless the secret isn't configured).
_SECRET_FIELD_TO_ENV: tuple[tuple[str, str | None, str], ...] = (
    ("minotaur/production/validator-keys", "key_0", "VALIDATOR_KEY_0"),
    ("minotaur/production/validator-keys", "key_1", "VALIDATOR_KEY_1"),
    ("minotaur/production/validator-keys", "key_2", "VALIDATOR_KEY_2"),
    ("minotaur/production/relayer-key", "relayer_private_key", "RELAYER_PRIVATE_KEY"),
    # Shared Claude API key for miner agents (Phase 6.A). Raw string (no
    # JSON field) so field=None. Loaded on both API and miner containers
    # because the API doesn't use it today — dropping it there is cheap
    # and avoids surprise if an endpoint starts calling Claude later.
    ("minotaur/production/anthropic-api-key", None, "ANTHROPIC_API_KEY"),
)


# Miner secrets are loaded dynamically based on MINER_HOTKEY so each miner
# container only fetches its own key + PAT. Keeps the blast radius of a
# compromised miner to its own credentials.
_MINER_SECRETS: tuple[tuple[str, str, str], ...] = (
    # (secret_id_template, field, env_var)
    ("minotaur/production/miner-{hotkey}/key", "signing_private_key", "MINER_PRIVATE_KEY"),
    ("minotaur/production/miner-{hotkey}/github-pat", None, "SOLVER_REPO_TOKEN"),  # noqa: E501
)


@dataclass(frozen=True)
class _LoadOutcome:
    env_vars_set: int
    secrets_fetched: int
    secrets_failed: list[str]


def hydrate_env_from_secrets_manager(*, region: str | None = None) -> _LoadOutcome:
    """Pull configured secrets from SM and set them as env vars.

    Skips gracefully if boto3 is missing or SM is unreachable — the caller
    is expected to continue with whatever environment they already have.
    Env vars that are ALREADY set (non-empty) are preserved; SM fallback
    is only used to fill in gaps. That way an operator can still override
    via .env for debugging.
    """
    try:
        import boto3  # type: ignore
    except ImportError:
        logger.info("[secrets] boto3 not installed; skipping Secrets Manager load")
        return _LoadOutcome(0, 0, [])

    if os.environ.get("SKIP_SECRETS_MANAGER_LOAD", "0").strip().lower() in (
        "1", "true", "yes", "on",
    ):
        logger.info("[secrets] SKIP_SECRETS_MANAGER_LOAD=1; using env only")
        return _LoadOutcome(0, 0, [])

    # Group field lookups by secret_id so we fetch each secret once.
    by_secret: dict[str, list[tuple[str | None, str]]] = {}
    for secret_id, field, env_name in _SECRET_FIELD_TO_ENV:
        by_secret.setdefault(secret_id, []).append((field, env_name))

    # Add miner-specific secrets keyed on MINER_HOTKEY so each miner
    # container only fetches its own. MINER_HOTKEY is set in the compose
    # file (``alpha`` / ``charlie``).
    miner_hotkey = os.environ.get("MINER_HOTKEY", "").strip().lower()
    if miner_hotkey:
        for template_id, field, env_name in _MINER_SECRETS:
            secret_id = template_id.format(hotkey=miner_hotkey)
            by_secret.setdefault(secret_id, []).append((field, env_name))

    region = region or os.environ.get("AWS_REGION", "us-east-1")
    try:
        client = boto3.client("secretsmanager", region_name=region)
    except Exception as exc:
        logger.warning("[secrets] could not construct SM client: %s", exc)
        return _LoadOutcome(0, 0, [])

    env_vars_set = 0
    secrets_fetched = 0
    failed: list[str] = []

    for secret_id, fields in by_secret.items():
        try:
            resp = client.get_secret_value(SecretId=secret_id)
        except Exception as exc:
            logger.warning(
                "[secrets] get_secret_value(%s) failed: %s "
                "(expected on hosts without the IAM role)",
                secret_id, exc,
            )
            failed.append(secret_id)
            continue

        secrets_fetched += 1
        raw_string = resp.get("SecretString") or ""

        # field=None ⇒ the whole SecretString is the value (used for opaque
        # PATs stored as raw strings rather than JSON blobs).
        parsed: dict[str, object] | None = None
        if any(f is not None for f, _ in fields):
            try:
                parsed = json.loads(raw_string or "{}")
            except json.JSONDecodeError:
                logger.warning("[secrets] %s is not valid JSON — skipping", secret_id)
                failed.append(secret_id)
                continue

        for field, env_name in fields:
            current = os.environ.get(env_name, "").strip()
            if current:
                # Explicitly set already — respect operator override.
                logger.info(
                    "[secrets] %s already set in env, not overriding from %s",
                    env_name, secret_id,
                )
                continue
            if field is None:
                value = raw_string.strip()
            else:
                value = str((parsed or {}).get(field, "")).strip()
            if not value:
                logger.warning(
                    "[secrets] secret %s has no field %r",
                    secret_id, field,
                )
                continue
            os.environ[env_name] = value
            env_vars_set += 1
            logger.info("[secrets] hydrated %s from %s", env_name, secret_id)

    return _LoadOutcome(
        env_vars_set=env_vars_set,
        secrets_fetched=secrets_fetched,
        secrets_failed=failed,
    )
