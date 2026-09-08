# Glossary

**CDB / PDB** — Container Database / Pluggable Database. Oracle 12c+ multitenant
architecture. `XE` is the container, `XEPDB1` the pluggable one. Application
objects go in the PDB. Connecting to the wrong one is the most common setup error
in this project.

**CDC** — Change Data Capture. While the initial bulk copy runs, new changes keep
arriving on the source; CDC streams those across so the target stays current.
Requires ARCHIVELOG mode and supplemental logging on Oracle.

**Full load** — the initial bulk copy of existing data, as distinct from CDC.

**DMS** — AWS Database Migration Service. Does both full load and CDC.

**Homogeneous migration** — same engine on both sides (Oracle to Oracle). No
schema conversion, no data-type mapping, no PL/SQL rewriting. This project is
homogeneous, which is why the value sits in assessment rather than conversion.

**SE2 / EE** — Oracle Standard Edition 2 / Enterprise Edition. On RDS, SE2 is
available license-included and caps at 16 vCPU, with no partitioning, TDE,
Advanced Compression or parallel query. **EE on RDS is BYOL only** — there is no
license-included Enterprise option. On AWS, two vCPUs count as one Oracle
processor licence.

**BYOL** — Bring Your Own Licence.

**OLA / DB OLA** — AWS Optimization and Licensing Assessment. A **funded,
partner-delivered programme, not a callable service** — no API, no
CloudFormation resource. This platform does not call it; it reproduces its
deliverables (estate inventory, utilization baseline, rightsizing, licence
position, TCO) automatically.

**ACU** — Aurora Capacity Unit, the billing unit for Aurora Serverless v2.

**OCU** — OpenSearch Compute Unit. Relevant only as a cost trap: a Classic
OpenSearch Serverless collection has a 2-OCU floor, roughly $345/month at zero
traffic.

**Readiness certificate** — the gate before production cutover. Overall score
plus schema compatibility, data quality, object conversion, performance and
security risk, blocker count. Any critical finding caps the overall score at 60.

**Blocker gate** — `has_critical_blockers`. A single boolean the state machine
keys off. Nothing past phase 5 runs while a critical finding is open. This is the
human-in-the-loop guarantee expressed as a database column rather than a policy
document.
