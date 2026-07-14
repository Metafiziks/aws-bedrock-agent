#!/usr/bin/env python3
"""Pre-create the OpenSearch Serverless k-NN index required by Bedrock Knowledge Base.

Bedrock does not auto-create the index — it must exist with the correct
knn_vector field mapping before the Knowledge Base resource is created.

Usage:
  COLLECTION_ENDPOINT=https://... AWS_REGION=us-east-1 python3 scripts/create_os_index.py
"""
import http.client
import json
import os
import ssl
from urllib.parse import urlparse

try:
    import boto3
    from botocore.auth import SigV4Auth
    from botocore.awsrequest import AWSRequest
except ImportError:
    raise SystemExit(
        "boto3 not found. The provision.sh script should have set up a venv.\n"
        "Run: python3 -m venv /tmp/aoss-venv && /tmp/aoss-venv/bin/pip install boto3 -q"
    )

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
    endpoint = os.environ["COLLECTION_ENDPOINT"]
    region = os.environ.get("AWS_REGION", os.environ.get("AWS_DEFAULT_REGION", "us-east-1"))

    session = boto3.Session()
    creds = session.get_credentials().get_frozen_credentials()

    url = f"{endpoint}/{INDEX_NAME}"
    body = json.dumps(INDEX_MAPPING).encode("utf-8")

    req = AWSRequest(method="PUT", url=url, data=body, headers={"Content-Type": "application/json"})
    SigV4Auth(creds, "aoss", region).add_auth(req)

    parsed = urlparse(url)
    conn = http.client.HTTPSConnection(parsed.netloc, context=ssl.create_default_context())
    conn.request("PUT", f"/{INDEX_NAME}", body=body, headers=dict(req.headers))
    resp = conn.getresponse()
    data = resp.read().decode()

    print(f"  HTTP {resp.status}: {data}")

    if resp.status in (200, 201):
        print("  ✓ Index created")
        return

    try:
        err_type = json.loads(data).get("error", {}).get("type", "")
        if err_type == "resource_already_exists_exception":
            print("  ✓ Index already exists, skipping")
            return
    except (json.JSONDecodeError, AttributeError):
        pass

    raise SystemExit(f"  ✗ Index creation failed: {data}")


if __name__ == "__main__":
    main()
