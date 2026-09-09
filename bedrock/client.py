"""Thin Bedrock client.

Phases ask for a **tier**, never a model id. Which model serves a tier is
decided by `models.json`, so swapping models is a config edit and no phase code
changes.

The one non-obvious thing this encapsulates: in `ap-south-1` most current models
have no on-demand throughput under their bare `anthropic.*` / `amazon.*` id and
must be called through a cross-region **inference profile** id instead. Getting
that wrong returns a `ValidationException` that reads like a permissions
problem but is a routing problem. See docs/05-aws-services.md.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

log = logging.getLogger("bedrock.client")

CONFIG_PATH = Path(__file__).resolve().parent / "models.json"

ANTHROPIC_VERSION = "bedrock-2023-05-31"


class BedrockError(RuntimeError):
    pass


class ModelUnavailable(BedrockError):
    """The model is not entitled on this account -- retrying will not help."""


def load_config(path: Path = CONFIG_PATH) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def resolve(tier: str, config: dict | None = None) -> dict:
    """Map a tier name to its binding. Raises rather than silently defaulting --
    a typo'd tier should fail loudly, not quietly bill for the wrong model."""
    config = config or load_config()
    tiers = config["tiers"]
    if tier not in tiers:
        raise BedrockError(f"unknown tier {tier!r}; configured: {sorted(tiers)}")
    binding = dict(tiers[tier])
    binding["region"] = config["region"]
    binding["tier"] = tier
    return binding


class BedrockClient:
    def __init__(self, config: dict | None = None, session=None):
        self.config = config or load_config()
        self.region = self.config["region"]
        self._session = session
        self._runtime = None

    @property
    def runtime(self):
        if self._runtime is None:
            import boto3

            session = self._session or boto3.Session()
            self._runtime = session.client("bedrock-runtime", region_name=self.region)
        return self._runtime

    def complete(
        self,
        tier: str,
        prompt: str,
        *,
        max_tokens: int = 1024,
        system: str | None = None,
        temperature: float | None = None,
    ) -> dict:
        """One turn against a tier. Returns the parsed response plus token usage,
        because cost per run is a first-class concern in this project."""
        binding = resolve(tier, self.config)
        body: dict[str, Any] = {
            "anthropic_version": ANTHROPIC_VERSION,
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": prompt}],
        }
        if system:
            body["system"] = system
        if temperature is not None:
            body["temperature"] = temperature

        try:
            response = self.runtime.invoke_model(
                modelId=binding["model_id"],
                body=json.dumps(body),
                contentType="application/json",
                accept="application/json",
            )
        except Exception as exc:  # noqa: BLE001 -- re-raised with diagnosis below
            raise self._diagnose(exc, binding) from exc

        payload = json.loads(response["body"].read())
        usage = payload.get("usage", {})
        text = "".join(
            block.get("text", "") for block in payload.get("content", []) if block.get("type") == "text"
        )
        log.info(
            "tier=%s model=%s in=%s out=%s",
            tier,
            binding["model_id"],
            usage.get("input_tokens"),
            usage.get("output_tokens"),
        )
        return {
            "tier": tier,
            "model_id": binding["model_id"],
            "text": text,
            "stop_reason": payload.get("stop_reason"),
            "input_tokens": usage.get("input_tokens"),
            "output_tokens": usage.get("output_tokens"),
        }

    def _diagnose(self, exc: Exception, binding: dict) -> BedrockError:
        """Turn Bedrock's two confusable failures into distinct, actionable errors."""
        message = str(exc)

        if "on-demand throughput isn" in message:
            return BedrockError(
                f"{binding['model_id']} was called as a bare model id but {self.region} "
                "has no on-demand throughput for it. Use the inference profile id "
                "(apac.* or global.*) instead -- run "
                "`aws bedrock list-inference-profiles` to find it. This is a routing "
                "error, not a permissions error."
            )
        if "is not available for this account" in message:
            return ModelUnavailable(
                f"{binding['model_id']} is not entitled on this account. Retrying and "
                "switching to its inference profile will both fail. Pick another model "
                "in models.json or raise it with AWS Sales."
            )
        if "AccessDeniedException" in message:
            return BedrockError(
                f"Access denied invoking {binding['model_id']}. Check model access is "
                f"enabled for {self.region} in the Bedrock console -- "
                "bedrock:InvokeModel in IAM is not the same as model access."
            )
        return BedrockError(f"invoke failed for {binding['model_id']}: {message}")
