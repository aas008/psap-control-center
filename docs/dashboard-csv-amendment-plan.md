# Dashboard CSV Amendment Plan — Level 1

**Goal:** let Control Center correct the staging dashboard CSV after a run, without hand-editing S3.

**Scope:** three admin operations on `consolidated_dashboard.csv`:

| Operation | Input | What changes |
|---|---|---|
| **Rename version** | One or more dashboard UUIDs + new label | `version` on every row for those UUIDs |
| **Import from MLflow** | MLflow run ID/URL, or a dashboard UUID | Adds the run's rows; skip or replace existing |
| **Delete rows** | One or more dashboard UUIDs | Removes every row for those UUIDs |

## The vision

![Data flow](assets/dashboard-csv-flow.svg)

Forge produces a per-run `dashboard.csv` and appends it to S3. The same artifact is
exported to MLflow. Control Center reads the CSV, resolves the run, previews the
change, writes a backup, and updates S3. The staging dashboard picks up the change
on its next read.

## What the user sees

### Push a finished job

![Job detail confirmation](assets/dashboard-csv-job-detail.svg)

On a Succeeded RHAIIS job, an admin gets **Push to Staging Dashboard**. The
confirmation shows the dashboard UUID, MLflow run, target S3 key, and exactly how
many rows will be added. If the UUID is already in the CSV, the action is disabled
and points to the correction panel.

### Fix the dashboard across runs

![Dashboard CSV admin panel](assets/dashboard-csv-admin-panel.svg)

The admin **Dashboard CSV** tab has the three operations side by side. Every
operation is preview-first; the preview shows the rows, the count, and the backup
path. Delete requires typing `DELETE`.

## How it works

1. **One key.** The CSV `uuid` is `tests.rhaiis.run_uuid`. Live jobs expose it as
   the `rhaiis.run-uuid` annotation; archived jobs keep it in
   `fjob_status...relevantDeployments[].runUUID`.
2. **Two producers, one object.** Forge owns the per-run artifact. Control Center
   owns the consolidated CSV edit.
3. **No lost updates.** Both writers use S3 ETag conditional writes and retry.
   Every Control Center write backs up the CSV first.
4. **No guessing runs.** Control Center resolves the MLflow run from `mlflow_url`
   or a new `rhaiis.run_uuid` MLflow tag; if it cannot, it stops and says what is
   missing.
5. **Admin only.** Preview, backup, audit, and typed confirmation for destructive
   actions.

## Who builds what

| Side | Changes |
|---|---|
| **Control Center** | S3 CSV service, MLflow artifact fetch, admin endpoints, job-detail button, Dashboard CSV tab, audit |
| **Forge** | Tag runs with `rhaiis.run_uuid`; dedupe and conditional write in `sync_csv_to_s3` |

## Delivery

| Step | Outcome |
|---|---|
| 0 | Forge tag + safe append |
| 1 | Control Center backend: CSV service + MLflow fetch + tests |
| 2 | Control Center API: preview, rename, import, delete, audit |
| 3 | Control Center UI: job button + admin tab |
| 4 | Deployment secret + dry-run + rollback runbook |

## Decisions needed

- **S3 write credentials in Control Center:** yes, or fall back to a Forge batch
  job?
- **Import default:** skip existing rows (recommended) or replace?
- **Placement:** admin tab under Fournos Dashboard (recommended) or Settings?

The detailed API, algorithms, file map, and test plan are in
[Level 2](dashboard-csv-amendment-plan-detailed.md).
