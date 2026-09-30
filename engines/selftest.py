"""Self-test for the source-engine seam.

Runs offline and needs no database: everything here is data and arithmetic over
it. That is the point of the module being data -- the facts other phases branch
on can be checked without standing up either engine.

The checks that matter most are the negative ones. This module exists so that
MySQL -> RDS for Oracle is refused in exactly one place, and a test that only
proved the legal pairs work would pass just as happily if `pair()` returned
something for every combination.

    python -m engines.selftest
"""

from __future__ import annotations

import sys

import engines
from engines import spec as S


class _Check:
    def __init__(self):
        self.n = self.ok = 0

    def __call__(self, name, cond, detail=""):
        self.n += 1
        self.ok += bool(cond)
        print(f"  [{'ok' if cond else 'FAIL'}] {name}" + (f" -- {detail}" if detail and not cond else ""))


def main(argv=None) -> int:
    c = _Check()

    print("names and normalization")
    c("both source engines are declared", set(engines.SOURCE_ENGINES) == {"ORACLE", "MYSQL"},
      str(engines.SOURCE_ENGINES))
    c("Oracle is the default, so an existing caller is unaffected",
      engines.DEFAULT == engines.ORACLE, engines.DEFAULT)
    c("nothing said means the default", engines.normalize(None) == engines.ORACLE)
    c("empty string means the default", engines.normalize("   ") == engines.ORACLE)
    c("case and separators are accepted", engines.normalize("my_sql") == engines.MYSQL)
    c("MariaDB normalizes to MySQL (wire-compatible, DMS treats it as one)",
      engines.normalize("MariaDB") == engines.MYSQL)
    c("is_mysql and is_oracle agree with normalize",
      engines.is_mysql("mysql") and engines.is_oracle("ORA") and not engines.is_mysql("oracle"))

    bad = False
    try:
        engines.normalize("sqlserver")
    except engines.EngineError as exc:
        bad = "sqlserver" in str(exc) and "ORACLE" in str(exc)
    c("an unknown engine is refused, and the message lists what is valid", bad)

    print()
    print("per-engine facts")
    for e in engines.SOURCE_ENGINES:
        sp = S.spec(e)
        c(f"{e}: every key a phase reads is present",
          all(k in sp for k in ("default_port", "dsn_shape", "driver_module",
                                "identifier_folding", "internal_prefixes",
                                "fold_schema_names", "has_licence_model",
                                "cdc_requirements")),
          str(sorted(sp)))
        c(f"{e}: the spec knows its own name", sp["engine"] == e)
    c("Oracle listens on 1521 and MySQL on 3306",
      S.spec("oracle")["default_port"] == 1521 and S.spec("mysql")["default_port"] == 3306)
    c("Oracle needs a service name in its DSN, MySQL a database",
      "service" in S.spec("oracle")["dsn_shape"] and "database" in S.spec("mysql")["dsn_shape"])

    print()
    print("identifier folding -- the fact Phase 7's mappings need")
    c("Oracle folds unquoted identifiers up", S.folds_identifiers_up("oracle"))
    c("MySQL does NOT fold, so a mixed-case table name must survive the move",
      not S.folds_identifiers_up("mysql"))

    print()
    print("schema-name case -- the trap that makes a collector read nothing")
    c("Oracle schema names may be folded up (the engine already did)",
      S.spec("oracle")["fold_schema_names"] is True)
    c("MySQL schema names must NOT be folded: on Linux they are directories, "
      "so Sales and SALES are different databases",
      S.spec("mysql")["fold_schema_names"] is False)

    print()
    print("licence model -- Phase 3 arithmetic")
    c("Oracle has editions and processor licences", S.spec("oracle")["has_licence_model"])
    c("MySQL has no edition or licence arithmetic, like the PostgreSQL target",
      not S.spec("mysql")["has_licence_model"])

    print()
    print("CDC prerequisites are named per engine, not assumed")
    c("Oracle names ARCHIVELOG and supplemental logging",
      any("ARCHIVELOG" in r for r in S.spec("oracle")["cdc_requirements"]))
    c("MySQL names binlog_format=ROW rather than Oracle's redo settings",
      any("binlog_format=ROW" in r for r in S.spec("mysql")["cdc_requirements"]))
    c("the two engines do not share a single CDC requirement string",
      not (set(S.spec("oracle")["cdc_requirements"]) & set(S.spec("mysql")["cdc_requirements"])))

    print()
    print("legal pairs -- four, and only four")
    c("exactly four pairs are declared", len(S.PAIRS) == 4, str(len(S.PAIRS)))
    c("Oracle offers RDS Oracle and RDS PostgreSQL",
      S.targets_for("oracle") == ("ORACLE", "POSTGRESQL"), str(S.targets_for("oracle")))
    c("MySQL offers RDS MySQL and RDS PostgreSQL",
      S.targets_for("mysql") == ("MYSQL", "POSTGRESQL"), str(S.targets_for("mysql")))
    c("the homogeneous target is offered first, being the weaker claim",
      S.PAIRS[(engines.MYSQL, S.targets_for("mysql")[0])]["kind"] == "homogeneous")
    c("every declared pair carries every key a phase reads",
      all(all(k in v for k in ("kind", "label", "converts_code", "data_mover",
                               "sct_conversion", "sct_assessment"))
          for v in S.PAIRS.values()))
    c("every pair names itself either homogeneous or heterogeneous",
      all(v["kind"] in ("homogeneous", "heterogeneous") for v in S.PAIRS.values()))
    c("every pair's source is a declared source engine",
      all(s in engines.SOURCE_ENGINES for (s, _t) in S.PAIRS))

    print()
    print("illegal pairs are refused in ONE place")
    for src, tgt in (("mysql", "ORACLE"), ("oracle", "MYSQL")):
        refused = not S.is_legal(src, tgt)
        c(f"{src} -> {tgt} is refused", refused)
        msg = ""
        try:
            S.pair(src, tgt)
        except engines.EngineError as exc:
            msg = str(exc)
        c(f"{src} -> {tgt} refusal names the supported targets instead",
          all(t in msg for t in S.targets_for(src)), msg[:70])
    c("a target nobody offers is refused rather than KeyError'd",
      not S.is_legal("mysql", "REDSHIFT"))
    c("an empty target is refused, not defaulted", not S.is_legal("mysql", ""))

    print()
    print("what each pair means downstream")
    c("MySQL -> PostgreSQL converts stored code", S.converts_code("mysql", "postgresql"))
    c("MySQL -> MySQL converts nothing", not S.converts_code("mysql", "mysql"))
    c("Oracle -> Oracle converts nothing", not S.converts_code("oracle", "oracle"))
    c("Oracle -> PostgreSQL converts stored code", S.converts_code("oracle", "postgresql"))

    c("Data Pump moves data only on Oracle -> Oracle",
      S.data_mover("oracle", "oracle") == "datapump")
    c("both MySQL paths use DMS -- Data Pump writes an Oracle-only format",
      S.data_mover("mysql", "mysql") == "dms" and S.data_mover("mysql", "postgresql") == "dms")
    c("every mover is one this project implements",
      all(v["data_mover"] in ("datapump", "dms") for v in S.PAIRS.values()))

    print()
    print("SCT: assessment is not the same claim as conversion")
    c("SCT assesses every pair", all(v["sct_assessment"] for v in S.PAIRS.values()))
    c("SCT reports conversion work on MySQL -> PostgreSQL",
      S.pair("mysql", "postgresql")["sct_conversion"])
    c("SCT reports NO conversion path for MySQL -> MySQL (AWS publishes none), "
      "which Phase 5 must read as CLEAR-with-a-reason, not missing evidence",
      S.pair("mysql", "mysql")["sct_conversion"] is False)
    c("a pair that converts code is exactly a pair SCT converts, except the "
      "homogeneous ones where neither does",
      all(v["converts_code"] == v["sct_conversion"] for v in S.PAIRS.values()))

    print()
    print(f"{c.ok}/{c.n} checks passed")
    return 0 if c.ok == c.n else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
