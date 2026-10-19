# Data contract

Status: **DRAFT** — needs sign-off from A, B and C (Stage 0 handover).

Shared schema between the runner (A), the collector (B) and the ML code (C).
To change anything here, open a PR that edits this file and tag the other two.

## Conventions
- `function_id` = Lambda function name, `{workload}-{memory}-{arch}-{packaging}`,
  e.g. `data-512-arm64-zip`.
  - `workload`: `tiny` | `web` | `data` | `ml`
  - `memory`: MB, one of 128, 256, 512, 1024, 2048 (core sweep; extend here if needed)
  - `arch`: `x86_64` → written as `x86` in names, `arm64`
  - `packaging`: `zip` | `layer` | `image`
- Timestamps `ts`: UTC ISO 8601 with milliseconds, `2026-10-17T14:03:22.123Z`
  (sorts correctly as a string).
- Durations: milliseconds as numbers (float) unless the field name says `_us`.
- Money: USD as numbers, not rounded.
- Missing value: leave the attribute out (don't write `0` or `""`).
- DynamoDB billing mode: on-demand (PAY_PER_REQUEST) for all tables.

## Table `invocations`
One row per invocation (warm or cold).

| Field | Type | Key | Written by | Notes |
|---|---|---|---|---|
| function_id | S | PK | both | |
| request_id | S | SK | both | Lambda request id: REPORT line (collector) = invoke response id (runner) |
| ts | S | | collector | REPORT log event timestamp |
| workload | S | | collector | parsed from `function_id` |
| memory_mb | N | | collector | |
| arch | S | | collector | `x86` / `arm64` |
| packaging | S | | collector | `zip` / `layer` / `image` |
| package_mb | N | | runner | deployed package size |
| n_imports | N | | collector | modules imported during INIT; set only when the import lines and REPORT arrive in the same batch |
| log_stream | S | | collector | cold starts only; joins to `import_profiles` |
| billed_restore_ms | N | | collector | SnapStart only |
| forced | BOOL | | runner | true for runner-forced cold starts |
| idle_gap_min | N | | runner | minutes since previous invoke; absent for forced cold starts |
| is_cold | BOOL | | collector | REPORT contains `Init Duration` or `Restore Duration` |
| init_ms | N | | collector | `Init Duration`; absent when warm |
| restore_ms | N | | collector | `Restore Duration` (SnapStart only) |
| duration_ms | N | | collector | `Duration` |
| billed_ms | N | | collector | `Billed Duration` |
| max_mem_mb | N | | collector | `Max Memory Used` |
| init_cost_usd | N | | collector | from `ml/cost.py` + `ml/pricing.json` |
| strategy | S | | runner | `on_demand` / `snapstart` / `provisioned` |
| run_id | S | | runner | experiment batch id, e.g. `core-sweep-2026-10-20` |

Both writers use `UpdateItem` on the same key (`function_id`, `request_id`),
so whichever arrives first, the row ends up with both sets of fields.
The SK is `request_id` alone (not `ts#request_id`) because the runner knows
the request id but not the log timestamp.

## Table `import_profiles`
One row per (function, module), from the latest cold start's
`PYTHONPROFILEIMPORTTIME` output. Only modules with `cumulative_us >= 1000`
are kept (collector env `MIN_IMPORT_US`); a full pandas profile is ~1000
modules per cold start, which would dominate the DynamoDB bill.

| Field | Type | Key | Notes |
|---|---|---|---|
| function_id | S | PK | |
| module | S | SK | full dotted name, e.g. `pandas.core.frame` |
| self_us | N | | self time, microseconds |
| cumulative_us | N | | cumulative time, microseconds |
| log_stream | S | | which cold start it came from (joins to `invocations.log_stream`) |
| ts | S | | |

## Table `recommendations`
One row per function, written by `ml/recommender.py`.

| Field | Type | Key | Notes |
|---|---|---|---|
| function_id | S | PK | |
| strategy | S | | `on_demand` / `snapstart` / `provisioned` |
| pred_init_p50_ms | N | | model A, alpha 0.5 |
| pred_init_p90_ms | N | | model A, alpha 0.9 |
| monthly_cost_usd | N | | for the chosen strategy |
| p95_ms | N | | expected p95 latency with that strategy |
| latency_target_ms | N | | input target |
| traffic_rps | N | | traffic assumption used |
| generated_at | S | | ts |

## CloudWatch EMF metrics (collector)
Namespace `ColdStartObservatory`. Dimensions: `workload`, `arch`, `packaging` only.

| Metric | Unit |
|---|---|
| InitDurationMs | Milliseconds |
| ColdStart | Count (1 cold / 0 warm) |
| ColdStartCostUSD | None |

## Sign-off
- [ ] A —
- [ ] B —
- [ ] C —
