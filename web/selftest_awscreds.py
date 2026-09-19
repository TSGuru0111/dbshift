"""Checks for the pasted-credential path.

Three things matter here and none of them are visible on screen:

  * the paste is accepted in every shape AWS hands out, and refused clearly
    when it is not a credential block at all;
  * writing our profile never disturbs the other profiles in the shared file,
    because losing dbshift-static to a config page would be its own outage;
  * a secret containing % or = survives the round trip. configparser
    interpolates % by default, and session tokens are base64 -- this is the
    bug that would corrupt a token silently and surface as an authentication
    failure hours later.

Every test runs against a temporary file. Nothing here touches the real
~/.aws/credentials.
"""

from __future__ import annotations

import configparser
import datetime as dt
import os
import sys
import tempfile
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    __package__ = "web"

PASS = FAIL = 0


def check(label, got, want=True):
    global PASS, FAIL
    ok = got == want
    if ok:
        PASS += 1
        print(f"PASS  {label}")
    else:
        FAIL += 1
        print(f"FAIL  {label}\n        got {got!r}, want {want!r}")
    return ok


def refuses(label, mod, text):
    global PASS, FAIL
    try:
        mod.parse_block(text)
    except mod.CredentialError:
        PASS += 1
        print(f"PASS  {label}")
        return True
    FAIL += 1
    print(f"FAIL  {label}\n        accepted something it should have refused")
    return False


BASH = ('export AWS_ACCESS_KEY_ID="ASIAEXAMPLEEXAMPLE00"\n'
        'export AWS_SECRET_ACCESS_KEY="wJalr/K7MDENG+bPxRfi%CYEXAMPLEKEY"\n'
        'export AWS_SESSION_TOKEN="IQoJb3JpZ2luX2Vj//////wEaCmFwLXNvdXRoLTEi+tok=="\n')
PWSH = ('$env:AWS_ACCESS_KEY_ID="ASIAEXAMPLEEXAMPLE00"\n'
        '$env:AWS_SECRET_ACCESS_KEY="s3cr3t"\n$env:AWS_SESSION_TOKEN="tok=="\n')
CMD = ("set AWS_ACCESS_KEY_ID=ASIAEXAMPLEEXAMPLE00\n"
       "set AWS_SECRET_ACCESS_KEY=s3cr3t\nset AWS_SESSION_TOKEN=tok==\n")
BARE = ("AWS_ACCESS_KEY_ID=ASIAEXAMPLEEXAMPLE00\n"
        "AWS_SECRET_ACCESS_KEY=s3cr3t\nAWS_SESSION_TOKEN=tok==\n")


def main() -> int:
    tmp = Path(tempfile.mkdtemp()) / "credentials"
    # Pre-seed the file the way a real machine looks, so the merge is tested
    # against something worth preserving rather than an empty file.
    tmp.write_text(
        "[dbshift-static]\naws_access_key_id = AKIAOLDOLDOLDOLD0000\n"
        "aws_secret_access_key = oldsecret\n\n"
        "[idbi]\naws_access_key_id = AKIAOTHEROTHEROTHER0\n"
        "aws_secret_access_key = othersecret\n", encoding="utf-8")
    os.environ["AWS_SHARED_CREDENTIALS_FILE"] = str(tmp)

    from . import awscreds as a          # imported after the env var is set
    a.CRED_FILE = tmp                    # module read it at import time

    print("-- the paste, in the shapes AWS offers it")
    for label, text in (("bash export", BASH), ("powershell", PWSH),
                        ("cmd set", CMD), ("bare KEY=VALUE", BARE)):
        got = a.parse_block(text)
        check(f"{label} parses", got["aws_access_key_id"], "ASIAEXAMPLEEXAMPLE00")
    check("a temporary key is recognised", a.parse_block(BASH)["is_temporary"], True)
    check("a long-lived key needs no token",
          a.parse_block("AWS_ACCESS_KEY_ID=AKIAEXAMPLEEXAMPLE00\n"
                        "AWS_SECRET_ACCESS_KEY=s")["is_temporary"], False)
    check("the region rides along when present",
          a.parse_block(BARE + "AWS_DEFAULT_REGION=ap-south-1").get("region"), "ap-south-1")

    print("\n-- what it refuses")
    refuses("nothing pasted", a, "")
    refuses("prose instead of a block", a, "the keys are in the email I sent")
    refuses("no secret", a, "AWS_ACCESS_KEY_ID=AKIAEXAMPLEEXAMPLE00")
    refuses("an ASIA key with no session token", a,
            "AWS_ACCESS_KEY_ID=ASIAEXAMPLEEXAMPLE00\nAWS_SECRET_ACCESS_KEY=s")
    refuses("a key id that is not one", a,
            "AWS_ACCESS_KEY_ID=hello\nAWS_SECRET_ACCESS_KEY=s")

    print("\n-- writing the profile")
    creds = a.parse_block(BASH)
    exp = dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=4)
    a.write_profile(creds, region="ap-south-1", expires_at=exp)

    cfg = configparser.RawConfigParser()
    cfg.read(tmp, encoding="utf-8")
    check("our profile is there", cfg.has_section(a.PROFILE), True)
    check("dbshift-static survived", cfg.has_section("dbshift-static"), True)
    check("idbi survived", cfg.has_section("idbi"), True)
    check("its key was not touched",
          cfg.get("dbshift-static", "aws_access_key_id"), "AKIAOLDOLDOLDOLD0000")
    # A session token is base64: it contains + / = and, after URL encoding,
    # %. configparser's default interpolation eats % and would corrupt it.
    check("a secret containing % survives verbatim",
          cfg.get(a.PROFILE, "aws_secret_access_key"), creds["aws_secret_access_key"])
    check("a token containing + / = survives verbatim",
          cfg.get(a.PROFILE, "aws_session_token"), creds["aws_session_token"])
    check("the key hint shows only the ends", a.key_hint(), "ASIA…LE00")
    check("the region was stored", a.stored_region(), "ap-south-1")
    check("roughly four hours are left", 14000 < (a.seconds_left() or 0) <= 14400, True)

    print("\n-- swapping a temporary key for a long-lived one")
    a.write_profile(a.parse_block("AWS_ACCESS_KEY_ID=AKIAEXAMPLEEXAMPLE00\n"
                                  "AWS_SECRET_ACCESS_KEY=plain"), region="ap-south-1")
    cfg = configparser.RawConfigParser()
    cfg.read(tmp, encoding="utf-8")
    # A stale token left behind makes every call fail with an error naming the
    # wrong problem, so the section is cleared before it is rewritten.
    check("the old session token is gone",
          cfg.has_option(a.PROFILE, "aws_session_token"), False)
    check("and so is the old expiry", a.seconds_left(), None)

    print("\n-- the expiry guard")
    for mins, should_block in ((240, False), (25, False), (8, True), (-1, True)):
        a.write_profile(creds, region="ap-south-1",
                        expires_at=dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=mins))
        blocked = False
        try:
            a.guard_long_run("a deploy")
        except a.CredentialError:
            blocked = True
        check(f"{mins:>4}m left -> {'refuses' if should_block else 'allows'} a deploy",
              blocked, should_block)
    a.clear_profile()
    blocked = False
    try:
        a.guard_long_run("a deploy")
    except a.CredentialError:
        blocked = True
    check("no pasted credentials -> the guard stays out of the way", blocked, False)

    print("\n-- removing it")
    a.write_profile(creds, region="ap-south-1", expires_at=exp)
    check("clear reports it removed one", a.clear_profile(), True)
    cfg = configparser.RawConfigParser()
    cfg.read(tmp, encoding="utf-8")
    check("our profile is gone", cfg.has_section(a.PROFILE), False)
    check("the others are still there",
          cfg.has_section("dbshift-static") and cfg.has_section("idbi"), True)
    check("clearing again is harmless", a.clear_profile(), False)

    print("\n-- the status never carries the secret")
    a.write_profile(creds, region="ap-south-1", expires_at=exp)

    class _Boom:
        def client(self, *_a, **_k):
            raise RuntimeError("ExpiredToken: the token is expired")

    st = a.status(lambda: _Boom(), "ap-south-1")
    blob = repr(st)
    check("no secret in the status", creds["aws_secret_access_key"] in blob, False)
    check("no token in the status", creds["aws_session_token"] in blob, False)
    check("no whole key id in the status", creds["aws_access_key_id"] in blob, False)
    check("an expired token is explained in English",
          "expired" in (st["error"] or "").lower(), True)

    print(f"\n{PASS}/{PASS + FAIL} checks passed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
