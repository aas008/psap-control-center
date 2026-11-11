# CSV → OpenSearch local prototype

This is the reproducible local prototype for the 2026-11-04 decision to test an
OpenSearch-backed store with a CSV-compatible read path. It uses the current
staging S3 object, not the older local snapshot.

## What it proves

- OpenSearch 3.9.0 can ingest the full RHAIIS CSV (35,998 rows × 56 columns).
- The index can be exported back to a byte-faithful CSV with 0 cell mismatches.
- The existing Streamlit dashboard renders the exported CSV identically to the
  original (same body text hash, same KPI values).
- The agent's `load_rhaiis_data()` reads the exported CSV unchanged (35,998 × 57,
  adding its derived `efficiency_ratio`).
- Exact filters, case-insensitive substring, top-N, version aggregation, version
  rename, idempotent upsert, add-field, and type-change behavior were measured.

## Setup

```bash
docker run -d --name psap-opensearch \
  -p 9200:9200 -p 9600:9600 \
  -e "discovery.type=single-node" \
  -e "DISABLE_SECURITY_PLUGIN=true" \
  -e "DISABLE_INSTALL_DEMO_CONFIG=true" \
  -e "OPENSEARCH_JAVA_OPTS=-Xms1g -Xmx1g" \
  opensearchproject/opensearch:3.9.0

mkdir -p data out
aws s3 cp s3://psap-dashboard-data/staging/rhaiis-dashboard/consolidated_dashboard.csv \
  data/consolidated_dashboard_s3.csv

python3 -m venv venv
./venv/bin/pip install opensearch-py pandas python-dotenv pydantic-settings structlog
```

## Run

```bash
./venv/bin/python 01_create_index_and_ingest.py
./venv/bin/python 02_roundtrip_csv.py
./venv/bin/python 03_query_battery.py
```

## Results

- 35,998 documents indexed in ~5 s.
- CSV round-trip: exact (0 cell mismatches), same 56 columns and row order.
- Exact version count: 1,829/1,829.
- Case-insensitive model substring: 3,797/3,797.
- Top-10 throughput: exact.
- Top-10 average throughput by version: exact **after**
  `shard_size: 1000`; the default `shard_size` returned a partial bucket
  (`vLLM-0.22.0` 5 rows instead of 62).
- Rename version by UUID: 5 rows updated, 5 verified.
- Re-index same `_id` twice: count stays 35,998.
- Add field: works. Change `version` keyword → text: rejected; reindex needed.
- `from: 10000` pagination: rejected by `index.max_result_window`.

## Persistence

The local OpenSearch container now uses a named volume:

```bash
docker volume create psap-opensearch-data
# mount it at /usr/share/opensearch/data
```

Saved objects (the `psap-rhaiis-benchmark-v1` index pattern and the **RHAIIS
table** saved search) live in OpenSearch’s `.kibana_1` index, so they survive a
container restart with the volume. Verified after `docker restart
psap-opensearch`: 35,998 documents and the saved search remained.

In the lab, persistence depends on the OpenSearch cluster’s PVCs, not the
Dashboards container. Save shared searches in the **Global** tenant (or a shared
tenant), not a private tenant, so other users can see them. A volume is not a
backup: use snapshots or saved-object export for that.

## Caveats

- The Docker index is ephemeral. Add a volume if you want it to survive.
- `terms` aggregations ordered by a sub-metric are approximate with the default
  `shard_size`; set it to at least the number of distinct terms.
- Full exports need `search_after`/PIT because of the 10,000-document window.
- The dashboard takes several seconds to load a 30 MB CSV and is blank until then.
- This prototype ingests the consolidated CSV once. It does **not** wire Forge's
  incremental per-run ingestion; that is the next step.
