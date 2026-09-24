"""Re-point the operator /32 rules at this machine's current address.

Both databases admit exactly one operator address: Phase 6's stack pins it in
`DbSecurityGroup`, and the source EC2 box carries the equivalent rule for the
Oracle listener. That is the right shape -- narrow, auditable -- but it breaks
the moment the address changes, which on a laptop is routine.

On 2026-09-22 it changed mid-session. The console lost the target, Phase 4c
failed with "Can't create a connection to host ...", and Phase 7's target
checks went BLOCKED rather than failing, because an unreadable target is not
the same as an empty one. Nothing said the cause was an IP; it took reading
two security groups by hand to see it. Fixing it took two CLI calls per
group -- authorize the new address, revoke the old.

This does that in one call, for every group DBShift owns, and says what it
changed. It is deliberately narrow:

  * only rules whose description marks them as ours are revoked, so a rule
    someone else added for their own access is never removed;
  * the new rule is added before the old one is revoked, so a failure
    half-way leaves access working rather than locked out;
  * an address that is already current is a no-op that says so.
"""

from __future__ import annotations

import ipaddress
import urllib.request

# What this module writes into the Description of every rule it creates, and
# the only marker it will revoke on. A rule without it is somebody else's.
MARKER = "DBShift operator"


def current_ip(timeout: int = 10) -> str:
    """This machine's public address. Raises rather than guessing."""
    with urllib.request.urlopen("https://checkip.amazonaws.com", timeout=timeout) as r:
        ip = r.read().decode().strip()
    ipaddress.IPv4Address(ip)          # raises if the service returned junk
    return ip


def _ours(rng: dict) -> bool:
    return MARKER in (rng.get("Description") or "")


def refresh_group(ec2, *, group_id: str, port: int, cidr: str, label: str) -> dict:
    """Point one group's operator rule at `cidr`. Idempotent.

    Returns what happened rather than raising, so one unreachable group does
    not stop the others from being fixed.
    """
    out = {"group_id": group_id, "label": label, "port": port,
           "added": False, "revoked": [], "already_current": False, "error": None}
    try:
        groups = ec2.describe_security_groups(GroupIds=[group_id])["SecurityGroups"]
        if not groups:
            out["error"] = "group not found"
            return out

        stale = []
        for perm in groups[0].get("IpPermissions", []):
            if perm.get("FromPort") != port or perm.get("IpProtocol") != "tcp":
                continue
            for rng in perm.get("IpRanges", []):
                if rng.get("CidrIp") == cidr:
                    out["already_current"] = True
                elif _ours(rng):
                    stale.append(rng["CidrIp"])

        # Add before revoking: a failure between the two leaves the old rule
        # in place, which is inconvenient. The reverse order locks you out.
        if not out["already_current"]:
            ec2.authorize_security_group_ingress(
                GroupId=group_id,
                IpPermissions=[{
                    "IpProtocol": "tcp", "FromPort": port, "ToPort": port,
                    "IpRanges": [{"CidrIp": cidr, "Description": f"{MARKER} -- {label}"}],
                }])
            out["added"] = True

        for old in stale:
            ec2.revoke_security_group_ingress(
                GroupId=group_id,
                IpPermissions=[{
                    "IpProtocol": "tcp", "FromPort": port, "ToPort": port,
                    "IpRanges": [{"CidrIp": old}],
                }])
            out["revoked"].append(old)
    except Exception as exc:  # noqa: BLE001 -- botocore's shapes vary
        if "InvalidPermission.Duplicate" in str(exc):
            out["already_current"] = True
        else:
            out["error"] = str(exc).splitlines()[0]
    return out
