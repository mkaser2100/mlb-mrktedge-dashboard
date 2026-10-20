# MLB GitHub Actions — Production Workflows and Schedule Inventory

**Audit date:** October 20, 2026  
**Repository:** `mkaser2100/mlb-mrktedge-dashboard`  
**Scope:** All 16 `.yml` files in `.github/workflows/` on `main`.  
**Purpose:** Record every automatic trigger, preserve offseason shutdown/restart instructions, and document the V3 production dependency chain.

> **Source of truth:** The YAML files on the default branch determine what GitHub Actions actually schedules. This README is an audited snapshot, not an automatic live status monitor. An active YAML schedule may still be disabled at the GitHub Actions UI level. GitHub Actions cron uses UTC and may be delayed.

## 1. Automatic schedule inventory

| Workflow file | Automatic trigger(s) in YAML | UTC cron | Eastern time in EDT (UTC−4) | Eastern time in EST (UTC−5) | Audit status |
|---|---|---|---|---|---|
| `daily-mlb-v3-production.yml` | `schedule` | `45 10 * * *` | **6:45 AM daily** | **5:45 AM daily** | **Cron enabled** |
| `mlb-power-model-daily.yml` | `schedule` + `workflow_run` | `37 12 * * *` / `7 13 * * *` / `37 13 * * *` | **8:37, 9:07, 9:37 AM daily** | **7:37, 8:07, 8:37 AM daily** | **Cron + event trigger enabled** |
| `refresh-mlb-official-lineups.yml` | None (manual only) | — | — | — | **Automatic scheduling off** |
| `load-mlb-market-odds.yml` | None (manual only); commented-out example `0 14 * * *` | — | — | — | **Automatic scheduling off** |

**Important:** The three Power Models cron expressions are redundant fallback opportunities, not necessarily three scoring executions: the workflow contains a status/readiness gate. It also has a separate `workflow_run` trigger on completion of `Refresh MLB V3 Features`. The gate can skip work, but the workflow can still be invoked automatically.

**Time-zone warning:** The existing cron expressions are fixed UTC values; they **do not automatically adjust** for US daylight saving time. The EST column shows what the same cron expressions mean in winter. No active cron in the audited files specifies a season-only date restriction.

## 2. Complete inventory of all 16 workflow files

| Workflow file | Direct `schedule` | Other automatic trigger | Manual `workflow_dispatch` | Reusable `workflow_call` | Operational note |
|---|---|---|---|---|---|
| `daily-mlb-v3-production.yml` | **Yes** | No | Yes | No | Master production orchestrator; calls child workflows |
| `daily-reds-batting.yml` | No | No | Yes | No | Retired stub; do not schedule |
| `load-all-mlb-phase-1.yml` | No | No | Yes | Yes | Child of master pipeline |
| `load-mlb-market-odds.yml` | No | No | Yes | No | Schedule commented out pending live Oddize validation |
| `load-mlb-pitcher-k-actuals.yml` | No | No | Yes | Yes | Callable/manual; no independent schedule |
| `load-mlb-statcast.yml` | No | No | Yes | Yes | Child of master pipeline |
| `load-v3-hit-actuals.yml` | No | No | Yes | Yes | Child of master pipeline |
| `mlb-power-model-daily.yml` | **Yes** | **Yes: `workflow_run`** | Yes | Yes | HR + total-bases shadow scoring; has 3 fallback crons |
| `pitcher-k-phase2-research.yml` | No | No | Yes | No | Manual research |
| `pitcher-k-phase3-train.yml` | No | No | Yes | No | Manual training/validation |
| `refresh-mlb-hit-board-performance.yml` | No | No | Yes | Yes | Child of master pipeline |
| `refresh-mlb-official-lineups.yml` | No | No | Yes | No | Manual only; its former hourly schedule has been removed |
| `refresh-mlb-pitcher-k-features.yml` | No | No | Yes | Yes | Callable/manual |
| `refresh-mlb-v3-features.yml` | No | No | Yes | Yes | Child of master pipeline; completion can trigger Power Models |
| `run-v3-hit-model-model.yml` | No | No | Yes | Yes | Child of master pipeline |
| `score-mlb-pitcher-k.yml` | No | No | Yes | Yes | Callable/manual |

**Scope note:** `workflow_call` means another workflow may invoke a file automatically as part of a parent run; it does **not** mean that file independently has a cron. This audit identifies triggers declared in workflow YAML, not GitHub Actions UI enablement, external API dispatches, or other repositories.

## 3. V3 production pipeline and dependencies

The authoritative V3 production entry point is:

`daily-mlb-v3-production.yml`

```text
Load All MLB Phase 1
        |
        +--------------------+
        |                    |
        v                    v
Load MLB Statcast      Load V3 Hit Actuals
        |
        v
Refresh MLB V3 Features
        |                    |
        +---------+----------+
                  |
                  v
          Run V3 Hit Model
                  |
                  v
 Refresh MLB Hit Board Performance
```

This is the core V3 chain documented in the original README. The master workflow also includes HR/total-bases actuals work; the workflow YAML is authoritative for its full job graph.

The V3 model must not run until both V3 Actuals and V3 Features succeed. GitHub Actions `needs:` dependencies enforce order. A failed prerequisite should block dependent jobs. Do not restore independent `schedule` or `workflow_run` triggers to the V3 child workflows.

**Production principle:** One master V3 scheduler, one dependency graph, and one authoritative production result. The separate Power Models workflow is an additional scheduled/event-driven pipeline and must be managed separately during offseason shutdown.

## 4. Offseason shutdown procedure

**Goal:** Stop recurring MLB-related compute while keeping manual runs and the code available for the next season.

### A. Stop the master V3 pipeline

In `.github/workflows/daily-mlb-v3-production.yml`, remove or comment out this top-level trigger:

```yaml
  schedule:
    - cron: "45 10 * * *"
```

Keep `workflow_dispatch:` and all jobs. This stops the master's daily cron and, consequently, its automatically invoked child chain. It does not prevent a deliberate manual run.

### B. Stop Power Models completely

In `.github/workflows/mlb-power-model-daily.yml`, remove or comment out **both** automatic trigger sections:

```yaml
  workflow_run:
    workflows:
      - "Refresh MLB V3 Features"
    types: [completed]

  schedule:
    - cron: "37 12 * * *"
    - cron: "7 13 * * *"
    - cron: "37 13 * * *"
```

**Why both?** If `workflow_run` remains, a successful or completed V3 feature refresh can still launch the Power Models workflow even after all its crons are removed. Keep `workflow_dispatch:` and `workflow_call:` if manual and reusable operation should remain available.

### C. Leave the already-disabled schedules off

- `refresh-mlb-official-lineups.yml` is currently manual only. No change required.
- `load-mlb-market-odds.yml` has only a **commented-out** cron example; leave it commented out unless separately validated and approved.
- No other file in the audited folder has an independent active cron.

### D. Verify shutdown

1. Commit the changes to `main`.
2. Reopen both workflow YAML files on `main`; verify neither contains an active top-level `schedule:`. Verify Power Models also has no active `workflow_run:`.
3. Confirm `workflow_dispatch:` remains available in GitHub **Actions** for both workflows.
4. Review GitHub Actions run history after the next former scheduled time. No new `schedule` runs should be created for these two workflows; pre-existing queued or running jobs may still need attention.
5. Check for external triggers, manually invoked parent workflows, and any other repositories if activity continues. YAML edits alone do not stop external dispatches or manually initiated work.

**Do not disable the entire workflow in the GitHub UI if the goal is to preserve manual execution.** Removing only automatic triggers is preferable for this use case.

## 5. Next-season restart procedure

1. Decide whether to retain the same UTC cron times. For desired fixed Eastern times across DST changes, adjust the schedule seasonally or implement an appropriate time-zone-aware strategy; GitHub cron alone does not follow Eastern DST.
2. Restore the original `schedule` block in `daily-mlb-v3-production.yml`:

```yaml
  schedule:
    - cron: "45 10 * * *"
```

3. Restore the original Power Models fallback schedule **only if still needed**:

```yaml
  schedule:
    - cron: "37 12 * * *"
    - cron: "7 13 * * *"
    - cron: "37 13 * * *"
```

4. Decide explicitly whether to restore the Power Models `workflow_run` trigger:

```yaml
  workflow_run:
    workflows:
      - "Refresh MLB V3 Features"
    types: [completed]
```

5. Commit to `main`, verify the triggers in YAML, and check that workflows are enabled in GitHub Actions.
6. Before relying on automation, manually validate Phase 1, Statcast, V3 Features, V3 Actuals, V3 predictions, serving cache, and Power Models outputs for a live eligible game day. Check secrets, APIs, year-specific settings, model readiness, and any season-specific data assumptions.
7. Observe the first scheduled production day, inspect the **first failed job** if there is an error, and verify downstream dependencies and data integrity.
8. Re-audit all workflow files for new or changed schedules and update this inventory.

**Restart checklist**

- [ ] Confirm desired first production date and Eastern start times
- [ ] Restore master V3 cron
- [ ] Restore Power Models fallback crons if required
- [ ] Decide whether Power Models `workflow_run` should be enabled
- [ ] Leave Official Lineups manual unless its hourly refresh is intentionally needed
- [ ] Keep Market Odds schedule disabled until validated
- [ ] Validate manual end-to-end run and data quality
- [ ] Confirm first scheduled run and update this README

## 6. Historical official-lineups schedule (reference only)

The previously removed `refresh-mlb-official-lineups.yml` automatic schedule was:

```yaml
  schedule:
    - cron: "15 14-23 * * *"
    - cron: "15 0-1 * * *"
```

These UTC expressions ran at **10:15 AM through 9:15 PM EDT, hourly** (12 opportunities). They are **not currently active**. If you reintroduce them, check for changes to confirmed-lineup availability, overlapping scoring jobs, daylight saving time, and game start times before enabling them.

## 7. Troubleshooting and integrity rules

- Inspect the first failed upstream stage rather than downstream skipped jobs.
- V3 sequence: Phase 1 → Statcast and Actuals → Features → V3 Model → Performance.
- A legitimate MLB off-day must be distinguished from stale or missing data; do not fabricate predictions.
- Eligible games with missing features or zero predictions should fail loudly.
- Validate official model activation, expected game coverage, feature contributions, explanations, and serving-cache readiness.
- Keep concurrency protection for expensive writes and scoring jobs.
- For historical backfills, use intentional manual dispatches and verify date inputs.

## 8. Audit methodology and maintenance

Reviewed the top-level trigger declarations of every workflow YAML in `.github/workflows/` on the `main` branch as of October 20, 2026. Classified `schedule`, `workflow_run`, `workflow_dispatch`, and `workflow_call`, including distinguishing commented-out cron examples from active triggers. UTC-to-Eastern conversions use EDT (UTC−4) and EST (UTC−5). The inventory reflects configured triggers, not proof of successful execution or actual job start times.

**Maintenance rule:** Update this schedule inventory whenever a workflow's automatic triggers change. Re-audit before each season's shutdown and restart.
