#!/usr/bin/env python3
"""Compare the real dashboard/agent query shapes against pandas baselines."""
from __future__ import annotations

import json
import time
from pathlib import Path

import pandas as pd
from opensearchpy import OpenSearch

INDEX = "psap-rhaiis-benchmark-v1"
CSV = Path("data/consolidated_dashboard_s3.csv")
OUT = Path("out/query_battery.json")


def timed(fn):
    start = time.perf_counter()
    value = fn()
    return round((time.perf_counter() - start) * 1000, 2), value


def main() -> None:
    client = OpenSearch(hosts=[{"host": "localhost", "port": 9200}], timeout=120)
    df = pd.read_csv(CSV, dtype=str, keep_default_na=False)
    results = {}

    # 1. Exact version match (dashboard filter).
    version = "RHAIIS-3.6-fast2"
    if version not in set(df["version"]):
        version = df["version"].value_counts().index[0]
    baseline = int((df["version"] == version).sum())
    ms, response = timed(lambda: client.count(index=INDEX, body={"query": {"term": {"version": version}}}))
    results["exact_version"] = {"version": version, "pandas": baseline, "opensearch": response["count"], "ms": ms}

    # 2. Case-insensitive substring on model (agent query shape).
    needle = "gpt-oss"
    baseline = int(df["model"].str.contains(needle, case=False, na=False).sum())
    ms, response = timed(lambda: client.count(index=INDEX, body={"query": {"wildcard": {"model": {"value": f"*{needle}*", "case_insensitive": True}}}}))
    results["substring_model"] = {"needle": needle, "pandas": baseline, "opensearch": response["count"], "ms": ms}

    # 3. Top-N by output throughput.
    df["_out"] = pd.to_numeric(df["output_tok/sec"], errors="coerce")
    baseline = [round(float(v), 3) for v in df["_out"].nlargest(10).tolist()]
    def top_n():
        return client.search(index=INDEX, body={
            "size": 10,
            "query": {"exists": {"field": "output_tok/sec"}},
            "sort": [{"output_tok/sec": {"order": "desc"}}],
            "_source": ["output_tok/sec"],
        })
    ms, response = timed(top_n)
    got = [round(float(h["_source"]["output_tok/sec"]), 3) for h in response["hits"]["hits"]]
    results["top_n_output_tok"] = {"pandas": baseline, "opensearch": got, "match": baseline == got, "ms": ms}

    # 4. Group by version, average throughput (dashboard aggregation shape).
    baseline = df.groupby("version")["_out"].mean().nlargest(10)
    def agg():
        return client.search(index=INDEX, body={
            "size": 0,
            "aggs": {"by_version": {"terms": {"field": "version", "size": 10, "shard_size": 1000, "order": {"avg_out": "desc"}}, "aggs": {"avg_out": {"avg": {"field": "output_tok/sec"}}}}},
        })
    ms, response = timed(agg)
    got = [(b["key"], round(float(b["avg_out"]["value"]), 3)) for b in response["aggregations"]["by_version"]["buckets"]]
    expected = [(k, round(float(v), 3)) for k, v in baseline.items()]
    results["agg_version_avg"] = {"pandas": expected, "opensearch": got, "match": expected == got, "ms": ms}

    # 5. Rename version values for one UUID (the user's most common task).
    counts = df[df["uuid"] != ""].groupby("uuid").size()
    uuid = counts[(counts >= 2) & (counts <= 5)].index[0]
    original_version = df.loc[df["uuid"] == uuid, "version"].iloc[0]
    new_version = "PROTOTYPE-RENAMED"
    def rename():
        return client.update_by_query(index=INDEX, body={
            "query": {"term": {"uuid": uuid}},
            "script": {"source": "ctx._source.version = params.version", "params": {"version": new_version}},
        }, refresh=True, conflicts="proceed")
    ms, response = timed(rename)
    after = client.count(index=INDEX, body={"query": {"bool": {"filter": [{"term": {"uuid": uuid}}, {"term": {"version": new_version}}]}}})["count"]
    before = int((df["uuid"] == uuid).sum())
    results["rename_version"] = {"uuid": uuid, "original_version": original_version, "new_version": new_version, "rows": before, "updated": response["updated"], "verified": after, "ok": after == before, "ms": ms}
    # Revert so the index still matches the CSV.
    client.update_by_query(index=INDEX, body={
        "query": {"term": {"uuid": uuid}},
        "script": {"source": "ctx._source.version = params.version", "params": {"version": original_version}},
    }, refresh=True, conflicts="proceed")

    # 6. Deep pagination limit.
    try:
        client.search(index=INDEX, body={"from": 10000, "size": 1})
        results["deep_pagination"] = {"rejected": False}
    except Exception as exc:  # noqa: BLE001
        results["deep_pagination"] = {"rejected": True, "error": str(exc)[:300]}

    # 7. Add a new field to the mapping, then try an in-place type change.
    try:
        client.indices.put_mapping(index=INDEX, body={"properties": {"prototype_new_field": {"type": "double"}}})
        results["add_field"] = {"ok": True}
    except Exception as exc:  # noqa: BLE001
        results["add_field"] = {"ok": False, "error": str(exc)[:300]}
    try:
        client.indices.put_mapping(index=INDEX, body={"properties": {"version": {"type": "text"}}})
        results["change_field_type"] = {"rejected": False}
    except Exception as exc:  # noqa: BLE001
        results["change_field_type"] = {"rejected": True, "error": str(exc)[:300]}

    # 8. Idempotent upsert: re-index the same document twice; count must not grow.
    sample = client.search(index=INDEX, body={"size": 1})["hits"]["hits"][0]
    doc_id = sample["_id"]
    before_count = client.count(index=INDEX)["count"]
    client.index(index=INDEX, id=doc_id, body=sample["_source"], refresh=True)
    client.index(index=INDEX, id=doc_id, body=sample["_source"], refresh=True)
    after_count = client.count(index=INDEX)["count"]
    results["upsert_idempotency"] = {"doc_id": doc_id, "before": before_count, "after": after_count, "ok": before_count == after_count}

    OUT.write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
