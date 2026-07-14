#!/usr/bin/env python3
"""Pre-create the OpenSearch Serverless k-NN index required by Bedrock Knowledge Base.

Bedrock does not auto-create the index — it must exist with the correct
knn_vector field mapping before the Knowledge Base resource is created.

Usage:
  COLLECTION_ENDPOINT=https://... AWS_REGION=us-east-1 python3 scripts/create_os_index.py
"""
import json
import os
from urllib.parse import urlparse

import boto3
from opensearchpy import OpenSearch, RequestsHttpConnection, AWSV4SignerAuth

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
    host = urlparse(endpoint).netloc

    session = boto3.Session()
    credentials = session.get_credentials()
    auth = AWSV4SignerAuth(credentials, region, "aoss")

    client = OpenSearch(
        hosts=[{"host": host, "port": 443}],
        http_auth=auth,
        use_ssl=True,
        verify_certs=True,
        connection_class=RequestsHttpConnection,
    )

    try:
        resp = client.indices.create(index=INDEX_NAME, body=INDEX_MAPPING)
        print(f"  ✓ Index created: {resp}")
    except Exception as e:
        err_str = str(e)
        if "resource_already_exists_exception" in err_str:
            print("  ✓ Index already exists, skipping")
        else:
            raise SystemExit(f"  ✗ Index creation failed: {e}")


if __name__ == "__main__":
    main()
