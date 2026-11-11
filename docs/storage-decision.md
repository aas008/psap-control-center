# Benchmark storage decision: CSV vs OpenSearch vs Postgres

Team-facing summary of the storage evaluation for the PSAP benchmark dashboard.
The detailed research, prototype scripts, and raw measurements are in
[`csv-opensearch-research.md`](csv-opensearch-research.md) and
[`../prototype/`](../prototype/).

**Decision requested:** choose the system of record for benchmark metadata.

**Recommendation:** **Postgres as the system of record.** Keep S3 for artifacts
(logs, traces, model files), and treat the consolidated CSV as an export during
the transition. OpenSearch is viable as a derived read/search index or for
ad-hoc exploration, but not as the system of record.

---

## 1. Why not CSV

Already agreed; listed here for completeness.

- **Lost updates.** Forge downloads the whole 30 MB CSV, appends rows, and
  re-uploads it. Two jobs finishing together overwrite each other — observed by
  Sai on 2026-11-03.
- **No dedupe.** Roughly 464 natural-key groups and ~7,770 surplus rows on
  `(uuid, run, measured concurrency)` (upper bound; some may be legitimate
  re-runs).
- **Manual edits.** Renaming versions, importing MLflow rows, and deleting
  duplicates require downloading the CSV, editing 10k+ rows in Sheets, and
  re-uploading.
- **Schema fragility.** Positional backfills have written `uuid` into
  `ttft_p99` and `mlflow_run_id` into `turn_index`; `RHAIIS_FIELDNAMES` emits 54
  columns while the consolidated file has 56 (`turn_index`, `env_vars`).
- **No transactions or validation.** A failed edit leaves no rollback and no
  record of what changed.

CSV is the baseline, not a candidate.

## 2. Why not OpenSearch

We built a local OpenSearch 3.9.0 + Dashboards 3.9.0 instance and tested it
against the **current staging CSV**: 35,998 rows × 56 columns, plus the llm-d
CSV (454 rows × 53 columns). Prototype scripts are in
[`../prototype/`](../prototype/).

### What worked

OpenSearch can store and query this data. Measured results:

- 35,998 documents ingested in ~5 s; CSV round-trip with **0 cell mismatches**.
- The Streamlit dashboard and the agent's `load_rhaiis_data()` read the exported
  CSV unchanged.
- Exact version count **1,829/1,829**; case-insensitive model substring
  **3,797/3,797**; exact top-10 throughput.
- Rename version by UUID: 5 rows updated in ~113 ms.
- Per-run `dashboard.csv` bulk-ingested in ~30 ms; redelivery is idempotent.
- llm-d index + read alias: **36,452 documents** across both indices;
  cross-project top-5 models matched pandas exactly.
- Discover gives a usable ad-hoc table with saved searches and CSV export.

So the objection is not "OpenSearch cannot do it." It is that the operations we
care about become harder, and we take on a new system to operate.

### Pain points found during setup and testing

**1. You still design and maintain a schema.**

- Mappings are mandatory for querying. We used `dynamic: strict` to avoid
  mapping explosion; the default field cap is 1,000.
- `keyword` vs `text` changes query semantics; choosing wrong is easy.
- **Field types cannot change in place.** Changing `version` from keyword to
  text was rejected. The fix is a new index, a reindex, and an alias swap.

**2. Aggregations are approximate by default.**

- A `terms` aggregation ordered by average throughput returned **5 rows for
  `vLLM-0.22.0` instead of 62**. We had to set `shard_size: 1000` to match
  pandas.
- This is a per-query setting. Every new aggregation needs to remember it.

**3. Deep pagination is capped.**

- `from: 10000` is rejected by `index.max_result_window`.
- Full exports need `search_after` or a point-in-time cursor — extra code that
  pandas does not need.

**4. No multi-row transactions.**

- Each document update is atomic, but `update_by_query`, `delete_by_query`, and
  bulk are **not atomic across documents**.
- SQL `DELETE` is unsupported in OpenSearch 3.x.
- A rename that dies halfway leaves some rows on the old version and some on the
  new. "Replace a run" is delete-then-insert, two operations with a window in
  between.
- This is the biggest gap: rename-by-UUID, replace-a-run, and delete-by-UUID +
  profile are exactly the issue #94 operations.

**5. The write path is still a code change.**

- Bulk indexing a per-run file works and is idempotent, but Forge must call
  OpenSearch, maintain mappings, and handle retries and partial batches.
- The S3 race is fixed only after that work lands. It is not a drop-in.

**6. Multi-project bookkeeping is comparable, not better.**

- Index per project + alias works, but shared fields must have identical
  mapping types; cross-index aggregations fail at query time otherwise.
- Adding a project means create index + mapping + alias action. Postgres needs
  create table + update view. The bookkeeping is comparable.

**7. The UI is for exploring, not editing.**

- Discover, filters, saved searches, and saved objects work well for looking at
  data.
- `_source` is not searchable; we hit the `_source field is not searchable`
  error while filtering.
- It is read/explore, not a replacement for editing rows.

**8. New operational surface.**

- Cluster, shards/replicas, mappings, reindex runbook, Dashboards saved objects,
  snapshots, tenant/access management.
- A lab instance exists, but there is no integration, mapping ownership, or
  reindex runbook yet.

**9. Capabilities we do not use.**

- Relevance ranking, analyzers, fuzzy matching, synonyms, highlighting.
- SQL/PPL support is limited; there are no joins (not needed here).
- We would pay for a search engine while using it as a table.

## 3. Why Postgres

Postgres matches the operations the team actually performs.

- **Transactions for corrections.** Rename by UUID, replace a run, and delete by
  UUID + profile are `BEGIN … COMMIT`. All rows change or none do; a crash rolls
  back cleanly.
- **Exact aggregates.** `GROUP BY` is exact; no `shard_size` tuning.
- **No pagination cap.** Full exports are a normal query.
- **SQL + `ILIKE`.** Substring matching on model/version works the way the team
  expects; the team already leans SQL.
- **Schema handling.** Stable columns stay typed; drifting or modality-specific
  fields go in `JSONB`. `ADD COLUMN` is cheap; `ALTER COLUMN TYPE` is one
  migration, not a reindex.
- **Multi-project.** Table per project plus a `UNION ALL` view. Explicit,
  type-checked, and comparable to an index + alias.
- **Write path.** `INSERT … ON CONFLICT` per row fixes the race and gives a
  transactional batch per run.
- **Operations.** Already deployed for agent state
  (`openshift-manifests/02-postgresql.yaml`); backups and operations are known.

### What Postgres does not solve

- **No bundled data browser.** Needs Metabase/Superset/Grafana/pgAdmin.
- **Full-text/fuzzy search is weaker.** Not a current requirement.
- **Sparse fields need `JSONB` discipline.** Weaker typing and GIN indexes for
  fast queries.
- **It was not prototyped in this pass.** Postgres claims here are based on
  standard behavior and the existing deployment, not a local benchmark. A short
  bake-off on the same corpus and queries would confirm them.

## 4. Scorecard

Confidence levels:

- **High** — measured on the actual corpus in the prototype, or unambiguous,
  well-documented behavior.
- **Medium** — standard behavior, but not tested on our corpus or deployment.
- **Low** — forward-looking; depends on requirements we do not have today.

| What we need | CSV | OpenSearch | Postgres | Winner | Confidence | Basis |
|---|---|---|---|---|---|---|
| Concurrent writes without lost updates | No | Yes, per document | Yes, per row; transactional batches | **Postgres** (OpenSearch ties on the race) | High | OpenSearch measured; Postgres standard |
| Rename / replace / delete a run atomically | No | No; non-atomic across documents | Yes, one transaction | **Postgres** | High | OpenSearch measured; Postgres ACID standard |
| Exact counts and group-bys | Yes (pandas) | Only with `shard_size`; default approximate | Yes | **Postgres** (CSV also exact) | High | OpenSearch measured |
| Full export and deep pagination | Yes | Needs `search_after`/PIT; 10k cap | Yes | **Postgres** (CSV also) | High | OpenSearch measured |
| Add a column | Fragile | Easy: `PUT _mapping` | Easy: `ADD COLUMN` | **Tie** | High | OpenSearch measured; Postgres standard |
| Change a column type | Hand-edit | New index + reindex + alias swap | One `ALTER` | **Postgres** | High | OpenSearch measured; Postgres standard |
| Cross-project queries | No | Index per project + alias | Table per project + view | **Tie** | Medium | OpenSearch measured; Postgres view not built |
| Sparse / per-modality fields | Breaks | Natural sparse documents | `JSONB` | **Slight OpenSearch** or tie | Medium | OpenSearch measured; Postgres JSONB standard |
| Ad-hoc browsing | Sheets (familiar, risky) | Discover + saved searches | Needs an external tool | **OpenSearch** | High | OpenSearch used |
| Operational burden | Low but fragile | New cluster, mappings, reindex runbook | Already deployed | **Postgres** | Medium | Existing deployment; no lab OpenSearch integration |
| Write-path code change | None | Bulk API + mappings | Insert/upsert + schema | **Tie** | Medium | OpenSearch prototype; Postgres not built |
| Team familiarity | High | Low (one expert) | Medium-High (SQL) | **Postgres** | High | Team discussion |
| Full-text / fuzzy search | No | Native strength | Weaker (`pg_trgm`) | **OpenSearch** | High | Not a current requirement; low relevance today |
| Scale at current volume | Fine | Fine | Fine | **Tie** (not a differentiator) | High | 100–2,000 rows/month; 36k today |

## 5. Recommendation and next steps

### Recommendation

1. **Adopt Postgres as the system of record** for benchmark metadata.
2. **Keep S3** for logs, profiler traces, model artifacts, and MLflow links.
3. **Treat the consolidated CSV as an export/projection** during the
   transition, so the dashboard and agent keep working unchanged.
4. **Keep the OpenSearch prototype** for ad-hoc exploration, and as a derived
   read/search index if full-text or fuzzy search becomes a real requirement.
   Do not make it the system of record.

### Proposed next steps

1. **Short Postgres bake-off (1–2 days).** Load the same 35,998-row corpus into
   Postgres, run the same query battery, and time the issue #94 operations.
2. **Design the schema.** Typed columns for stable fields; `JSONB` for drifting
   and modality-specific fields; table per project plus a cross-project view.
3. **Change the write path.** Per-run inserts in one transaction, replacing the
   S3 download/concat/upload.
4. **Build the read adapter.** Keep `load_data()` and `load_rhaiis_data()`
   returning the same DataFrame contract.
5. **Cut over and retire CSV as the system of record**, keeping it as an export.

### What would change this recommendation

- Multi-row corrections turn out **not** to be required → OpenSearch becomes a
  viable primary store.
- Full-text or fuzzy search becomes a **core** requirement → use OpenSearch as
  the primary or as a derived index.
- Data volume grows by orders of magnitude, or multi-tenant search becomes a
  requirement → reconsider OpenSearch.
- Postgres operations become a constraint (ownership, availability, migrations)
  → reconsider.

## Appendix: what was tested

- OpenSearch 3.9.0 and OpenSearch Dashboards 3.9.0, single node in Docker,
  current staging S3 objects.
- RHAIIS: 35,998 rows × 56 columns; llm-d: 454 rows × 53 columns (47 shared).
- Scripts: `prototype/01`–`05`; results in `prototype/*.json`.
- Not tested in this pass: a Postgres implementation, the lab OpenSearch
  cluster, production load, and tenant/access configuration.
