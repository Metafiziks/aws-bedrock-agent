#!/usr/bin/env python3
"""Upload an approved local corpus plus per-document ACLs and wait for a clean sync.

Protected mode only. The corpus must live OUTSIDE this repository (never the public
sample ``docs/``). Every file needs an explicit entry in an ACL manifest; the script
refuses to run unless the bucket is private and the data source is an ACL-enabled source
on that bucket of a managed knowledge base, and it only succeeds when the ingestion job
completes with zero failures.

Manifest (``acl.json`` in the corpus directory, or --manifest):
  {"approved.txt": {"users": ["a@example.com"]},
   "control.txt":  {"users": []}}
Users are email addresses matched exactly against the signed-in user's verified email.
S3 ACLs support users only (no groups). A document without an ACL is counted as a FAILED
document by the sync (observed live), so an entry with no readers is not uploaded at all and
is never searchable. There is no "everyone" option.
Objects under docs/ that are not in the corpus are deleted, so the index follows the manifest.
"""

import argparse
import json
import re
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SUFFIXES = {".txt", ".md", ".html", ".pdf", ".docx"}
EMAIL = re.compile(r"[A-Za-z0-9._%+'-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")
PUBLIC_GRANTEES = ("AllUsers", "AuthenticatedUsers")
DOC_PREFIX = "docs/"
METADATA_SUFFIX = ".metadata.json"


def corpus_files(directory):
    root = Path(directory).resolve(strict=True)
    if not root.is_dir():
        raise ValueError("The corpus must be a directory")
    if root == REPO or REPO in root.parents or root in REPO.parents:
        raise ValueError("Keep the protected corpus outside this repository, never in docs/")
    files = sorted(
        path for path in root.rglob("*")
        if path.is_file() and path.suffix.lower() in SUFFIXES
        and not path.name.endswith(METADATA_SUFFIX)
        and not any(part.startswith((".", "~$")) for part in path.relative_to(root).parts)
    )
    if not files:
        raise ValueError("No supported documents in the corpus")
    if any(path.is_symlink() or not path.resolve().is_relative_to(root) for path in files):
        raise ValueError("The corpus must not contain symlinks")
    return root, files


def load_manifest(path, root, files):
    with Path(path).open(encoding="utf-8") as handle:
        manifest = json.load(handle)
    names = {file.relative_to(root).as_posix() for file in files}
    if not isinstance(manifest, dict) or set(manifest) != names:
        raise ValueError("The ACL manifest must have exactly one entry per corpus file")
    for name, entry in manifest.items():
        if not isinstance(entry, dict) or set(entry) != {"users"}:
            raise ValueError(f"{name}: use exactly a 'users' list (S3 ACLs support users only)")
        users = entry["users"]
        if not isinstance(users, list) or not all(isinstance(v, str) and EMAIL.fullmatch(v) for v in users):
            raise ValueError(f"{name}: 'users' must be a list of email addresses")
        if len({u for u in users}) != len(users):
            raise ValueError(f"{name}: duplicate users")
    return manifest


def metadata_document(name, users):
    return {
        "metadataAttributes": {"title": name.split("/")[-1]},
        "accessControlList": [{"Name": u, "Type": "USER", "Access": "ALLOW"} for u in users],
    }


def bucket_is_private(public_access_block, policy_status, acl_grants):
    """All four public-access blocks on, no public bucket policy, no public ACL grants."""
    blocks = all(public_access_block.get(k) is True for k in (
        "BlockPublicAcls", "IgnorePublicAcls", "BlockPublicPolicy", "RestrictPublicBuckets"))
    public_policy = policy_status.get("IsPublic") is True
    public_acl = any(
        (g.get("Grantee") or {}).get("URI", "").endswith(PUBLIC_GRANTEES) for g in acl_grants
    )
    return blocks and not public_policy and not public_acl


def check_destination(s3, agent, bucket, kb_id, ds_id):
    block = s3.get_public_access_block(Bucket=bucket)["PublicAccessBlockConfiguration"]
    try:
        status = s3.get_bucket_policy_status(Bucket=bucket)["PolicyStatus"]
    except Exception as exc:  # noqa: BLE001 - botocore ClientError; no bucket policy means not public
        if getattr(exc, "response", {}).get("Error", {}).get("Code") != "NoSuchBucketPolicy":
            raise
        status = {"IsPublic": False}
    if not bucket_is_private(block, status, s3.get_bucket_acl(Bucket=bucket).get("Grants", [])):
        raise RuntimeError("The bucket is not private (needs all public access blocks, no public policy or ACL)")
    # Same checks the runtime makes on every request, so ingestion and retrieval cannot disagree.
    sys.path.insert(0, str(REPO / "src" / "protected"))
    from protected_search import acl_enabled_on_bucket, is_protected_kb

    if not is_protected_kb(agent.get_knowledge_base(knowledgeBaseId=kb_id), kb_id):
        raise RuntimeError("The knowledge base is not an active managed knowledge base")
    if not acl_enabled_on_bucket(agent.get_data_source(knowledgeBaseId=kb_id, dataSourceId=ds_id), ds_id, bucket):
        raise RuntimeError("The data source is not an ACL-enabled source on this bucket")


def wait_for_ingestion(agent, kb_id, ds_id, job_id, timeout, sleep=time.sleep, clock=time.monotonic):
    deadline = clock() + timeout
    while True:
        job = agent.get_ingestion_job(knowledgeBaseId=kb_id, dataSourceId=ds_id, ingestionJobId=job_id)["ingestionJob"]
        if job["status"] in ("COMPLETE", "FAILED", "STOPPED"):
            break
        if clock() >= deadline:
            raise TimeoutError("Timed out waiting for the knowledge base sync")
        sleep(10)
    stats = job.get("statistics") or {}
    failed = int(stats.get("numberOfDocumentsFailed", 0))
    reasons = job.get("failureReasons") or []
    if job["status"] != "COMPLETE" or failed != 0 or reasons:
        raise RuntimeError(
            f"Sync did not verify ingestion: status={job['status']}, failed={failed}, failure_reasons={len(reasons)}. "
            "Inspect the ingestion job in the Bedrock console."
        )
    return stats


def sync_corpus(s3, bucket, root, files, manifest):
    """Upload documents + ACL files; delete everything else under docs/. Returns (readable, skipped)."""
    wanted = {}
    for path in files:
        name = path.relative_to(root).as_posix()
        users = manifest[name]["users"]
        if users:  # a document without an ACL would make the sync report a failure
            wanted[f"{DOC_PREFIX}{name}"] = path
            wanted[f"{DOC_PREFIX}{name}{METADATA_SUFFIX}"] = json.dumps(metadata_document(name, users))
    for key, source in wanted.items():
        if isinstance(source, Path):
            s3.upload_file(str(source), bucket, key)
        else:
            s3.put_object(Bucket=bucket, Key=key, Body=source.encode("utf-8"), ContentType="application/json")
    stale = []
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=DOC_PREFIX):
        stale += [o["Key"] for o in page.get("Contents", []) if o["Key"] not in wanted]
    for key in stale:
        s3.delete_object(Bucket=bucket, Key=key)
    readable = sum(1 for f in files if manifest[f.relative_to(root).as_posix()]["users"])
    return readable, len(files) - readable


def ingest(bucket, kb_id, ds_id, directory, manifest_path=None, timeout=1800, region=None, s3=None, agent=None):
    import boto3

    root, files = corpus_files(directory)
    manifest = load_manifest(manifest_path or root / "acl.json", root, files)
    s3 = s3 or boto3.client("s3", region_name=region)
    agent = agent or boto3.client("bedrock-agent", region_name=region)
    check_destination(s3, agent, bucket, kb_id, ds_id)
    readable, skipped = sync_corpus(s3, bucket, root, files, manifest)
    if not readable:
        raise ValueError("No document has any readers; nothing would be searchable")
    job = agent.start_ingestion_job(knowledgeBaseId=kb_id, dataSourceId=ds_id)["ingestionJob"]["ingestionJobId"]
    stats = wait_for_ingestion(agent, kb_id, ds_id, job, timeout)
    print(f"Uploaded {len(files)} files; sync complete with no failures (scanned={stats.get('numberOfDocumentsScanned')}, "
          f"new={stats.get('numberOfNewDocumentsIndexed')}, modified={stats.get('numberOfModifiedDocumentsIndexed')}, "
          f"skipped={stats.get('numberOfDocumentsSkipped')}); {readable} with explicit ACLs, {skipped} without readers "
          "were not uploaded and are not searchable.")
    return stats


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    for field in ("bucket", "knowledge-base-id", "data-source-id", "directory"):
        parser.add_argument(f"--{field}", required=True)
    parser.add_argument("--manifest")
    parser.add_argument("--region")
    parser.add_argument("--timeout", type=int, default=1800, help="Sync wait in seconds")
    args = parser.parse_args()
    try:
        ingest(args.bucket, args.knowledge_base_id, args.data_source_id, args.directory, args.manifest,
               args.timeout, args.region)
    except Exception as exc:  # noqa: BLE001
        sys.exit(f"Protected ingestion failed: {type(exc).__name__}: {exc}")


if __name__ == "__main__":
    main()
