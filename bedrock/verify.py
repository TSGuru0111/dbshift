"""Verify every configured tier actually invokes.

Run this after any change to models.json, and on any new account or region.
Catalogue presence proves nothing -- only a real invoke does.

    python -m bedrock.verify
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    __package__ = "bedrock"

from .client import BedrockClient, BedrockError, load_config

PROBE = "Reply with exactly: OK"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify Bedrock tier bindings")
    parser.add_argument("--include-alternates", action="store_true")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.WARNING if args.quiet else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s %(message)s",
    )

    config = load_config()
    client = BedrockClient(config)

    targets = [(name, b) for name, b in config["tiers"].items()]
    if args.include_alternates:
        targets += [(f"alt:{n}", b) for n, b in config.get("alternates", {}).items()]

    print(f"region: {config['region']}\n")
    failures = 0
    for name, binding in targets:
        model_id = binding["model_id"]
        try:
            result = client.complete(
                name.replace("alt:", ""), PROBE, max_tokens=16
            ) if not name.startswith("alt:") else None
            if result is None:
                # Alternates are not tiers, so invoke them directly.
                import json as _json

                raw = client.runtime.invoke_model(
                    modelId=model_id,
                    body=_json.dumps(
                        {
                            "anthropic_version": "bedrock-2023-05-31",
                            "max_tokens": 16,
                            "messages": [{"role": "user", "content": PROBE}],
                        }
                    ),
                    contentType="application/json",
                    accept="application/json",
                )
                payload = _json.loads(raw["body"].read())
                result = {
                    "text": "".join(b.get("text", "") for b in payload.get("content", [])),
                    "input_tokens": payload.get("usage", {}).get("input_tokens"),
                    "output_tokens": payload.get("usage", {}).get("output_tokens"),
                }
            print(f"  [OK  ] {name:<16} {model_id}")
            print(f"         reply={result['text']!r} "
                  f"tokens in={result['input_tokens']} out={result['output_tokens']}")
        except BedrockError as exc:
            failures += 1
            print(f"  [FAIL] {name:<16} {model_id}")
            print(f"         {exc}")
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"  [FAIL] {name:<16} {model_id}")
            print(f"         {type(exc).__name__}: {exc}")

    unavailable = config.get("unavailable", {})
    if unavailable:
        print("\n  known unavailable (not tested):")
        for model_id, info in unavailable.items():
            print(f"    {model_id} -- {info['reason']}")

    print(f"\n{len(targets) - failures}/{len(targets)} tiers invoked successfully")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
