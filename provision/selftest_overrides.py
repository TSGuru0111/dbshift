"""Offline checks for the manual instance choice and the database configuration.

No AWS, no database. What matters here is not that a person *can* change a value
-- that is easy -- but that the record still says what happened afterwards. The
Provision screen's whole claim is that every value says where it came from, and
an override is the one case where that claim is easiest to quietly break.

So the checks fall in three groups:

  1. a bad override is refused, with a reason a person can act on
  2. an accepted override reaches the template, the preflight and the price
  3. the derived value survives in the provenance next to what replaced it
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    __package__ = "provision"

from . import overrides as O
from . import policy, render

PASS = FAIL = 0


def check(label, got, want):
    global PASS, FAIL
    if got == want:
        PASS += 1
        print(f"  [ok] {label}")
    else:
        FAIL += 1
        print(f"  [XX] {label}\n       got  {got!r}\n       want {want!r}")


def refuses(label, fn, *a, **kw):
    """The refusal must carry a reason, not just fail."""
    global PASS, FAIL
    try:
        fn(*a, **kw)
    except O.OverrideError as exc:
        if str(exc).strip():
            PASS += 1
            print(f"  [ok] {label}  -- {str(exc)[:66]}")
        else:
            FAIL += 1
            print(f"  [XX] {label}: refused with an empty message")
    except Exception as exc:
        FAIL += 1
        print(f"  [XX] {label}: raised {type(exc).__name__}, not OverrideError -- {exc}")
    else:
        FAIL += 1
        print(f"  [XX] {label}: was ACCEPTED and should not have been")


REASON = "client standardises on m5 for production databases"


def _records():
    """The minimum Phase 3/5 records render() reads, on the Oracle path."""
    return {
        "sizing": {
            "collector_run_id": "run-1234",
            "decision": {"edition": "SE2", "licence_model": "license-included",
                         "processor_licences": 1, "instance_class": "db.t3.medium",
                         "vcpu": 2, "memory_gib": 4, "storage_gb": 100,
                         "storage_type": "gp3", "forced_by": [],
                         "character_set": "AL32UTF8"},
            "facts": {"segment_gb": 1.03, "utilization": {"basis": "capacity"}},
        },
        "gate": {"verdict": "PROCEED", "blockers": [], "collector_run_id": "run-1234",
                 "by_phase": {"provision": {"status": "clear", "blocked_by": []}}},
        "assessment": {"source": {"estate": "DBMIG_APP"}, "issues": [], "scores": {},
                       "findings": []},
        "remediation": {"totals": {}, "fixes_with_sql": 0, "entries": []},
    }


def _render(instance_override=None, config_override=None):
    return render.render(
        _records(), {"version": "19", "version_full": "19.3.0", "nls_characterset": "AL32UTF8"},
        engine_version="19.0.0.0.ru-2024-01.rur-2024-01.r1",
        stack_name="dbshift-target-dbmig-app", estate="DBMIG_APP",
        now=datetime(2026, 9, 16, tzinfo=timezone.utc),
        instance_override=instance_override, config_override=config_override)


def _prop(rendered, name):
    return rendered["template"]["Resources"]["DbInstance"]["Properties"][name]


def _prov(rendered, prop):
    return next((r for r in rendered["provenance"] if r["property"] == prop), None)


def main() -> int:
    print("instance class -- refusals")
    refuses("an unknown class is refused", O.validate_instance_class,
            "db.t3.medum", "db.t3.medium", REASON)
    refuses("a class equal to the derived one is not an override",
            O.validate_instance_class, "db.t3.medium", "db.t3.medium", REASON)
    refuses("no reason is refused", O.validate_instance_class,
            "db.m5.large", "db.t3.medium", None)
    refuses("a token reason is refused", O.validate_instance_class,
            "db.m5.large", "db.t3.medium", "n/a")

    print("\ninstance class -- accepted")
    o = O.validate_instance_class("db.m5.large", "db.t3.medium", REASON)
    check("keeps the derived value", o["derived"], "db.t3.medium")
    check("records the choice", o["chosen"], "db.m5.large")
    check("records the reason", o["reason"], REASON)
    check("carries the spec so the console need not look it up",
          (o["vcpu"], o["memory_gib"]), (2, 8))
    check("notes that it is larger, and so costs more",
          any("larger" in n for n in o["notes"]), True)

    small = O.validate_instance_class("db.t3.small", "db.m5.2xlarge", REASON)
    check("notes when a choice is SMALLER than the evidence supports",
          any("smaller" in n for n in small["notes"]), True)
    check("and warns that burstable throttles",
          any("throttle" in n for n in small["notes"]), True)

    print("\nconfiguration -- refusals")
    refuses("a locked property is refused with its reason",
            O.validate_config, {"engine_version": "21"}, REASON)
    refuses("an unknown field is refused", O.validate_config, {"colour": "blue"}, REASON)
    refuses("a db name over 8 characters is refused",
            O.validate_config, {"db_name": "TOOLONGNAME"}, REASON)
    refuses("a db name starting with a digit is refused",
            O.validate_config, {"db_name": "9LIVES"}, REASON)
    refuses("a port below the allowed range is refused",
            O.validate_config, {"port": 80}, REASON)
    refuses("backup retention over 35 days is refused",
            O.validate_config, {"backup_retention_days": 400}, REASON)
    refuses("a non-numeric port is refused", O.validate_config, {"port": "abc"}, REASON)
    refuses("changing a value without a reason is refused",
            O.validate_config, {"multi_az": True}, None)

    print("\nconfiguration -- accepted")
    c = O.validate_config({"multi_az": True, "backup_retention_days": 7}, REASON)
    check("both changes are recorded", len(c["changed"]), 2)
    check("and both are flagged as costing money",
          sorted(c["billing_changes"]), ["Backup retention (days)", "Multi-AZ standby"])
    # A form posts every field. Only what differs is an override.
    everything = {f["name"]: f["default"] for f in O.CONFIG_FIELDS}
    everything["multi_az"] = True
    c2 = O.validate_config(everything, REASON)
    check("posting every field records only what actually changed",
          [x["field"] for x in c2["changed"]], ["multi_az"])
    check("an unchanged form needs no reason",
          O.validate_config({f["name"]: f["default"] for f in O.CONFIG_FIELDS}, None)["changed"], [])
    check("'true' as a string is accepted as a boolean",
          O.validate_config({"multi_az": "true"}, REASON)["values"]["multi_az"], True)

    print("\neffective policy")
    check("defaults when nothing was overridden",
          O.effective_policy(None)["multi_az"], policy.MULTI_AZ)
    check("the override wins", O.effective_policy(c)["multi_az"], True)
    check("policy itself is never mutated", policy.MULTI_AZ, False)

    print("\nthe render -- no override")
    base = _render()
    check("uses the Phase 3 instance class", _prop(base, "DBInstanceClass"), "db.t3.medium")
    check("uses the default Multi-AZ", _prop(base, "MultiAZ"), policy.MULTI_AZ)
    check("uses the default database name", _prop(base, "DBName"), policy.DB_NAME)
    check("reports no overrides", base["overrides"]["any"], False)
    check("provenance cites Phase 3",
          "Phase 3" in _prov(base, "DBInstanceClass")["source"], True)

    print("\nthe render -- overridden")
    r = _render(instance_override=o, config_override=c)
    check("the chosen class reaches the template",
          _prop(r, "DBInstanceClass"), "db.m5.large")
    check("Multi-AZ reaches the template", _prop(r, "MultiAZ"), True)
    check("backup retention reaches the template", _prop(r, "BackupRetentionPeriod"), 7)
    check("the render reports the override", r["overrides"]["any"], True)
    check("and reports Multi-AZ back to the caller", r["multi_az"], True)

    row = _prov(r, "DBInstanceClass")
    check("provenance names a person as the source", "a person" in row["source"], True)
    check("the DERIVED value survives in the row", "db.t3.medium" in row["why"], True)
    check("so does the reason", REASON in row["why"], True)
    check("a changed config value gets its own provenance row",
          _prov(r, "Multi-AZ standby") is not None, True)
    check("and that row says it costs money",
          "costs" in _prov(r, "Multi-AZ standby")["why"], True)
    check("an UNCHANGED config value gets no row",
          _prov(r, "Deletion protection"), None)

    print("\nthe template still validates as JSON")
    check("it serialises", isinstance(json.dumps(r["template"]), str), True)
    check("every tag is still present",
          any(t["Key"] == "Purpose" for t in _prop(r, "Tags")), True)

    print("\nwhat the console is told")
    d = O.describe()
    check("instance classes are offered", len(d["instance_classes"]) > 5, True)
    check("config fields are offered", len(d["config_fields"]), len(O.CONFIG_FIELDS))
    check("locked properties are explained, not hidden",
          all(x["why"] for x in d["locked"]), True)
    check("every field has help text", all(f.get("help") for f in d["config_fields"]), True)

    print(f"\n{PASS}/{PASS + FAIL} checks passed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
