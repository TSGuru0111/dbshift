"""The five levels of validation, from "it exists" to "it behaves".

Each check returns findings with one of four verdicts:

  match                identical, or identical where it must be
  expected_difference  different, and correctly so -- the reason is recorded
  mismatch             different and unexplained. Only these fail the phase.
  not_comparable       one side could not be read. Never guessed at, and never
                       quietly counted as a match -- the first version of the
                       Phase 7 count did exactly that and flattered itself.

Every expected difference names what caused it: a decision recorded earlier, a
version difference between 21c and 19c, or an account that exists only on the
source. If a difference cannot be traced to one of those, it is a mismatch.
"""

from __future__ import annotations

import re
import time
from collections import Counter, defaultdict

from provision import policy as prov_policy

from .context import EXPECTED, MATCH, MISMATCH, NOT_COMPARABLE, Ctx, finding, is_internal

IDENT = re.compile(r"^[A-Z][A-Z0-9_$#]{0,127}$")
# Types whose values compare byte for byte across 21c and 19c. LOBs, XMLType and
# object columns are excluded and reported, rather than compared badly.
CHECKSUM_TYPES = ("VARCHAR2", "CHAR", "NVARCHAR2", "NCHAR", "NUMBER", "FLOAT", "DATE", "RAW",
                  "BINARY_DOUBLE", "BINARY_FLOAT")


# --------------------------------------------------------------------------- level 1

def objects(ctx: Ctx) -> list[dict]:
    owner = ctx.estate
    src = {(o["object_type"], o["object_name"]) for o in ctx.data("objects") if o.get("owner") == owner}
    tgt = {(t, n) for t, n in ctx.rows(ctx.target(),
           "SELECT object_type, object_name FROM dba_objects WHERE owner = :o", {"o": owner})}
    out = []

    named_src = {x for x in src if not is_internal(x[1])}
    named_tgt = {x for x in tgt if not is_internal(x[1])}
    missing, extra = sorted(named_src - named_tgt), sorted(named_tgt - named_src)
    if missing:
        out.append(finding(1, "objects present", MISMATCH,
                           f"{len(missing)} object(s) on the source are not on the target",
                           evidence={"missing": [f"{t} {n}" for t, n in missing]}))
    else:
        out.append(finding(1, "objects present", MATCH,
                           f"all {len(named_src)} named objects exist on the target",
                           evidence={"by_type": dict(Counter(t for t, _ in named_src))}))
    if extra:
        out.append(finding(1, "objects added", MISMATCH,
                           f"{len(extra)} object(s) exist on the target only",
                           evidence={"extra": [f"{t} {n}" for t, n in extra]}))

    # Oracle-generated names: counted, never matched by name.
    isrc, itgt = Counter(t for t, n in src if is_internal(n)), Counter(t for t, n in tgt if is_internal(n))
    if isrc or itgt:
        same = isrc == itgt
        out.append(finding(1, "Oracle-generated objects", MATCH if same else EXPECTED,
                           f"source {sum(isrc.values())}, target {sum(itgt.values())}",
                           why="" if same else "Oracle names and counts these itself, and 21c and 19c differ "
                               "-- the Text index alone has $B, $C and $Q tables on 21c that 19c does not "
                               "build. They are rebuilt by their index, not migrated.",
                           evidence={"source": dict(isrc), "target": dict(itgt)}))
    return out


# --------------------------------------------------------------------------- level 2

def _user_tables(ctx: Ctx) -> list[str]:
    owner = ctx.estate
    ext = {e["table_name"] for e in ctx.data("external_tables") if e["owner"] == owner}
    return sorted({t["table_name"] for t in ctx.data("tables")
                   if t.get("owner") == owner and not is_internal(t["table_name"])
                   and t["table_name"] not in ext and IDENT.match(t["table_name"])})


def structure(ctx: Ctx) -> list[dict]:
    owner, out = ctx.estate, []
    tables = _user_tables(ctx)

    # --- columns -------------------------------------------------------------
    src_cols = defaultdict(dict)
    for c in ctx.data("columns"):
        if c.get("owner") == owner and c.get("user_generated") != "NO" and c["table_name"] in tables:
            src_cols[c["table_name"]][c["column_name"]] = (
                c["data_type"], c["data_length"], c["data_precision"], c["data_scale"], c["nullable"])
    tgt_cols = defaultdict(dict)
    for t, n, dt, dl, dp, ds, nu in ctx.rows(ctx.target(),
            "SELECT table_name, column_name, data_type, data_length, data_precision, data_scale, nullable "
            "FROM dba_tab_cols WHERE owner = :o AND user_generated = 'YES'", {"o": owner}):
        if t in tables:
            tgt_cols[t][n] = (dt, dl, dp, ds, nu)
    differences = []
    for table in tables:
        s, t = src_cols.get(table, {}), tgt_cols.get(table, {})
        for col in sorted(set(s) | set(t)):
            if s.get(col) != t.get(col):
                differences.append(f"{table}.{col}: source {s.get(col)} target {t.get(col)}")
    out.append(finding(2, "columns", MISMATCH if differences else MATCH,
                       f"{sum(len(v) for v in src_cols.values())} columns across {len(tables)} tables"
                       + (f"; {len(differences)} differ" if differences else "; every one matches on type, "
                          "length, precision, scale and nullability"),
                       evidence={"differences": differences[:40]}))

    # --- constraints: compared by shape, since Oracle names some itself -------
    def sig_src():
        cols = defaultdict(list)
        for cc in ctx.data("constraint_columns"):
            if cc.get("owner") == owner:
                cols[cc["constraint_name"]].append((cc.get("position") or 0, cc["column_name"]))
        out_ = Counter()
        for c in ctx.data("constraints"):
            if c.get("owner") != owner or c["table_name"] not in tables:
                continue
            key = (c["table_name"], c["constraint_type"],
                   ",".join(n for _, n in sorted(cols.get(c["constraint_name"], []))),
                   c.get("validated"))
            out_[key] += 1
        return out_

    tgt_cols_by_con = defaultdict(list)
    for cn, cname, pos in ctx.rows(ctx.target(),
            "SELECT constraint_name, column_name, position FROM dba_cons_columns WHERE owner = :o",
            {"o": owner}):
        tgt_cols_by_con[cn].append((pos or 0, cname))
    tgt_sig = Counter()
    for cn, ctype, table, validated in ctx.rows(ctx.target(),
            "SELECT constraint_name, constraint_type, table_name, validated FROM dba_constraints "
            "WHERE owner = :o", {"o": owner}):
        if table in tables:
            tgt_sig[(table, ctype, ",".join(n for _, n in sorted(tgt_cols_by_con.get(cn, []))), validated)] += 1
    s_sig = sig_src()
    only_src = sorted(f"{k[0]} {k[1]} ({k[2]}) {k[3]}" for k in (s_sig - tgt_sig))
    only_tgt = sorted(f"{k[0]} {k[1]} ({k[2]}) {k[3]}" for k in (tgt_sig - s_sig))
    out.append(finding(2, "constraints", MISMATCH if (only_src or only_tgt) else MATCH,
                       f"{sum(s_sig.values())} on the source, {sum(tgt_sig.values())} on the target"
                       + ("" if not (only_src or only_tgt) else "; they do not line up"),
                       evidence={"missing_on_target": only_src[:30], "target_only": only_tgt[:30]}))

    # --- indexes: by table, uniqueness and column list ------------------------
    icols = defaultdict(list)
    for ic in ctx.data("index_columns"):
        if ic.get("index_owner") == owner:
            icols[ic["index_name"]].append((ic["column_position"], ic["column_name"]))
    s_idx = Counter()
    for i in ctx.data("indexes"):
        if i.get("owner") == owner and i["table_name"] in tables and not is_internal(i["index_name"]):
            s_idx[(i["table_name"], i["index_type"], i["uniqueness"],
                   ",".join(n for _, n in sorted(icols.get(i["index_name"], []))))] += 1
    t_icols = defaultdict(list)
    for iname, cname, pos in ctx.rows(ctx.target(),
            "SELECT index_name, column_name, column_position FROM dba_ind_columns WHERE index_owner = :o",
            {"o": owner}):
        t_icols[iname].append((pos, cname))
    t_idx = Counter()
    for iname, itype, table, uniq in ctx.rows(ctx.target(),
            "SELECT index_name, index_type, table_name, uniqueness FROM dba_indexes WHERE owner = :o",
            {"o": owner}):
        if table in tables and not is_internal(iname):
            t_idx[(table, itype, uniq, ",".join(n for _, n in sorted(t_icols.get(iname, []))))] += 1
    missing_idx = sorted(f"{k[0]} {k[1]} {k[2]} ({k[3]})" for k in (s_idx - t_idx))
    extra_idx = sorted(f"{k[0]} {k[1]} {k[2]} ({k[3]})" for k in (t_idx - s_idx))
    out.append(finding(2, "indexes", MISMATCH if missing_idx else (EXPECTED if extra_idx else MATCH),
                       f"{sum(s_idx.values())} on the source, {sum(t_idx.values())} on the target",
                       why="An index the target has and the source does not is usually a fix applied on the "
                           "way -- check it is one you approved." if extra_idx and not missing_idx else "",
                       evidence={"missing_on_target": missing_idx, "target_only": extra_idx}))
    return out


# --------------------------------------------------------------------------- level 3

def row_counts(ctx: Ctx) -> list[dict]:
    owner, out = ctx.estate, []
    matched, differ, unreadable = [], [], []
    for table in _user_tables(ctx):
        src, tgt = ctx.both(f'SELECT COUNT(*) FROM "{owner}"."{table}"')
        s = src[0][0] if isinstance(src, list) else src
        t = tgt[0][0] if isinstance(tgt, list) else tgt
        if isinstance(s, int) and isinstance(t, int):
            (matched if s == t else differ).append((table, s, t))
        else:
            unreadable.append((table, s, t))
        ctx.log(f"{table:<24} source {s}  target {t}")
    if differ:
        out.append(finding(3, "row counts", MISMATCH, f"{len(differ)} table(s) differ",
                           evidence={"differ": [f"{t}: source {s}, target {g}" for t, s, g in differ]}))
    # "0 of 0 match" is not a pass. It means nothing was compared.
    out.append(finding(3, "row counts",
                       NOT_COMPARABLE if not matched else (MATCH if not differ else NOT_COMPARABLE),
                       f"{len(matched)} of {len(matched) + len(differ)} comparable tables match exactly"
                       if matched else "no table could be counted on both sides",
                       evidence={"rows": {t: s for t, s, _ in matched}}))
    if unreadable:
        out.append(finding(3, "row counts", NOT_COMPARABLE,
                           f"{len(unreadable)} table(s) could not be counted on one side",
                           why="The read-only discovery account holds no grant on them. That is a limit of "
                               "what discovery may read, not evidence of a difference.",
                           evidence={"tables": [f"{t}: source {s}, target {g}" for t, s, g in unreadable]}))
    return out


# --------------------------------------------------------------------------- level 4

def _checksum_sql(owner: str, table: str, cols: list[dict]) -> str:
    parts = []
    for c in cols:
        name, dtype = f'"{c["column_name"]}"', c["data_type"]
        if dtype == "DATE":
            expr = f"TO_CHAR({name},'YYYY-MM-DD HH24:MI:SS')"
        elif dtype.startswith("TIMESTAMP"):
            fmt = "YYYY-MM-DD HH24:MI:SS.FF6" + (" TZH:TZM" if "TIME ZONE" in dtype else "")
            expr = f"TO_CHAR({name},'{fmt}')"
        elif dtype in ("NUMBER", "FLOAT", "BINARY_DOUBLE", "BINARY_FLOAT"):
            expr = f"TO_CHAR({name})"
        elif dtype == "RAW":
            expr = f"RAWTOHEX({name})"
        else:
            expr = name
        parts.append(f"NVL({expr},'~')")
    joined = " || '|' || ".join(parts)
    # ORA_HASH takes at most 4000 characters; rows wider than that are truncated,
    # which the evidence records rather than hides.
    return (f"SELECT COUNT(*), NVL(SUM(ORA_HASH(SUBSTR({joined},1,4000))),0) "
            f'FROM "{owner}"."{table}"')


def content(ctx: Ctx) -> list[dict]:
    owner, out = ctx.estate, []
    if not ctx.opts.checksum:
        return [finding(4, "data content", NOT_COMPARABLE, "checksums were switched off for this run")]
    cols_by_table = defaultdict(list)
    skipped_cols = defaultdict(list)
    for c in sorted((c for c in ctx.data("columns") if c.get("owner") == owner),
                    key=lambda c: c.get("column_id") or 0):
        if c.get("user_generated") == "NO" or c.get("hidden_column") == "YES":
            continue
        (cols_by_table if c["data_type"] in CHECKSUM_TYPES else skipped_cols)[c["table_name"]].append(
            c if c["data_type"] in CHECKSUM_TYPES else f'{c["column_name"]} ({c["data_type"]})')

    same, differ, skipped, unreadable = [], [], [], []
    for table in _user_tables(ctx):
        cols = cols_by_table.get(table, [])
        if not cols:
            skipped.append(f"{table}: no column of a comparable type")
            continue
        sql = _checksum_sql(owner, table, cols)
        started = time.monotonic()
        src, tgt = ctx.both(sql)
        secs = time.monotonic() - started
        if not isinstance(src, list) or not isinstance(tgt, list):
            unreadable.append(f"{table}: source {src if not isinstance(src, list) else 'ok'}, "
                              f"target {tgt if not isinstance(tgt, list) else 'ok'}")
            continue
        (s_rows, s_hash), (t_rows, t_hash) = src[0], tgt[0]
        ok = (s_rows, s_hash) == (t_rows, t_hash)
        (same if ok else differ).append(f"{table}: {s_rows} rows, checksum {s_hash}"
                                        + ("" if ok else f" vs target {t_rows} rows, checksum {t_hash}"))
        ctx.log(f"{table:<24} {s_rows:>9} rows  checksum {'match' if ok else 'DIFFER'}  ({secs:.1f}s)")
    if differ:
        out.append(finding(4, "data content", MISMATCH,
                           f"{len(differ)} table(s) hold different values", evidence={"differ": differ}))
    out.append(finding(4, "data content",
                       NOT_COMPARABLE if not same else (MATCH if not differ else NOT_COMPARABLE),
                       f"{len(same)} table(s) match on a checksum of every row" if same
                       else "no table could be checksummed on both sides",
                       why="Counts can match while values differ; this compares the values themselves.",
                       evidence={"checked": same}))
    if skipped_cols:
        out.append(finding(4, "columns not checksummed", EXPECTED,
                           f"{sum(len(v) for v in skipped_cols.values())} column(s) of types a checksum "
                           "cannot compare across versions",
                           why="LOBs, XMLType and object columns are excluded deliberately rather than "
                               "compared badly. Their tables' other columns are still compared.",
                           evidence={t: v for t, v in list(skipped_cols.items())[:20]}))
    if unreadable:
        out.append(finding(4, "data content", NOT_COMPARABLE, f"{len(unreadable)} table(s) unreadable on "
                           "one side", evidence={"tables": unreadable}))
    return out


# --------------------------------------------------------------------------- level 5

def behaviour(ctx: Ctx) -> list[dict]:
    owner, out = ctx.estate, []

    # invalid objects
    src_invalid = {(o["object_type"], o["object_name"]) for o in ctx.data("invalid_objects")
                   if o.get("owner") == owner}
    tgt_invalid = {(t, n) for t, n in ctx.rows(ctx.target(),
                   "SELECT object_type, object_name FROM dba_objects WHERE owner = :o AND status <> 'VALID'",
                   {"o": owner})}
    new_invalid = sorted(tgt_invalid - src_invalid)
    if new_invalid:
        out.append(finding(5, "objects compile", MISMATCH,
                           f"{len(new_invalid)} object(s) invalid on the target that were valid on the source",
                           evidence={"invalid": [f"{t} {n}" for t, n in new_invalid]}))
    else:
        out.append(finding(5, "objects compile", MATCH if not tgt_invalid else EXPECTED,
                           f"{len(tgt_invalid)} invalid on the target, {len(src_invalid)} on the source",
                           why="" if not tgt_invalid else "Invalid on the source too -- reproduced, not "
                               "introduced.",
                           evidence={"invalid": [f"{t} {n}" for t, n in sorted(tgt_invalid)]}))

    # grants made by the owner
    src_grants = {(g["grantee"], g["privilege"], g["table_name"]) for g in ctx.data("table_privileges")
                  if g.get("owner") == owner}
    tgt_grants = {(a, b, c) for a, b, c in ctx.rows(ctx.target(),
                  "SELECT grantee, privilege, table_name FROM dba_tab_privs WHERE owner = :o", {"o": owner})}
    missing_grants = sorted(src_grants - tgt_grants)
    discovery_account = (ctx.opts.collector_user or "").upper()
    explained = [g for g in missing_grants if g[0] == discovery_account]
    unexplained = [g for g in missing_grants if g[0] != discovery_account]
    if unexplained:
        out.append(finding(5, "grants", MISMATCH, f"{len(unexplained)} grant(s) missing on the target",
                           evidence={"missing": [f"{p} ON {t} TO {g}" for g, p, t in unexplained]}))
    else:
        out.append(finding(5, "grants", MATCH, f"{len(src_grants) - len(explained)} grant(s) present"))
    if explained:
        out.append(finding(5, "grants", EXPECTED,
                           f"{len(explained)} grant(s) to {discovery_account} are absent",
                           why="That is the read-only discovery account. It exists only on the source and has "
                               "no business on a migrated target.",
                           evidence={"missing": [f"{p} ON {t}" for _, p, t in explained][:20]}))

    # sequences
    seq_src = {s["sequence_name"]: s["last_number"] for s in ctx.data("sequences")
               if s.get("sequence_owner") == owner}
    seq_tgt = dict(ctx.rows(ctx.target(),
                   "SELECT sequence_name, last_number FROM dba_sequences WHERE sequence_owner = :o",
                   {"o": owner}))
    behind = {k: (v, seq_tgt.get(k)) for k, v in seq_src.items()
              if seq_tgt.get(k) is None or seq_tgt[k] < v}
    out.append(finding(5, "sequences", MISMATCH if behind else MATCH,
                       f"{len(seq_src)} sequence(s); "
                       + ("all at or ahead of the source" if not behind else
                          f"{len(behind)} behind the source, so they would reissue used values"),
                       evidence={"source": seq_src, "target": seq_tgt}))

    # the scheduler job Phase 4 asked to be disabled
    for job in ctx.data("scheduler_jobs"):
        if job.get("owner") != owner:
            continue
        name = job["job_name"]
        enabled = ctx.one(ctx.target(), "SELECT enabled FROM dba_scheduler_jobs WHERE owner = :o "
                          "AND job_name = :j", {"o": owner, "j": name})
        if enabled is None:
            out.append(finding(5, "scheduler job", MISMATCH, f"{name} does not exist on the target"))
        elif enabled == "FALSE" and job.get("enabled") == "TRUE":
            out.append(finding(5, "scheduler job", EXPECTED, f"{name} is disabled on the target",
                               why="Phase 7 disabled it on Phase 4's advice (RDS-006) so it could not run "
                                   "against a half-built schema. It must be re-enabled at cutover.",
                               evidence={"source": job.get("enabled"), "target": enabled}))
        else:
            out.append(finding(5, "scheduler job", MATCH, f"{name} enabled = {enabled}"))

    # materialized views
    for mv in ctx.data("materialized_views"):
        if mv.get("owner") != owner:
            continue
        row = ctx.rows(ctx.target(), "SELECT staleness, last_refresh_type, compile_state FROM dba_mviews "
                       "WHERE owner = :o AND mview_name = :m", {"o": owner, "m": mv["mview_name"]})
        if not row:
            out.append(finding(5, "materialized view", MISMATCH, f"{mv['mview_name']} is not on the target"))
            continue
        staleness, refresh_type, compile_state = row[0]
        fresh = staleness in ("FRESH", "UNKNOWN") and compile_state == "VALID"
        out.append(finding(5, "materialized view", MATCH if fresh else MISMATCH,
                           f"{mv['mview_name']}: {staleness}, last refresh {refresh_type}, {compile_state}",
                           evidence={"source_staleness": mv.get("staleness")}))

    # the external table, which only works because RDS-004 was resolved
    for ext in ctx.data("external_tables"):
        if ext.get("owner") != owner:
            continue
        name = ext["table_name"]
        src, tgt = ctx.both(f'SELECT COUNT(*) FROM "{owner}"."{name}"')
        t = tgt[0][0] if isinstance(tgt, list) else tgt
        s = src[0][0] if isinstance(src, list) else src
        if not isinstance(t, int):
            out.append(finding(5, "external table", MISMATCH, f"{name} cannot be read on the target: {t}"))
        elif isinstance(s, int) and s != t:
            out.append(finding(5, "external table", MISMATCH, f"{name}: source {s} rows, target {t}"))
        else:
            out.append(finding(5, "external table", MATCH if isinstance(s, int) else EXPECTED,
                               f"{name} reads {t} row(s) on the target",
                               why="" if isinstance(s, int) else "The source side cannot be read by the "
                                   "discovery account, which needs write access to the directory for the "
                                   "reader's log file. The target reading its file is what RDS-004 asked for.",
                               evidence={"source": s, "target": t}))

    # a text index that answers a query, not just one that exists
    text = [i for i in ctx.data("indexes") if i.get("owner") == owner and i["index_type"] == "DOMAIN"]
    for idx in text:
        table = idx["table_name"]
        col = next((ic["column_name"] for ic in ctx.data("index_columns")
                    if ic.get("index_name") == idx["index_name"]), None)
        if not col:
            continue
        # Search for a word taken from the data. The first version searched for
        # "the", matched nothing on either side, and called 0 = 0 a match -- a
        # check that cannot fail proves nothing.
        token = None
        try:
            got = ctx.rows(ctx.source(),
                           f'SELECT TO_CHAR(REGEXP_SUBSTR(TO_CHAR(SUBSTR("{col}",1,200)),\'[A-Za-z]{{4,}}\')) '
                           f'FROM "{owner}"."{table}" WHERE "{col}" IS NOT NULL AND ROWNUM = 1')
            token = got[0][0] if got else None
        except Exception as exc:  # noqa: BLE001
            ctx.log(f"could not take a search word from {table}.{col}: {str(exc).splitlines()[0]}")
        if not token:
            out.append(finding(5, "text index", NOT_COMPARABLE,
                               f"{idx['index_name']}: no word could be taken from the data to search for",
                               evidence={"table": table, "column": col}))
            continue
        sql = f'SELECT COUNT(*) FROM "{owner}"."{table}" WHERE CONTAINS("{col}", :t) > 0'
        src, tgt = ctx.both(sql, {"t": token})
        s = src[0][0] if isinstance(src, list) else src
        t = tgt[0][0] if isinstance(tgt, list) else tgt
        if not isinstance(s, int) or not isinstance(t, int):
            verdict, why = NOT_COMPARABLE, "one side could not run the search."
        elif s == 0:
            verdict, why = NOT_COMPARABLE, ("the word matched nothing on the source either, so equal counts "
                                            "here prove nothing about the target's index.")
        elif s == t:
            verdict, why = MATCH, ("The index was rebuilt on 19c in Phase 7; this asks it to answer a real "
                                   "search, rather than checking that it exists.")
        else:
            verdict, why = MISMATCH, ""
        out.append(finding(5, "text index", verdict,
                           f"{idx['index_name']}: searching for '{token}' returns source {s}, target {t}",
                           why=why, evidence={"table": table, "column": col, "token": token}))

    # database links
    for link in ctx.data("db_links"):
        if link.get("owner") != owner:
            continue
        host = ctx.one(ctx.target(), "SELECT host FROM dba_db_links WHERE owner = :o AND db_link LIKE :d",
                       {"o": owner, "d": link["db_link"] + "%"})
        out.append(finding(5, "database link", EXPECTED if host else MISMATCH,
                           f"{link['db_link']}: {'present' if host else 'absent'} on the target",
                           why="It still points at the source host (RDS-005). It exists, and it will fail "
                               "when used until it is recreated with a network path.",
                           evidence={"target_host": (host or "")[:120]}))

    # the profile Phase 7 deliberately did not copy
    src_profile = next((u.get("profile") for u in ctx.data("users") if u["username"] == owner), None)
    tgt_profile = ctx.one(ctx.target(), "SELECT profile FROM dba_users WHERE username = :u", {"u": owner})
    if src_profile and src_profile != tgt_profile:
        out.append(finding(5, "password profile", EXPECTED,
                           f"source {src_profile}, target {tgt_profile}",
                           why="Phase 7 did not copy it: SEC-006 flagged the source profile for never "
                               "expiring passwords, so carrying it over is the security owner's decision."))

    # what Phase 4 set aside for this phase
    for entry in ctx.state.get("validate_artefacts", []):
        sql = (entry.get("artefact") or {}).get("sql_on_target", "")
        if not sql.strip().upper().startswith("SELECT"):
            continue
        try:
            got = ctx.rows(ctx.target(), sql)
            out.append(finding(5, f"Phase 4 check {entry['rule_id']}", MATCH,
                               f"{entry.get('object_name')}: {got[0] if got else 'no rows'}",
                               why="Phase 4 asked for this to be measured after the load, not before.",
                               evidence={"sql": " ".join(sql.split())[:200], "rows": [list(r) for r in got[:5]]}))
        except Exception as exc:  # noqa: BLE001
            out.append(finding(5, f"Phase 4 check {entry['rule_id']}", NOT_COMPARABLE,
                               str(exc).splitlines()[0]))
    return out


LEVELS = [
    (1, "Objects", "Everything that should exist, does -- by name, not by count.", objects),
    (2, "Structure", "Columns, constraints and indexes match the source definition.", structure),
    (3, "Row counts", "Exact counts, table by table.", row_counts),
    (4, "Data content", "A checksum of every row, so equal counts cannot hide changed values.", content),
    (5, "Behaviour", "The things that only show up when the database is used.", behaviour),
]
