# Conventions

## SQL

- Run scripts in SQL Developer with **F5** (Run Script) and **nothing
  highlighted**. Ctrl+Enter runs only the statement under the cursor; a
  selection runs only the selection. This has silently caused a full data load
  to do nothing.
- Verify with a `COUNT(*)` query. Do not trust the output pane — a script can
  print its closing banner having executed almost none of itself.
- No substitution variables (`&name`). They interact badly with `SET DEFINE`
  and behave differently between SQL*Plus and SQL Developer. Hardcode values and
  edit in place.
- No OS-specific paths in scripts where avoidable. Use built-in tablespaces.
- Scripts must be re-runnable: lead with a cleanup block and expect
  "does not exist" errors on a fresh database.

## Python

- `oracledb` thin mode. No Instant Client dependency.
- Credentials from environment or keychain, never from a file in git.
- Structured logging: SQL executed and row counts, never data or credentials.

## Metadata

- Nine schemas: `migration_control`, `source_inventory`, `target_inventory`,
  `assessment`, `remediation`, `migration`, `validation`, `reporting`, `audit`.
- **Append-only.** Every row carries a `collector_run_id`; the latest run wins
  and history is preserved.
- Every Bedrock call writes one row to `audit.agent_decision`: model id, input
  hash, structured output, confidence, validator verdict. Store the decision and
  the evidence, not the model's internal reasoning.

## AI calls

- Strict JSON output against a defined schema. Never free-form.
- Every output passes a validator before anything acts on it.
- Where the model and the rules engine disagree, **the rules win** and the
  disagreement is logged as a finding for human review. That logged disagreement
  is a feature — it is evidence to a client that the AI is bounded.
