# DBShift — demo card

One page. Full version: [DEMO-GUIDE.md](DEMO-GUIDE.md).

> **AI proposes. Deterministic rules decide. A person approves anything irreversible.**

---

## The ten phases

| # | Phase | One line | Say this |
|---|---|---|---|
| **1** | Discover | Catalogue → SQLite mirror | "Read-only. No table data leaves the source." |
| **2** | Assess | 51 rules over the mirror | "Rules are data, not code. Every finding cites its evidence. Recall 100%." |
| **4b** | Convert | PL/SQL → PL/pgSQL, **compiled on real PostgreSQL**, rolled back | "Most tools hand you a report. We compile it. 6 of 8 passed. One is broken on the source — we won't migrate broken code." |
| **3** | Target | **Client picks** Oracle or PostgreSQL | "The effort number comes from 4b — measured by compiling, not estimated." |
| **4** | Remediate | A fix per finding; model where judgement is needed | "Model drafted 18. All blocked or rejected. **None applied.**" |
| **5** | Gate | Verdict **per downstream phase** | "CDC and cutover blocked; provision and full load clear. Each says what clears it." |
| **6** | Provision | Renders + checks; deploy is separate | "Every value says which phase decided it. 8-hour TTL." |
| **7** | Migrate | AWS DMS + **residue list** | "Data Pump writes Oracle-only format. And we name what DMS leaves behind." |
| **8** | Validate | Five levels, all SELECTs | "Cross-engine: canonical text form, or a perfect migration would show failures." |
| **9** | Cutover | Nine requirements → certificate | "It refuses — and every line says what would clear it." |
| **10** | Report | Built from this run only | "Nothing written by hand." |

**Order note:** 4b runs before 3 so phase 3 has a *measured* effort figure.

---

## The three moments to land

1. **4b** — "We compile, not just report."
2. **4** — "The model proposed; the gates refused."
3. **8 + 9** — "It won't certify what it can't verify."

---

## Expected results

| Phase | Expect |
|---|---|
| 1 | ~30–40s |
| 2 | 99 findings / 33 issues, recall 100% |
| 4b | 8 objects, **6 compiled and rolled back** |
| 4 | ~17–18 model-drafted, **none applied** |
| 5 | **Halt**, per-phase |
| 8 | **mismatch** ← correct |
| 9 | **not ready**, 3 unmet |

*Model count moves 17↔18 between runs — it's non-deterministic. The gates aren't.*

---

## Hard questions

**"Why is validation failing?"**
"The target is empty — DMS can't reach an on-premises Oracle from AWS. The system
refused to certify a migration that hasn't happened. That's the product."

**"So the AI isn't doing much?"**
"It drafted 18 fixes; deterministic gates rejected every one. An AI that could
approve its own SQL against your production database is a liability, not a feature."

**"How is this different from SCT + DMS?"**
"SCT gives a report — we compile on real PostgreSQL. DMS goes quiet about what it
left behind — we enumerate and route it. Neither refuses to cut over when the
evidence isn't there."

**"unreachable vs mismatch?"**
"`unreachable` = I couldn't look. `mismatch` = I looked, here's what's wrong."

---

## Run it

```powershell
# console (leave running)
.\.venv\Scripts\python.exe -m uvicorn web.server:app --host 127.0.0.1 --port 8765

# narrated walkthrough
cd scripts\console-test
node drive_live_demo.js        # --fast, or DEMO_PAUSE=6000
```

## Pre-flight

- [ ] **IP check** — SG admits specific /32s; a network change → phase 8 `unreachable`
      ```powershell
      aws ec2 authorize-security-group-ingress --group-id sg-03c28c88be67a8c9e `
        --protocol tcp --port 5432 --cidr "$(curl -s https://checkip.amazonaws.com)/32" --region ap-south-1
      ```
- [ ] **Port 8765** — a stale console 404s on `/api/engine`, PostgreSQL button looks broken
- [ ] Oracle 1521 + PostgreSQL 5432 up
- [ ] `python -m bedrock.verify --quiet` → 2/2

## Safety (if asked)

Read-only until approved · compile then roll back · one writer (`APPROVED` only) ·
`Purpose=DMA` tags · 8h TTL · kill switch · approver from AWS identity, never typed ·
source never written.
