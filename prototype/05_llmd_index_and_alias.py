#!/usr/bin/env python3
"""Add the llm-d index and a read alias across both RHAIIS and llm-d.

Shows the multi-project model from the research doc: one cluster, one index per
project with its own mapping, and an alias for cross-project queries.
"""
from __future__ import annotations

import csv
import hashlib
import importlib.util
import json
from collections import Counter
from pathlib import Path

import pandas as pd
from opensearchpy import OpenSearch, helpers

SCRIPTS = Path(__file__).resolve().parent
DATA = Path("data")
OUT = Path("out")

RHAIIS_INDEX = "psap-rhaiis-benchmark-v1"
LLMD_INDEX = "psap-llmd-benchmark-v1"
ALIAS = "psap-all-benchmarks"

LLMD_INTEGER_FIELDS = {"EP", "replicas", "prefill_pod_count", "decode_pod_count"}
LLMD_TEXT_FIELDS = {"router_config", "notes"}


def load_module(filename: str, name: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ingest01 = load_module("01_create_index_and_ingest.py", "ingest01")
roundtrip02 = load_module("02_roundtrip_csv.py", "roundtrip02")


def mapping_for(columns: list[str]) -> dict:
    properties = {"_row_index": {"type": "long"}}
    for col in columns:
        if col == "runtime_args":
            properties[col] = {
                "type": "text",
                "fields": {"keyword": {"type": "keyword", "ignore_above": 8192}},
            }
        elif col in LLMD_TEXT_FIELDS:
            properties[col] = {"type": "text"}
        elif col in ingest01.KEYWORD_FIELDS:
            properties[col] = {"type": "keyword", "ignore_above": 8192}
        elif col in ingest01.INTEGER_FIELDS or col in LLMD_INTEGER_FIELDS:
            properties[col] = {"type": "long", "coerce": True, "ignore_malformed": True}
        elif col in ingest01.DOUBLE_FIELDS:
            properties[col] = {"type": "double", "coerce": True, "ignore_malformed": True}
        else:
            raise SystemExit(f"unmapped column: {col!r}")
    return {
        "settings": {"index": {"number_of_shards": 1, "number_of_replicas": 0}},
        "mappings": {"dynamic": "strict", "properties": properties},
    }


def main() -> None:
    OUT.mkdir(exist_ok=True)
    client = OpenSearch(hosts=[{"host": "localhost", "port": 9200}], timeout=120)

    source = DATA / "llmd-dashboard_s3.csv"
    with source.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        columns = list(reader.fieldnames or [])
        rows = list(reader)

    # Same index-per-project model as RHAIIS, separate mapping.
    if client.indices.exists(index=LLMD_INDEX):
        client.indices.delete(index=LLMD_INDEX)
    client.indices.create(index=LLMD_INDEX, body=mapping_for(columns))

    seen: Counter[str] = Counter()
    actions = []
    for row_index, row in enumerate(rows):
        doc = {}
        for col in columns:
            value = row.get(col, "")
            numeric = (
                col in ingest01.INTEGER_FIELDS
                or col in ingest01.DOUBLE_FIELDS
                or col in LLMD_INTEGER_FIELDS
            )
            doc[col] = None if numeric and value == "" else value
        doc["_row_index"] = row_index
        digest = hashlib.sha256(
            json.dumps([row.get(c, "") for c in columns], ensure_ascii=False).encode()
        ).hexdigest()
        occurrence = seen[digest]
        seen[digest] += 1
        actions.append(
            {
                "_index": LLMD_INDEX,
                "_id": digest if occurrence == 0 else f"{digest}-{occurrence}",
                "_source": doc,
            }
        )
    helpers.bulk(client, actions, chunk_size=1000, request_timeout=180)
    client.indices.refresh(index=LLMD_INDEX)

    llmd_count = client.count(index=LLMD_INDEX)["count"]
    rhaiis_count = client.count(index=RHAIIS_INDEX)["count"]
    print(f"llmd_indexed={llmd_count} rhaiis={rhaiis_count}")

    # One alias, two concrete indices. Read-only for now.
    client.indices.update_aliases(
        body={
            "actions": [
                {"add": {"index": RHAIIS_INDEX, "alias": ALIAS}},
                {"add": {"index": LLMD_INDEX, "alias": ALIAS}},
            ]
        }
    )
    alias_count = client.count(index=ALIAS)["count"]
    print(f"alias={ALIAS} count={alias_count} expected={rhaiis_count + llmd_count}")

    # Round-trip the llm-d index too.
    llmd_roundtrip = OUT / "llmd_dashboard_roundtrip.csv"
    exported = roundtrip02.export(client, columns, llmd_roundtrip, index=LLMD_INDEX)
    original = pd.read_csv(source, dtype=str, keep_default_na=False)
    roundtrip = pd.read_csv(llmd_roundtrip, dtype=str, keep_default_na=False)
    llmd_exact = original.equals(roundtrip)
    print(f"llmd_exported={exported} columns={len(columns)} exact={llmd_exact}")

    # Cross-project aggregation on a shared keyword field.
    def top_models(index: str) -> list[tuple[str, int]]:
        resp = client.search(
            index=index,
            body={
                "size": 0,
                "aggs": {"models": {"terms": {"field": "model", "size": 5}}},
            },
        )
        return [(b["key"], b["doc_count"]) for b in resp["aggregations"]["models"]["buckets"]]

    both = pd.concat([original, pd.read_csv(DATA / "consolidated_dashboard_s3.csv", dtype=str)])
    baseline = [(k, int(v)) for k, v in both["model"].value_counts().head(5).items()]
    alias_top = top_models(ALIAS)
    print("alias_top_models", alias_top)
    print("baseline_top_models", baseline)

    # A field that only one project has.
    ep_docs = client.count(
        index=ALIAS, body={"query": {"exists": {"field": "EP"}}}
    )["count"]
    print(f"alias_docs_with_EP={ep_docs} (llm-d only)")

    # The alias has no write index: writes through it must be rejected.
    write_rejected = False
    try:
        client.index(index=ALIAS, body={"model": "should-not-land"}, refresh=True)
    except Exception:  # noqa: BLE001
        write_rejected = True
    print(f"write_via_alias_rejected={write_rejected}")

    summary = {
        "rhaiis_index": RHAIIS_INDEX,
        "rhaiis_rows": rhaiis_count,
        "llmd_index": LLMD_INDEX,
        "llmd_rows": llmd_count,
        "alias": ALIAS,
        "alias_rows": alias_count,
        "alias_counts_match": alias_count == rhaiis_count + llmd_count,
        "llmd_roundtrip_exact": bool(llmd_exact),
        "llmd_columns": len(columns),
        "shared_columns": len(set(columns) & set(pd.read_csv(DATA / "consolidated_dashboard_s3.csv", nrows=1).columns)),
        "alias_top_models": alias_top,
        "baseline_top_models": baseline,
        "alias_docs_with_EP": ep_docs,
        "write_via_alias_rejected": write_rejected,
    }
    (OUT / "llmd_alias.json").write_text(json.dumps(summary, indent=2))
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
