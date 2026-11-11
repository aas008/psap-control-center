#!/usr/bin/env python3
"""Create the RHAIIS benchmark index and ingest the current S3 CSV."""
from __future__ import annotations

import csv
import hashlib
import json
from collections import Counter
from pathlib import Path

from opensearchpy import OpenSearch, helpers

INDEX = "psap-rhaiis-benchmark-v1"
CSV_PATH = Path("data/consolidated_dashboard_s3.csv")

KEYWORD_FIELDS = {
    "run", "accelerator", "model", "version", "uuid", "image_tag",
    "guidellm_version", "dataset", "spec_decoding", "prefix_caching",
    "request_type", "mlflow_run_id", "mlflow_experiment_id", "env_vars",
}
INTEGER_FIELDS = {
    "prompt toks", "output toks", "TP", "intended concurrency",
    "successful_requests", "errored_requests", "DP", "turns",
    "prefix_tokens", "prefix_count", "turn_index",
    "guidellm_start_time_ms", "guidellm_end_time_ms",
}
DOUBLE_FIELDS = {
    "measured concurrency", "measured rps", "output_tok/sec", "total_tok/sec",
    "prompt_token_count_mean", "prompt_token_count_p99",
    "output_token_count_mean", "output_token_count_p99",
    "ttft_median", "ttft_p95", "ttft_p1", "ttft_p999", "ttft_mean", "ttft_p99",
    "tpot_median", "tpot_p95", "tpot_p99", "tpot_p999", "tpot_p1",
    "itl_median", "itl_p95", "itl_p999", "itl_p1", "itl_mean", "itl_p99",
    "request_latency_median", "request_latency_min", "request_latency_max",
}


def mapping_for(columns: list[str]) -> dict:
    properties = {"_row_index": {"type": "long"}}
    for col in columns:
        if col in KEYWORD_FIELDS:
            properties[col] = {"type": "keyword", "ignore_above": 8192}
        elif col in INTEGER_FIELDS:
            properties[col] = {
                "type": "long", "coerce": True, "ignore_malformed": True,
            }
        elif col in DOUBLE_FIELDS:
            properties[col] = {
                "type": "double", "coerce": True, "ignore_malformed": True,
            }
        elif col == "runtime_args":
            properties[col] = {
                "type": "text",
                "fields": {"keyword": {"type": "keyword", "ignore_above": 8192}},
            }
        else:
            raise SystemExit(f"unmapped column: {col!r}")
    return {
        "settings": {"index": {"number_of_shards": 1, "number_of_replicas": 0}},
        "mappings": {"dynamic": "strict", "properties": properties},
    }


def main() -> None:
    with CSV_PATH.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        columns = list(reader.fieldnames or [])
        rows = list(reader)

    client = OpenSearch(
        hosts=[{"host": "localhost", "port": 9200}],
        http_compress=True,
        use_ssl=False,
        verify_certs=False,
        timeout=120,
    )
    if client.indices.exists(index=INDEX):
        client.indices.delete(index=INDEX)
    client.indices.create(index=INDEX, body=mapping_for(columns))

    seen: Counter[str] = Counter()
    actions = []
    for row_index, row in enumerate(rows):
        source = {}
        for col in columns:
            value = row.get(col, "")
            if col in INTEGER_FIELDS or col in DOUBLE_FIELDS:
                source[col] = None if value == "" else value
            else:
                source[col] = value
        source["_row_index"] = row_index

        # Deterministic id from the original row, with a suffix for exact duplicates.
        digest = hashlib.sha256(
            json.dumps([row.get(c, "") for c in columns], ensure_ascii=False).encode()
        ).hexdigest()
        occurrence = seen[digest]
        seen[digest] += 1
        doc_id = digest if occurrence == 0 else f"{digest}-{occurrence}"
        actions.append({"_index": INDEX, "_id": doc_id, "_source": source})

    helpers.bulk(client, actions, chunk_size=1000, request_timeout=180)
    client.indices.refresh(index=INDEX)
    count = client.count(index=INDEX)["count"]
    print(f"rows_read={len(rows)} indexed={count} index={INDEX} columns={len(columns)}")
    if count != len(rows):
        raise SystemExit(f"count mismatch: {count} != {len(rows)}")


if __name__ == "__main__":
    main()
