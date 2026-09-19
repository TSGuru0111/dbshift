"""Accept a pasted AWS SSO credential block and park it in a named profile.

SSO credentials last about four hours, so a demo spans two or three of them.
The console's own screens all resolve AWS through a named boto3 profile, and
so do the CLI entry points (dms/run.py, killswitch/run.py, provision/run.py),
which means the least surprising place to put a fresh set is the same
~/.aws/credentials file every one of those already reads. Writing it there,
rather than into this repo or into the server's memory, has three
consequences worth stating:

  * nothing lands in the working tree, so nothing can be committed. This
    repo has leaked estate passwords to a public remote once already.
  * the CLI scripts see the same credentials the console does, so a demo can
    move between the two without a second paste.
  * the credentials outlive the server process. That is the point -- but it
    also means "paste once, run all evening" is false advertising, hence the
    expiry tracking below.

Nothing here ever returns the secret or the session token to a caller. The
only things that come back out are the account, the role, the region, the
last four characters of the key id, and when it expires.
"""

from __future__ import annotations

import configparser
import datetime as _dt
import os
import re
from pathlib import Path

# The profile this module owns. Kept distinct from dbshift-static and
# dbshift-bedrock so that pasting a short-lived block can never clobber a
# long-lived profile someone set up by hand.
PROFILE = "dbshift-console"

# Where boto3 itself looks, honouring the same override it does.
CRED_FILE = Path(os.environ.get("AWS_SHARED_CREDENTIALS_FILE",
                               Path.home() / ".aws" / "credentials"))

# Our own bookkeeping key. botocore ignores unknown keys in a profile, so this
# rides along in the same section without upsetting anything that reads it.
EXPIRY_KEY = "x_dbshift_expires_at"

# An operation that takes longer than this should not be started on a session
# that expires sooner -- a CloudFormation stack half-created by an expired
# token is worse than one never started.
LONG_RUN_MINUTES = 20

_KEYS = {
    "AWS_ACCESS_KEY_ID": "aws_access_key_id",
    "AWS_SECRET_ACCESS_KEY": "aws_secret_access_key",
    "AWS_SESSION_TOKEN": "aws_session_token",
    "AWS_DEFAULT_REGION": "region",
    "AWS_REGION": "region",
}

# export FOO="bar" | export FOO=bar | FOO='bar' | set FOO=bar | FOO: bar
_LINE = re.compile(
    r"""^\s*(?:export\s+|set\s+|\$env:)?      # shell/pwsh prefixes, all optional
        (?P<key>[A-Za-z_][A-Za-z0-9_]*)       # the variable
        \s*[=:]\s*                            # = or : (the console's own format)
        (?P<q>["']?)(?P<val>.*?)(?P=q)\s*;?\s*$""",
    re.VERBOSE)


class CredentialError(ValueError):
    """The pasted block is not usable. The message is shown to the user."""


def parse_block(text: str) -> dict:
    """Pull the credential variables out of whatever AWS handed the user.

    The SSO portal offers the same three values in several shapes -- bash
    exports, PowerShell $env:, Windows set, or a bare KEY=VALUE list. All of
    them arrive here as a paste, so all of them are accepted rather than
    asking someone to reformat a secret by hand at a client site.
    """
    if not text or not text.strip():
        raise CredentialError("Nothing pasted.")

    found: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = _LINE.match(line)
        if not m:
            continue
        key = m.group("key").upper()
        if key in _KEYS:
            val = m.group("val").strip()
            if val:
                found.setdefault(_KEYS[key], val)

    missing = [k for k in ("aws_access_key_id", "aws_secret_access_key") if k not in found]
    if missing:
        raise CredentialError(
            "Could not find " + " and ".join(m.upper() for m in missing) +
            " in what was pasted. Copy the whole block from the AWS access portal.")

    key_id = found["aws_access_key_id"]
    # ASIA is the temporary-credential prefix; AKIA is a long-lived IAM user
    # key. Both work, but a long-lived key pasted into a browser is a much
    # worse thing to have done, and the user should be told so.
    if not re.fullmatch(r"[A-Z0-9]{16,128}", key_id):
        raise CredentialError("That does not look like an AWS access key id.")
    if key_id.startswith("ASIA") and "aws_session_token" not in found:
        raise CredentialError(
            "This is a temporary key (ASIA…) but no AWS_SESSION_TOKEN was "
            "included. Paste all three lines.")

    found["is_temporary"] = key_id.startswith("ASIA")
    return found


def _now() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


def write_profile(creds: dict, region: str | None = None,
                  expires_at: _dt.datetime | None = None) -> None:
    """Merge the credentials into the shared file as our own profile.

    Read-modify-write rather than truncate: this file holds other people's
    profiles and losing dbshift-static to a config page would be its own
    outage. The file is written 0600 where the platform honours it.
    """
    cfg = configparser.RawConfigParser()
    if CRED_FILE.exists():
        # RawConfigParser so that a literal % in a secret is not read as
        # interpolation -- base64 session tokens contain them.
        cfg.read(CRED_FILE, encoding="utf-8")

    if not cfg.has_section(PROFILE):
        cfg.add_section(PROFILE)
    # Clear first: moving from a temporary key to a long-lived one must not
    # leave a stale session token behind, which would make every call fail
    # with an error that names the wrong problem.
    for stale in ("aws_access_key_id", "aws_secret_access_key",
                  "aws_session_token", "region", EXPIRY_KEY):
        cfg.remove_option(PROFILE, stale)

    cfg.set(PROFILE, "aws_access_key_id", creds["aws_access_key_id"])
    cfg.set(PROFILE, "aws_secret_access_key", creds["aws_secret_access_key"])
    if creds.get("aws_session_token"):
        cfg.set(PROFILE, "aws_session_token", creds["aws_session_token"])
    if region or creds.get("region"):
        cfg.set(PROFILE, "region", region or creds["region"])
    if expires_at:
        cfg.set(PROFILE, EXPIRY_KEY, expires_at.astimezone(_dt.timezone.utc)
                .replace(microsecond=0).isoformat().replace("+00:00", "Z"))

    CRED_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = CRED_FILE.with_suffix(CRED_FILE.suffix + ".dbshift-tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        cfg.write(fh)
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass          # Windows ACLs; the replace below still happens
    os.replace(tmp, CRED_FILE)


def clear_profile() -> bool:
    """Drop our profile. Returns whether there was one to drop."""
    if not CRED_FILE.exists():
        return False
    cfg = configparser.RawConfigParser()
    cfg.read(CRED_FILE, encoding="utf-8")
    if not cfg.has_section(PROFILE):
        return False
    cfg.remove_section(PROFILE)
    tmp = CRED_FILE.with_suffix(CRED_FILE.suffix + ".dbshift-tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        cfg.write(fh)
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, CRED_FILE)
    return True


def stored_expiry() -> _dt.datetime | None:
    """When the parked credentials stop working, if we recorded it."""
    if not CRED_FILE.exists():
        return None
    cfg = configparser.RawConfigParser()
    try:
        cfg.read(CRED_FILE, encoding="utf-8")
    except configparser.Error:
        return None
    raw = cfg.get(PROFILE, EXPIRY_KEY, fallback=None) if cfg.has_section(PROFILE) else None
    if not raw:
        return None
    try:
        return _dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def key_hint() -> str | None:
    """The last four characters of the stored key id -- never the whole thing."""
    if not CRED_FILE.exists():
        return None
    cfg = configparser.RawConfigParser()
    try:
        cfg.read(CRED_FILE, encoding="utf-8")
    except configparser.Error:
        return None
    if not cfg.has_section(PROFILE):
        return None
    kid = cfg.get(PROFILE, "aws_access_key_id", fallback="")
    return f"{kid[:4]}…{kid[-4:]}" if len(kid) >= 8 else None


def stored_region() -> str | None:
    if not CRED_FILE.exists():
        return None
    cfg = configparser.RawConfigParser()
    try:
        cfg.read(CRED_FILE, encoding="utf-8")
    except configparser.Error:
        return None
    if not cfg.has_section(PROFILE):
        return None
    return cfg.get(PROFILE, "region", fallback=None) or None


def seconds_left() -> int | None:
    """Seconds until expiry. None when we have no expiry on record."""
    exp = stored_expiry()
    if not exp:
        return None
    return int((exp - _now()).total_seconds())


def status(session_factory, region: str) -> dict:
    """What the Config screen and the topbar chip draw.

    Calls STS so the answer is what AWS thinks, not what we stored: a key can
    be revoked long before its clock runs out, which is exactly what should
    happen to one that has been pasted somewhere it should not have been.
    """
    left = seconds_left()
    out = {
        "configured": False,
        "profile": PROFILE,
        "account": None, "identity": None, "arn": None,
        "region": stored_region() or region,
        "key_hint": key_hint(),
        "expires_at": None,
        "seconds_left": left,
        "expired": bool(left is not None and left <= 0),
        "valid": False,
        "error": None,
        "credentials_file": str(CRED_FILE),
    }
    exp = stored_expiry()
    if exp:
        out["expires_at"] = exp.replace(microsecond=0).isoformat().replace("+00:00", "Z")
    if not out["key_hint"]:
        return out
    out["configured"] = True

    if out["expired"]:
        # Do not spend a round trip proving what the clock already says.
        out["error"] = "The pasted credentials have expired."
        return out

    try:
        ident = session_factory().client("sts", region_name=out["region"]).get_caller_identity()
        out["account"] = ident.get("Account")
        out["arn"] = ident.get("Arn")
        out["identity"] = _short_identity(ident.get("Arn", ""))
        out["valid"] = True
    except Exception as exc:                        # noqa: BLE001 -- shapes vary
        out["error"] = _friendly(exc)
    return out


def _short_identity(arn: str) -> str | None:
    """assumed-role/AWSPowerUserAccess_abc/guru -> AWSPowerUserAccess/guru."""
    if not arn:
        return None
    tail = arn.rsplit(":", 1)[-1]
    parts = [p for p in tail.split("/") if p]
    if parts and parts[0] == "assumed-role" and len(parts) >= 3:
        role = re.sub(r"_[0-9a-f]{8,}$", "", parts[1])
        return f"{role}/{parts[-1]}"
    return parts[-1] if parts else None


def _friendly(exc: Exception) -> str:
    """Turn a botocore error into something worth showing a person."""
    text = str(exc)
    if "ExpiredToken" in text or "token included in the request is expired" in text:
        return "The credentials have expired. Paste a fresh block."
    if "InvalidClientTokenId" in text or "security token included in the request is invalid" in text:
        return "AWS rejected these credentials. They may have been revoked."
    if "SignatureDoesNotMatch" in text:
        return "The secret access key does not match the key id. Re-copy the whole block."
    if "AccessDenied" in text:
        # The credentials are fine; the role simply cannot call sts. Say so,
        # rather than implying the paste failed.
        return "These credentials work, but the role cannot call sts:GetCallerIdentity."
    if "EndpointConnectionError" in text or "Could not connect" in text:
        return "Cannot reach AWS from this machine."
    return text.splitlines()[0][:200]


def guard_long_run(what: str) -> None:
    """Refuse to start something slow on credentials about to expire.

    Raises CredentialError, which the routes turn into a 409. A deploy that
    dies half way leaves a half-built stack and a confusing screen; being told
    up front costs one paste.
    """
    left = seconds_left()
    if left is None:
        return                                  # no expiry recorded; not our call
    if left <= 0:
        raise CredentialError(
            f"The AWS credentials have expired, so {what} would fail. "
            "Paste a fresh block on the Config screen.")
    if left < LONG_RUN_MINUTES * 60:
        mins = max(1, left // 60)
        raise CredentialError(
            f"The AWS credentials expire in {mins} minute{'s' if mins != 1 else ''}, "
            f"and {what} usually takes longer than that. Paste a fresh block first.")
