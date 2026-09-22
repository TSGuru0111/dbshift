"""Where SCT actually executes. One interface, two implementations.

A host takes a written `.scts` scenario and runs SCT's batch CLI over it,
streaming progress lines back. That is the whole contract:

    run(scenario_path, toolchain, on_line=None, timeout=None) -> HostResult

  local   subprocess on this machine. The demo, and the seam everything else is
          developed against, because it needs no AWS and no network path.
  ec2     SSM send_command against an instance inside the customer's VPC, where
          their existing Direct Connect or VPN already reaches their Oracle.

The split exists because *reachability*, not capability, decides the host. SCT
must open a TCP connection to the source listener, so it has to run somewhere
that can see it: this machine for a local XE, an instance in their network for a
customer's Oracle. No amount of AWS service selection changes that.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class HostResult:
    """What a host reports back. Deliberately dumb -- parsing lives elsewhere."""
    ok: bool
    exit_code: int | None
    host: str
    lines: list[str] = field(default_factory=list)
    detail: str = ""

    def as_dict(self) -> dict:
        return {
            "ok": self.ok,
            "exit_code": self.exit_code,
            "host": self.host,
            "detail": self.detail,
            "line_count": len(self.lines),
        }
