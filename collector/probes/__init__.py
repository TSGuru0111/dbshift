from . import (
    constraints,
    dataprofile,
    features,
    identity,
    indexes,
    objects,
    partitions,
    plsql,
    programmatic,
    security,
    storage,
    tables,
)

PROBES = (
    identity,
    objects,
    tables,
    indexes,
    constraints,
    storage,
    partitions,
    plsql,
    programmatic,
    security,
    features,
    dataprofile,
)


def probes_for(source_engine=None):
    """The probe set for one source engine.

    Oracle's probes live here and MySQL's in `collector/probes_mysql/`, each
    module a `collect(s, owners)` returning the same dataset names. That sameness
    is the contract the rest of the pipeline rests on: `assess/loader.py` builds
    its SQLite tables from dataset names, and the `v_user_*` views and 51 rules
    are written against them. A MySQL probe that renamed a dataset would not fail
    here -- it would fail as "no such column" inside a rule, three phases later.
    `selftest_probes_mysql` asserts the parity directly for that reason.

    Imported lazily so a machine without `pymysql` can still run the Oracle path.
    """
    import engines

    if engines.is_mysql(source_engine):
        from ..probes_mysql import PROBES as MYSQL_PROBES

        return MYSQL_PROBES
    return PROBES
