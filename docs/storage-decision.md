# Benchmark storage decision

## 1. Decision

The decision is which system should be the authoritative store for benchmark metadata: the current S3 CSV, OpenSearch, or Postgres. The evaluation compared them on the operations the team actually performs: per-run writes, version renames, run replacement and deletion, exact rollups, and dashboard/agent reads. The recommendation is Postgres as the single system of record, with S3 retained for artifacts and the consolidated CSV generated as an export during the transition. OpenSearch was tested and handles the reads, but it cannot make multi-row corrections atomic, so it should not be the system of record. No second store is proposed.

## 2. Problem statement

Forge treats one consolidated CSV in S3 as the source of truth: each job downloads the whole file, appends its rows, and re-uploads it. Two jobs finishing together overwrite each other's rows, there is no deduplication or validation, and corrections such as renaming a version or replacing a run are manual edits against a 30 MB object. The schema is also diverging across projects — 56 RHAIIS columns, 53 llm-d columns, only 47 shared — which makes hand-editing riskier. The team needs a store where concurrent writes are safe and multi-row corrections are atomic, without forcing the dashboard and agent to change.

## 3. Why not OpenSearch

A local OpenSearch 3.9 instance was built and tested against the current 35,998-row corpus: it ingested the data, round-tripped the CSV with zero mismatches, and matched exact counts, substring filters, and top-N queries. The blocking problem is corrections, because `update_by_query`, `delete_by_query`, and bulk operations are not atomic across documents; a rename or replace that fails halfway leaves a partially corrected run. The prototype also required explicit mappings, rejected an in-place field-type change, needed `shard_size` tuning to make aggregations exact, and hit the 10,000-document pagination cap. Each issue is manageable, but together they make the operations the team cares about harder while adding a new cluster to operate.

## 4. Why Postgres

Postgres provides the missing guarantees: a rename, replace, or delete across a run's rows is one transaction that either commits or rolls back, and `GROUP BY` is exact without tuning. It supports the current query shapes with SQL and `ILIKE`, handles schema drift with typed columns plus `JSONB`, and represents multiple projects as a table per project with a union view. It is already deployed for agent state, so the team is not adding a new system to operate. The proposed path is to write each run's rows to Postgres in one transaction, generate the consolidated CSV from it so the dashboard and agent remain unchanged, and keep S3 and MLflow for logs, traces, and model artifacts.
