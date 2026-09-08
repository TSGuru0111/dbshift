# Data

## `dbmig_golden.dmp`

Data Pump export of `DBMIG_APP`, taken **after** defect seeding. This is the
reset button — restore from here rather than rebuilding from SQL.

Not committed to git (see `.gitignore`); ~500 MB. Keep a copy somewhere durable.

### Restore

```
impdp dbmig_app/DbMig2026App@localhost:1521/XEPDB1 schemas=DBMIG_APP ^
  directory=DBMIG_EXT_DIR dumpfile=dbmig_golden.dmp logfile=dbmig_golden_imp.log
```

### Re-export after any deliberate change

Delete the old dumpfile first — `expdp` refuses to overwrite.

```
expdp dbmig_app/DbMig2026App@localhost:1521/XEPDB1 schemas=DBMIG_APP ^
  directory=DBMIG_EXT_DIR dumpfile=dbmig_golden.dmp logfile=dbmig_golden_exp.log
```

`DBMIG_EXT_DIR` resolves to `C:\oracle\ext_data`. Run from Command Prompt, not
from SQL Developer.
