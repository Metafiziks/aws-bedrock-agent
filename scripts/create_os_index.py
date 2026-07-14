#!/usr/bin/env python3
"""Pre-create the OpenSearch Serverless k-NN index required by Bedrock Knowledge Base.

Bedrock does not auto-create the index — it must exist with the correct
knn_vector field mapping before the Knowledge Base resource is created.

Usage:
  COLLECTION_ENDPOINT=https://... AWS_REGION=us-east-1 python3 scripts/create_os_index.py
"""
import json
import os
import sys

import boto3
import requests
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.credentials import Credentials

INDEX_NAME = "bedrock-kb-index"
INDEX_MAPPING = {
    "settings": {"index": {"knn": True}},
    "mappings": {
        "properties": {
            "bedrock-knowledge-base-default-vector": {
                "type": "knn_vector",
                "dimension": 1536,
                "method": {"name": "hnsw", "engine": "faiss", "space_type": "l2"},
            },
            "AMAZON_BEDROCK_TEXT_CHUNK": {"type": "text"},
            "AMAZON_BEDROCK_METADATA": {"type": "text", "index": False},
        }
    },
}


def main():
    endpoint = os.environ["COLLECTION_ENDPOINT"].rstrip("/")
    region = os.environ.get("AWS_REGION", os.environ.get("AWS_DEFAULT_REGION", "us-east-1"))

    session = boto3.Session()
    frozen = session.get_credentials().get_frozen_credentials()

    url = f"{endpoint}/{INDEX_NAME}"
    body = json.dumps(INDEX_MAPPING).encode("utf-8")

    # Build and sign the request with botocore SigV4Auth
    aws_req = AWSRequest(
        method="PUT",
        url=url,
        data=body,
        headers={"Content-Type": "application/json"},
    )
    creds = Credentials(frozen.access_key, frozen.secret_key, frozen.token)
    SigV4Auth(creds, "aoss", region).add_auth(aws_req)

    # Send via requests (avoids http.client Host-header conflicts)
    resp = requests.put(url, data=body, headers=dict(aws_req.headers))
    data = resp.text
    print(f"  HTTP {resp.status_code}: {data}")

    if resp.status_code in (200, 201):
        print("  ✓ Index created")
        return

    try:
        err_type = resp.json().get("error", {}).get("type", "")
        if err_type == "resource_already_exists_exception":
            print("  ✓ Index already exists, skipping")
            return
    except Exception:
        pass

    raise SystemExit(f"  ✗ Index creation failed: {data}")


if __name__ == "__main__":
    main()
