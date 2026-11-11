#!/usr/bin/env python3
"""Simulate Forge onboarding one per-run dashboard.csv into OpenSearch.

The real Forge flow appends a run's ``dashboard.csv`` to the consolidated S3 CSV
by downloading the whole file, concatenating, and re-uploading. This script does
the OpenSearch equivalent: bulk-index those rows with stable document IDs, then
regenerate the consolidated CSV from the index and prove the rows are identical.
"""
from __future__ import annotations

import csv
import hashlib
import importlib.util
import json
import time
from collections import Counter
from pathlib import Path

import pandas as pd
from opensearchpy import OpenSearch, helpers

SCRIPTS = Path(__file__).resolve().parent
CONSOLIDATED = Path("data/consolidated_dashboard_s3.csv")
OUT = Path("out")
INDEX = "psap-rhaiis-benchmark-v1"


def load_module(filename: str, name: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ingest01 = load_module("01_create_index_and_ingest.py", "ingest01")
roundtrip02 = load_module("02_roundtrip_csv.py", "roundtrip02")


def row_to_source(row: dict[str, str], columns: list[str], row_index: int) -> dict:
    source = {}
    for col in columns:
        value = row.get(col, "")
        if col in ingest01.INTEGER_FIELDS or col in ingest01.DOUBLE_FIELDS:
            source[col] = None if value == "" else value
        else:
            source[col] = value
    source["_row_index"] = row_index
    return source


def doc_id_for(row: dict[str, str], columns: list[str], seen: Counter[str]) -> str:
    digest = hashlib.sha256(
        json.dumps([row.get(c, "") for c in columns], ensure_ascii=False).encode()
    ).hexdigest()
    occurrence = seen[digest]
    seen[digest] += 1
    return digest if occurrence == 0 else f"{digest}-{occurrence}"


def actions_for(rows: list[dict[str, str]], columns: list[str], start_index: int) -> list[dict]:
    seen: Counter[str] = Counter()
    actions = []
    for offset, row in enumerate(rows):
        actions.append(
            {
                "_index": INDEX,
                "_id": doc_id_for(row, columns, seen),
                "_source": row_to_source(row, columns, start_index + offset),
            }
        )
    return actions


def main() -> None:
    OUT.mkdir(exist_ok=True)
    client = OpenSearch(hosts=[{"host": "localhost", "port": 9200}], timeout=120)

    with CONSOLIDATED.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        columns = list(reader.fieldnames or [])
        rows = list(reader)

    # Pick a real multi-row run to use as the "per-run dashboard.csv".
    by_uuid: dict[str, list[dict[str, str]]] = {}
    for row in rows:
        if row["uuid"]:
            by_uuid.setdefault(row["uuid"], []).append(row)
    uuid = sorted(
        (u for u, rs in by_uuid.items() if 4 <= len(rs) <= 6), key=lambda u: len(by_uuid[u])
    )[0]
    run_rows = by_uuid[uuid]
    run_csv = OUT / f"run_dashboard_{uuid[:8]}.csv"
    with run_csv.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=columns, lineterminator="\n")
        writer.writeheader()
        writer.writerows(run_rows)
    print(f"per_run_csv={run_csv} rows={len(run_rows)} uuid={uuid}")

    # Simulate a brand-new run: make sure its rows are not in the store yet.
    start = time.perf_counter()
    deleted = client.delete_by_query(
        index=INDEX,
        body={"query": {"term": {"uuid": uuid}}},
        refresh=True,
        conflicts="proceed",
    )
    delete_ms = round((time.perf_counter() - start) * 1000, 2)
    total = client.count(index=INDEX)["count"]
    print(f"deleted={deleted['deleted']} remaining={total} delete_ms={delete_ms}")

    before_csv = OUT / "before_onboard.csv"
    exported_before = roundtrip02.export(client, columns, before_csv)
    print(f"exported_before={exported_before} (expected {len(rows) - len(run_rows)})")

    # Onboard the per-run CSV. New rows sort after everything already stored.
    max_row_index = client.search(
        index=INDEX,
        body={"size": 1, "sort": [{"_row_index": "desc"}], "_source": ["_row_index"]},
    )["hits"]["hits"][0]["_source"]["_row_index"]
    actions = actions_for(run_rows, columns, max_row_index + 1)

    start = time.perf_counter()
    ok, errors = helpers.bulk(client, actions, chunk_size=1000, request_timeout=180)
    client.indices.refresh(index=INDEX)
    bulk_ms = round((time.perf_counter() - start) * 1000, 2)
    after_bulk = client.count(index=INDEX)["count"]
    print(f"bulk_indexed={ok} errors={len(errors)} total={after_bulk} bulk_ms={bulk_ms}")

    # Re-deliver the same per-run CSV twice: count must not grow.
    for attempt in range(2):
        helpers.bulk(client, actions, chunk_size=1000, request_timeout=180)
        client.indices.refresh(index=INDEX)
        print(f"redelivery_{attempt + 1}_total={client.count(index=INDEX)['count']}")

    # Regenerate the consolidated CSV from the index.
    after_csv = OUT / "after_onboard.csv"
    start = time.perf_counter()
    exported_after = roundtrip02.export(client, columns, after_csv)
    export_ms = round((time.perf_counter() - start) * 1000, 2)
    print(f"exported_after={exported_after} export_ms={export_ms}")

    # Compare as a multiset: incremental rows sort last, so order may differ.
    source = pd.read_csv(CONSOLIDATED, dtype=str, keep_default_na=False)
    projected = pd.read_csv(after_csv, dtype=str, keep_default_na=False)
    print("shape", source.shape, projected.shape)
    same_set = source.sort_values(list(columns)).reset_index(drop=True).equals(
        projected.sort_values(list(columns)).reset_index(drop=True)
    )
    print("same_rows_ignoring_order", same_set)

    # The new run specifically, as it appears in the regenerated CSV.
    projected_run = projected[projected["uuid"] == uuid].reset_index(drop=True)
    expected_run = pd.DataFrame(run_rows, dtype=str)
    run_rows_match = expected_run.equals(projected_run)
    print("run_rows_in_projection", len(projected_run), "run_rows_match", run_rows_match)

    summary = {
        "uuid": uuid,
        "per_run_csv": run_csv.name,
        "per_run_rows": len(run_rows),
        "deleted_for_simulation": deleted["deleted"],
        "bulk_ms": bulk_ms,
        "export_ms": export_ms,
        "total_after": client.count(index=INDEX)["count"],
        "same_rows_ignoring_order": bool(same_set),
        "run_rows_match": bool(run_rows_match),
    }
    (OUT / "incremental_ingest.json").write_text(json.dumps(summary, indent=2))
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
