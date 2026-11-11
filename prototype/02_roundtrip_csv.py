#!/usr/bin/env python3
"""Export the OpenSearch index back to the original 56-column CSV and compare."""
from __future__ import annotations

import csv
from pathlib import Path

import pandas as pd
from opensearchpy import OpenSearch

INDEX = "psap-rhaiis-benchmark-v1"
SOURCE = Path("data/consolidated_dashboard_s3.csv")
OUTPUT = Path("out/consolidated_dashboard_roundtrip.csv")
BATCH = 1000


def export(
    client: OpenSearch, columns: list[str], output: Path = OUTPUT, index: str = INDEX
) -> int:
    exported = 0
    search_after = None
    with output.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=columns, lineterminator="\n")
        writer.writeheader()
        while True:
            body = {
                "size": BATCH,
                "query": {"match_all": {}},
                "sort": [{"_row_index": "asc"}],
                "_source": columns,
            }
            if search_after is not None:
                body["search_after"] = search_after
            resp = client.search(index=index, body=body)
            hits = resp["hits"]["hits"]
            if not hits:
                break
            for hit in hits:
                src = hit["_source"]
                row = {}
                for col in columns:
                    value = src.get(col)
                    row[col] = "" if value is None else value
                writer.writerow(row)
                exported += 1
            search_after = hits[-1]["sort"]
    return exported


def main() -> None:
    client = OpenSearch(hosts=[{"host": "localhost", "port": 9200}], timeout=120)
    with SOURCE.open(newline="", encoding="utf-8") as f:
        columns = list(csv.DictReader(f).fieldnames or [])
    exported = export(client, columns)
    print(f"exported={exported}")

    source = pd.read_csv(SOURCE, dtype=str, keep_default_na=False)
    roundtrip = pd.read_csv(OUTPUT, dtype=str, keep_default_na=False)
    print("source_shape", source.shape, "roundtrip_shape", roundtrip.shape)
    if list(source.columns) != list(roundtrip.columns):
        print("COLUMN MISMATCH")
        print("source-only", [c for c in source.columns if c not in roundtrip.columns])
        print("roundtrip-only", [c for c in roundtrip.columns if c not in source.columns])
        raise SystemExit(1)
    if source.shape != roundtrip.shape:
        raise SystemExit(f"shape mismatch: {source.shape} != {roundtrip.shape}")

    mismatches = (source != roundtrip)
    total = int(mismatches.to_numpy().sum())
    print("cell_mismatches", total)
    if total:
        rows = mismatches.any(axis=1)
        print("rows_with_mismatch", int(rows.sum()))
        first = int(rows[rows].index[0])
        print("first_mismatch_row_index", first)
        for col in source.columns:
            if source.loc[first, col] != roundtrip.loc[first, col]:
                print("  col", col, "source=", repr(source.loc[first, col]), "roundtrip=", repr(roundtrip.loc[first, col]))
        raise SystemExit(2)
    print("ROUNDTRIP_EXACT_MATCH")


if __name__ == "__main__":
    main()
