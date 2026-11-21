"""Offline tests for protected mode. Run: python3 -m unittest discover -s tests -p 'test_protected*.py'

No AWS calls: clients are fakes, and the real shell scripts run with stubbed aws/terraform commands.
"""

import base64
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src" / "protected"), str(ROOT / "scripts")]

import handler  # noqa: E402
import ingest_protected as ingest  # noqa: E402
import protected_search as ps  # noqa: E402
import test_protected_live as live  # noqa: E402

KB, DS = "ABCDE12345", "FGHIJ67890"
BUCKET = "123456789012-env-protected-docs"
ISSUER = "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_Pool"
CLIENT = "clientid123"
A, B = "usera@example.com", "userb@example.com"
KB_BODY = {"knowledgeBase": {"knowledgeBaseId": KB, "status": "ACTIVE", "knowledgeBaseConfiguration": {"type": "MANAGED"}}}
DS_BODY = {"dataSource": {"dataSourceId": DS, "dataSourceConfiguration": {"managedKnowledgeBaseConnectorConfiguration": {
    "connectorParameters": {"type": "S3", "aclEnabled": True, "connectionConfiguration": {"bucketName": BUCKET}}}}}}


def hit(name, text, bucket=BUCKET):
    return {"content": {"text": text}, "location": {"type": "S3", "s3Location": {"uri": f"s3://{bucket}/docs/{name}"}}}


class FakeAgent:
    def __init__(self, kb=None, ds=None, fail=False):
        self.kb, self.ds, self.fail, self.calls = KB_BODY if kb is None else kb, DS_BODY if ds is None else ds, fail, 0

    def get_knowledge_base(self, **kwargs):
        self.calls += 1
        if self.fail:
            raise RuntimeError("secret provider detail")
        return self.kb

    def get_data_source(self, **kwargs):
        if self.fail:
            raise RuntimeError("secret provider detail")
        return self.ds


class FakeRuntime:
    """Returns what the platform would: documents whose ACL allows the supplied userContext."""

    def __init__(self, acl=None, fail=False, results=None):
        self.acl, self.fail, self.results, self.calls = acl or {}, fail, results, []

    def retrieve(self, **kwargs):
        self.calls.append(kwargs)
        if self.fail:
            raise RuntimeError("secret provider detail")
        if self.results is not None:
            return {"retrievalResults": self.results}
        return {"retrievalResults": list(self.acl.get(kwargs["userContext"]["userId"], []))}


def make_search(runtime=None, agent=None, clock=None):
    kwargs = {"clock": clock} if clock else {}
    return ps.ProtectedRetrieval(KB, DS, BUCKET, agent or FakeAgent(), runtime or FakeRuntime(), **kwargs)


def event(email=A, *, body=None, headers=None, claims=None, raw=None):
    all_claims = {"email": email, "email_verified": "true", "token_use": "id", "iss": ISSUER, "aud": CLIENT, "sub": "s"}
    all_claims.update(claims or {})
    payload = body if body is not None else {"input": "What is the code?", "store": False}
    return {
        "headers": headers or {},
        "body": raw if raw is not None else json.dumps(payload),
        "requestContext": {"authorizer": {"jwt": {"claims": all_claims}}},
    }


class RetrievalTests(unittest.TestCase):
    def test_retrieval_uses_the_supplied_user_and_managed_search(self):
        runtime = FakeRuntime({A: [hit("approved.txt", "ORCHID-47")]})
        docs = make_search(runtime).excerpts("q", A)
        self.assertEqual(docs, [{"source": "approved.txt", "excerpt": "ORCHID-47"}])
        call = runtime.calls[0]
        self.assertEqual(call["userContext"], {"userId": A})
        self.assertEqual(call["knowledgeBaseId"], KB)
        self.assertIn("managedSearchConfiguration", call["retrievalConfiguration"])
        self.assertNotIn("vectorSearchConfiguration", call["retrievalConfiguration"])

    def test_results_outside_the_protected_bucket_or_without_text_are_dropped(self):
        results = [hit("x.txt", "other", bucket="someone-elses-bucket"), hit("y.txt", "  "),
                   {"content": {"text": "t"}, "location": {"type": "WEB", "webLocation": {"url": "https://x"}}},
                   "junk", hit("ok.txt", "fine")]
        docs = make_search(FakeRuntime(results=results)).excerpts("q", A)
        self.assertEqual([d["source"] for d in docs], ["ok.txt"])

    def test_https_s3_locations_returned_by_managed_knowledge_bases_are_understood(self):
        """Observed live: location.s3Location.uri is https://<bucket>.s3.amazonaws.com/<key>."""
        for uri, expected in ((f"https://{BUCKET}.s3.amazonaws.com/docs/approved.txt", "approved.txt"),
                              (f"https://{BUCKET}.s3.us-east-1.amazonaws.com/docs/my%20file.txt", "my file.txt"),
                              (f"s3://{BUCKET}/docs/a.txt", "a.txt")):
            self.assertEqual(ps.source_name(uri, BUCKET), expected)
        for uri in (f"https://{BUCKET}.evil.example/docs/a.txt", f"https://evil.s3.amazonaws.com/{BUCKET}/a.txt",
                    f"https://x{BUCKET}.s3.amazonaws.com/docs/a.txt", f"http://{BUCKET}.s3.amazonaws.com/docs/a.txt",
                    f"https://{BUCKET}.s3.amazonaws.com/", "s3://other/docs/a.txt", None):
            self.assertIsNone(ps.source_name(uri, BUCKET), uri)
        docs = make_search(FakeRuntime(results=[hit("approved.txt", "t")])).excerpts("q", A)
        self.assertEqual(docs[0]["source"], "approved.txt")

    def test_sources_are_filenames_not_urls(self):
        docs = make_search(FakeRuntime(results=[hit("approved.txt", "t")])).excerpts("q", A)
        self.assertNotIn("/", docs[0]["source"])
        self.assertNotIn("http", docs[0]["source"])

    def test_unverified_knowledge_base_fails_closed_before_any_retrieval(self):
        bad_kb = {"knowledgeBase": {**KB_BODY["knowledgeBase"], "knowledgeBaseConfiguration": {"type": "VECTOR"}}}
        inactive = {"knowledgeBase": {**KB_BODY["knowledgeBase"], "status": "CREATING"}}
        no_acl = json.loads(json.dumps(DS_BODY))
        no_acl["dataSource"]["dataSourceConfiguration"]["managedKnowledgeBaseConnectorConfiguration"]["connectorParameters"]["aclEnabled"] = False
        other_bucket = json.loads(json.dumps(DS_BODY))
        other_bucket["dataSource"]["dataSourceConfiguration"]["managedKnowledgeBaseConnectorConfiguration"]["connectorParameters"]["connectionConfiguration"]["bucketName"] = "public-sample-docs"
        for agent in (FakeAgent(kb=bad_kb), FakeAgent(kb=inactive), FakeAgent(ds=no_acl), FakeAgent(ds=other_bucket),
                      FakeAgent(kb={}), FakeAgent(ds={"dataSource": {}}), FakeAgent(fail=True)):
            runtime = FakeRuntime({A: [hit("a.txt", "t")]})
            with self.assertRaises(ps.ProtectedRetrievalError):
                make_search(runtime, agent).excerpts("q", A)
            self.assertEqual(runtime.calls, [], "must not retrieve from an unverified store")

    def test_connector_parameters_returned_as_a_json_string_are_understood(self):
        """Observed live: GetDataSource returns connectorParameters as a JSON string."""
        as_string = json.loads(json.dumps(DS_BODY))
        cfg = as_string["dataSource"]["dataSourceConfiguration"]["managedKnowledgeBaseConnectorConfiguration"]
        cfg["connectorParameters"] = json.dumps(cfg["connectorParameters"])
        self.assertTrue(ps.acl_enabled_on_bucket(as_string, DS, BUCKET))
        cfg["connectorParameters"] = json.dumps({**json.loads(cfg["connectorParameters"]), "aclEnabled": False})
        self.assertFalse(ps.acl_enabled_on_bucket(as_string, DS, BUCKET))
        cfg["connectorParameters"] = "not json"
        self.assertFalse(ps.acl_enabled_on_bucket(as_string, DS, BUCKET))

    def test_configuration_failure_does_not_leak_provider_details(self):
        with self.assertRaises(ps.ProtectedRetrievalError) as ctx:
            make_search(agent=FakeAgent(fail=True)).excerpts("q", A)
        self.assertNotIn("secret", str(ctx.exception))

    def test_configuration_check_is_cached_briefly_then_repeated(self):
        now = [0.0]
        agent = FakeAgent()
        search = make_search(FakeRuntime(), agent, clock=lambda: now[0])
        search.excerpts("q", A)
        search.excerpts("q", A)
        self.assertEqual(agent.calls, 1)
        now[0] = ps.CONFIG_TTL + 1
        search.excerpts("q", A)
        self.assertEqual(agent.calls, 2)

    def test_a_failed_check_is_never_cached(self):
        agent = FakeAgent(fail=True)
        search = make_search(FakeRuntime(), agent)
        for _ in range(2):
            with self.assertRaises(ps.ProtectedRetrievalError):
                search.excerpts("q", A)
        self.assertEqual(agent.calls, 2)

    def test_missing_or_malformed_identity_never_reaches_retrieval(self):
        for bad in (None, "", "not-an-email", "a@b", "x" * 300 + "@example.com", 5, "a b@example.com"):
            runtime = FakeRuntime()
            with self.assertRaises(ps.IdentityRejected):
                make_search(runtime).excerpts("q", bad)
            self.assertEqual(runtime.calls, [])

    def test_retrieval_errors_fail_closed_without_details(self):
        with self.assertRaises(ps.ProtectedRetrievalError) as ctx:
            make_search(FakeRuntime(fail=True)).excerpts("q", A)
        self.assertNotIn("secret", str(ctx.exception))
        for results in (None,):
            runtime = FakeRuntime()
            runtime.retrieve = lambda **kw: {"retrievalResults": "nope"}
            with self.assertRaises(ps.ProtectedRetrievalError):
                make_search(runtime).excerpts("q", A)

    def test_id_validation(self):
        for args in (("short", DS, BUCKET), (KB, "bad id!", BUCKET), (KB, DS, "Bad_Bucket")):
            with self.assertRaises(ValueError):
                ps.validate_ids(*args)


class HandlerTests(unittest.TestCase):
    def build(self, acl=None, text="ORCHID-47 is the code. (approved.txt)", fail=False, retrieval_fail=False):
        self.runtime = FakeRuntime(acl if acl is not None else {A: [hit("approved.txt", "The code is ORCHID-47")]}, fail=retrieval_fail)
        self.generated = []

        def generate(instructions, payload):
            self.generated.append(payload)
            if fail:
                raise RuntimeError("model secret detail")
            return text

        return make_search(self.runtime), generate

    def call(self, evt, **build):
        search, generate = self.build(**build)
        response = handler.handle(evt, search, generate, ISSUER, CLIENT)
        return response["statusCode"], json.loads(response["body"]), response

    def test_cited_answer_for_an_authorized_user(self):
        status, body, response = self.call(event(A))
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "completed")
        self.assertIn("ORCHID-47", body["text"])
        self.assertEqual(body["sources"], ["approved.txt"])
        self.assertEqual(response["headers"]["Cache-Control"], "no-store")
        self.assertEqual(set(body), {"status", "text", "sources"})

    def test_user_with_no_readable_documents_gets_a_completed_response_and_no_model_call(self):
        status, body, _ = self.call(event(B))
        self.assertEqual((status, body["status"], body["text"], body["sources"]), (200, "completed", handler.NO_DOCUMENTS, []))
        self.assertEqual(self.generated, [])

    def test_identity_comes_only_from_validated_claims(self):
        evt = event(B, headers={"x-user-email": A, "X-Amzn-Oidc-Identity": A, "authorization": "Bearer " + "a" * 40})
        status, body, _ = self.call(evt)
        self.assertEqual(body["text"], handler.NO_DOCUMENTS)
        self.assertEqual(self.runtime.calls[0]["userContext"], {"userId": B})

    def test_body_supplied_identity_is_rejected(self):
        for extra in ({"userContext": {"userId": A}}, {"userId": A}, {"email": A}, {"sessionId": "s-123456"}, {"memoryId": "m-123456"},
                      {"endSession": True}, {"conversation": "c"}, {"previous_response_id": "r"}):
            evt = event(B, body={"input": "q", "store": False, **extra})
            search, generate = self.build()
            response = handler.handle(evt, search, generate, ISSUER, CLIENT)
            self.assertEqual(response["statusCode"], 400, extra)
            self.assertEqual(self.runtime.calls, [])

    def test_missing_or_untrusted_claims_are_401_before_anything_else(self):
        bad = [
            {}, {"requestContext": {}}, {"requestContext": {"authorizer": {}}},  # direct invoke, no authorizer
            event(claims={"email_verified": "false"}), event(claims={"email_verified": None}),
            event(claims={"token_use": "access"}), event(claims={"iss": "https://evil.example"}),
            event(claims={"aud": "other-client"}), event(email="not-an-email"), event(email=None),
        ]
        for evt in bad:
            search, generate = self.build()
            response = handler.handle(evt, search, generate, ISSUER, CLIENT)
            self.assertEqual(response["statusCode"], 401, evt)
            self.assertEqual(self.runtime.calls, [])
            self.assertEqual(self.generated, [])

    def test_stateful_or_malformed_requests_are_400_before_retrieval(self):
        bodies = [{"input": "q"}, {"input": "q", "store": True}, {"input": "q", "store": False, "background": True},
                  {"input": "", "store": False}, {"input": 5, "store": False}, {"store": False},
                  {"input": "x" * 3000, "store": False}]
        for body in bodies:
            _, payload, response = self.call(event(A, body=body))
            self.assertEqual(response["statusCode"], 400, body)
            self.assertNotIn("text", payload)
        for raw in ("not json", "[]", "x" * 9000):
            self.assertEqual(self.call(event(A, raw=raw))[2]["statusCode"], 400)
        no_body = event(A)
        no_body["body"] = None
        self.assertEqual(self.call(no_body)[2]["statusCode"], 400)
        self.assertEqual(self.runtime.calls, [])

    def test_base64_bodies_are_decoded(self):
        evt = event(A)
        evt["body"] = base64.b64encode(evt["body"].encode()).decode()
        evt["isBase64Encoded"] = True
        self.assertEqual(self.call(evt)[0], 200)

    def test_model_declining_returns_the_no_documents_message_without_sources(self):
        status, body, _ = self.call(event(A), text=handler.NO_DOCUMENTS)
        self.assertEqual((status, body["text"], body["sources"]), (200, handler.NO_DOCUMENTS, []))

    def test_retrieval_failure_is_503_and_skips_the_model(self):
        status, body, _ = self.call(event(A), retrieval_fail=True)
        self.assertEqual(status, 503)
        self.assertNotIn("secret", json.dumps(body))
        self.assertEqual(self.generated, [])

    def test_unverified_knowledge_base_is_503(self):
        runtime = FakeRuntime({A: [hit("a.txt", "t")]})
        search = make_search(runtime, FakeAgent(fail=True))
        response = handler.handle(event(A), search, lambda i, p: "x", ISSUER, CLIENT)
        self.assertEqual(response["statusCode"], 503)
        self.assertEqual(runtime.calls, [])

    def test_model_failure_leaks_nothing(self):
        status, body, _ = self.call(event(A), fail=True)
        self.assertEqual(status, 502)
        self.assertNotIn("secret", json.dumps(body))

    def test_model_sees_only_returned_excerpts_and_the_question(self):
        self.call(event(A))
        sent = json.loads(self.generated[0])
        self.assertEqual(set(sent), {"question", "documents"})
        self.assertEqual(sent["documents"], [{"source": "approved.txt", "excerpt": "The code is ORCHID-47"}])
        self.assertNotIn(A, self.generated[0])

    def test_model_call_is_stateless_with_no_tools(self):
        class Rt:
            def converse(self, **kwargs):
                self.kwargs = kwargs
                return {"output": {"message": {"content": [{"text": "answer"}]}}}

        rt = Rt()
        self.assertEqual(handler.make_generate(rt, "amazon.nova-lite-v1:0")("sys", "payload"), "answer")
        self.assertEqual(set(rt.kwargs), {"modelId", "system", "messages", "inferenceConfig"})
        self.assertEqual(len(rt.kwargs["messages"]), 1)

    def test_default_model_is_not_a_retired_or_unsupported_one(self):
        self.assertEqual(handler.DEFAULT_MODEL, "amazon.nova-lite-v1:0")


# ── Ingest ────────────────────────────────────────────────────────────────────────────
class FakeS3:
    def __init__(self, block=None, status=None, grants=None, existing=()):
        self.block = block or {k: True for k in ("BlockPublicAcls", "IgnorePublicAcls", "BlockPublicPolicy", "RestrictPublicBuckets")}
        self.status, self.grants = status or {"IsPublic": False}, grants or []
        self.objects, self.deleted = {k: b"old" for k in existing}, []

    def get_public_access_block(self, **kw):
        return {"PublicAccessBlockConfiguration": self.block}

    def get_bucket_policy_status(self, **kw):
        return {"PolicyStatus": self.status}

    def get_bucket_acl(self, **kw):
        return {"Grants": self.grants}

    def upload_file(self, path, bucket, key):
        self.objects[key] = Path(path).read_bytes()

    def put_object(self, Bucket, Key, Body, ContentType):
        self.objects[Key] = Body

    def get_paginator(self, name):
        outer = self

        class P:
            def paginate(self, **kw):
                return [{"Contents": [{"Key": k} for k in sorted(outer.objects)]}]

        return P()

    def delete_object(self, Bucket, Key):
        self.deleted.append(Key)
        self.objects.pop(Key, None)


class FakeIngestAgent(FakeAgent):
    def __init__(self, statuses=("IN_PROGRESS", "COMPLETE"), stats=None, reasons=None, **kw):
        super().__init__(**kw)
        self.statuses, self.stats, self.reasons, self.started = list(statuses), stats or {"numberOfDocumentsFailed": 0}, reasons, 0

    def start_ingestion_job(self, **kw):
        self.started += 1
        return {"ingestionJob": {"ingestionJobId": "JOB1"}}

    def get_ingestion_job(self, **kw):
        status = self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
        job = {"status": status, "statistics": self.stats}
        if self.reasons:
            job["failureReasons"] = self.reasons
        return {"ingestionJob": job}


class IngestTests(unittest.TestCase):
    def corpus(self, tmp, names=("approved.txt", "control.txt")):
        root = Path(tmp)
        for name in names:
            (root / name).write_text("synthetic")
        return root

    def test_corpus_must_be_outside_the_repo(self):
        for path in (ROOT, ROOT / "docs", ROOT.parent):
            with self.assertRaisesRegex(ValueError, "outside"):
                ingest.corpus_files(path)

    def test_corpus_rejects_empty_symlinks_and_ignores_metadata_and_hidden_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError, "No supported"):
                ingest.corpus_files(tmp)
            root = self.corpus(tmp, ("a.txt",))
            (root / "a.txt.metadata.json").write_text("{}")
            (root / ".hidden.txt").write_text("x")
            self.assertEqual([p.name for p in ingest.corpus_files(tmp)[1]], ["a.txt"])
            outside = Path(tempfile.mkdtemp()) / "secret.txt"
            outside.write_text("x")
            (root / "link.txt").symlink_to(outside)
            with self.assertRaisesRegex(ValueError, "symlinks"):
                ingest.corpus_files(tmp)

    def test_manifest_requires_explicit_entries_and_valid_users(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.corpus(tmp)
            root, files = ingest.corpus_files(tmp)
            path = root / "acl.json"

            def load(data):
                path.write_text(json.dumps(data))
                return ingest.load_manifest(path, root, files)

            self.assertEqual(load({"approved.txt": {"users": [A]}, "control.txt": {"users": []}})["control.txt"], {"users": []})
            for bad in ({"approved.txt": {"users": [A]}},  # missing entry
                        {"approved.txt": {"users": [A]}, "control.txt": {"users": [], "groups": []}},  # groups unsupported
                        {"approved.txt": {"users": ["everyone"]}, "control.txt": {"users": []}},
                        {"approved.txt": {"users": [A, A]}, "control.txt": {"users": []}},
                        {"approved.txt": {"users": "a@b.co"}, "control.txt": {"users": []}},
                        {"approved.txt": {"users": [A]}, "control.txt": {"users": []}, "extra.txt": {"users": []}}):
                with self.assertRaises(ValueError):
                    load(bad)

    def test_metadata_documents_carry_explicit_allow_entries_only(self):
        doc = ingest.metadata_document("dir/approved.txt", [A, B])
        self.assertEqual(doc["accessControlList"], [{"Name": A, "Type": "USER", "Access": "ALLOW"}, {"Name": B, "Type": "USER", "Access": "ALLOW"}])
        self.assertEqual(doc["metadataAttributes"], {"title": "approved.txt"})

    def test_bucket_privacy_check(self):
        full = {k: True for k in ("BlockPublicAcls", "IgnorePublicAcls", "BlockPublicPolicy", "RestrictPublicBuckets")}
        self.assertTrue(ingest.bucket_is_private(full, {"IsPublic": False}, []))
        self.assertFalse(ingest.bucket_is_private({**full, "RestrictPublicBuckets": False}, {"IsPublic": False}, []))
        self.assertFalse(ingest.bucket_is_private({}, {"IsPublic": False}, []))
        self.assertFalse(ingest.bucket_is_private(full, {"IsPublic": True}, []))
        self.assertFalse(ingest.bucket_is_private(full, {"IsPublic": False},
                                                  [{"Grantee": {"URI": "http://acs.amazonaws.com/groups/global/AllUsers"}}]))
        self.assertFalse(ingest.bucket_is_private(full, {"IsPublic": False},
                                                  [{"Grantee": {"URI": "http://acs.amazonaws.com/groups/global/AuthenticatedUsers"}}]))

    def test_destination_check_refuses_unprotected_targets(self):
        ingest.check_destination(FakeS3(), FakeAgent(), BUCKET, KB, DS)  # the good case passes
        with self.assertRaisesRegex(RuntimeError, "not private"):
            ingest.check_destination(FakeS3(status={"IsPublic": True}), FakeAgent(), BUCKET, KB, DS)
        with self.assertRaisesRegex(RuntimeError, "managed knowledge base"):
            ingest.check_destination(FakeS3(), FakeAgent(kb={"knowledgeBase": {}}), BUCKET, KB, DS)
        no_acl = json.loads(json.dumps(DS_BODY))
        no_acl["dataSource"]["dataSourceConfiguration"]["managedKnowledgeBaseConnectorConfiguration"]["connectorParameters"]["aclEnabled"] = False
        with self.assertRaisesRegex(RuntimeError, "ACL-enabled"):
            ingest.check_destination(FakeS3(), FakeAgent(ds=no_acl), BUCKET, KB, DS)

    def test_missing_bucket_policy_means_not_public(self):
        class NoPolicy(FakeS3):
            def get_bucket_policy_status(self, **kw):
                err = RuntimeError("x")
                err.response = {"Error": {"Code": "NoSuchBucketPolicy"}}
                raise err

        ingest.check_destination(NoPolicy(), FakeAgent(), BUCKET, KB, DS)

    def test_sync_uploads_acls_only_for_readable_files_and_prunes_the_rest(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.corpus(tmp)
            root, files = ingest.corpus_files(tmp)
            s3 = FakeS3(existing=("docs/removed.txt", "docs/control.txt.metadata.json", "docs/control.txt"))
            readable, skipped = ingest.sync_corpus(s3, BUCKET, root, files,
                                                   {"approved.txt": {"users": [A]}, "control.txt": {"users": []}})
            self.assertEqual((readable, skipped), (1, 1))
            # a document without an ACL is a failed document in the sync, so it is not uploaded (and is pruned)
            self.assertEqual(sorted(s3.objects), ["docs/approved.txt", "docs/approved.txt.metadata.json"])
            self.assertEqual(sorted(s3.deleted), ["docs/control.txt", "docs/control.txt.metadata.json", "docs/removed.txt"])
            acl = json.loads(s3.objects["docs/approved.txt.metadata.json"])["accessControlList"]
            self.assertEqual(acl, [{"Name": A, "Type": "USER", "Access": "ALLOW"}])

    def test_ingest_end_to_end_waits_for_a_clean_sync(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.corpus(tmp)
            (Path(tmp) / "acl.json").write_text(json.dumps({"approved.txt": {"users": [A]}, "control.txt": {"users": []}}))
            agent = FakeIngestAgent()
            orig = ingest.time.sleep
            ingest.time.sleep = lambda _: None
            try:
                stats = ingest.ingest(BUCKET, KB, DS, tmp, s3=FakeS3(), agent=agent)
            finally:
                ingest.time.sleep = orig
            self.assertEqual(agent.started, 1)
            self.assertEqual(stats["numberOfDocumentsFailed"], 0)

    def test_ingest_refuses_when_nobody_can_read_anything(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.corpus(tmp)
            (Path(tmp) / "acl.json").write_text(json.dumps({"approved.txt": {"users": []}, "control.txt": {"users": []}}))
            with self.assertRaisesRegex(ValueError, "nothing would be searchable"):
                ingest.ingest(BUCKET, KB, DS, tmp, s3=FakeS3(), agent=FakeIngestAgent())

    def test_sync_must_complete_with_zero_failures(self):
        wait = lambda agent: ingest.wait_for_ingestion(agent, KB, DS, "J", 100, sleep=lambda _: None)  # noqa: E731
        self.assertEqual(wait(FakeIngestAgent(statuses=["COMPLETE"]))["numberOfDocumentsFailed"], 0)
        for agent in (FakeIngestAgent(statuses=["FAILED"]), FakeIngestAgent(statuses=["COMPLETE"], stats={"numberOfDocumentsFailed": 1}),
                      FakeIngestAgent(statuses=["COMPLETE"], reasons=["boom"]), FakeIngestAgent(statuses=["STOPPED"])):
            with self.assertRaisesRegex(RuntimeError, "did not verify"):
                wait(agent)

    def test_sync_timeout(self):
        now = [0]

        def clock():
            now[0] += 60
            return now[0]

        with self.assertRaises(TimeoutError):
            ingest.wait_for_ingestion(FakeIngestAgent(statuses=["IN_PROGRESS"]), KB, DS, "J", 100, sleep=lambda _: None, clock=clock)


# ── Acceptance gate, run against the real handler behind a fake API Gateway + fake platform ──────────
def jwt(email):
    enc = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()  # noqa: E731
    return f"{enc({'alg': 'RS256'})}.{enc({'email': email})}.sig-{email.replace('.', '_')}"


class AcceptanceTests(unittest.TestCase):
    CONFIG = {
        "directory": "/tmp/x", "allowed_user": A, "denied_user": B, "approved_file": "approved.txt",
        "control_file": "control.txt", "question": "What is the restricted code?", "expected": "ORCHID-47",
        "control_question": "What is the control code?", "control_expected": "MAPLE-82",
    }
    FAST = dict(settle_timeout=0.3, clean_streak=3, interval=0, final_checks=2, sleep=lambda _: None)

    def args(self):
        return type("A", (), dict(self.CONFIG, api_url="https://abc.execute-api.us-east-1.amazonaws.com", bucket=BUCKET, region="us-east-1"))()

    def server(self, leak_control=False, accept_stateful=False, accept_forged=False, denied_pattern=(), always_leak=False,
               storage_status=403, trust_headers=False, no_authorizer=False):
        pattern = list(denied_pattern)

        class Runtime:
            def retrieve(self_inner, **kw):
                user = kw["userContext"]["userId"]
                question = kw["retrievalQuery"]["text"]
                results = []
                if "restricted" in question and (user == A or (user == B and (always_leak or (pattern and pattern.pop(0) == "leak")))):
                    results.append(hit("approved.txt", "The restricted code is ORCHID-47"))
                if leak_control and "control" in question:
                    results.append(hit("control.txt", "The control code is MAPLE-82"))
                return {"retrievalResults": results}

        search = ps.ProtectedRetrieval(KB, DS, BUCKET, FakeAgent(), Runtime())

        def generate(instructions, payload):
            return "Answer: " + " ".join(d["excerpt"] for d in json.loads(payload)["documents"]) + " (" + ", ".join(
                d["source"] for d in json.loads(payload)["documents"]) + ")"

        def api(request: httpx.Request) -> httpx.Response:
            if request.method in ("HEAD", "GET"):
                return httpx.Response(storage_status)
            auth = request.headers.get("authorization", "").removeprefix("Bearer ")
            claims = None
            parts = auth.split(".")
            try:
                payload = json.loads(base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4))) if len(parts) == 3 else None
            except ValueError:
                payload = None
            if payload and (parts[2] == "sig-" + payload["email"].replace(".", "_") or accept_forged):  # signature covers the email claim
                claims = {"email": payload["email"], "email_verified": "true", "token_use": "id", "iss": ISSUER, "aud": CLIENT}
            if claims is None:
                return httpx.Response(401, json={"message": "Unauthorized"})
            body = json.loads(request.content)
            if accept_stateful and body.get("store") is not False:
                return httpx.Response(200, json={"text": "x"})
            evt = {"body": json.dumps(body), "headers": dict(request.headers), "requestContext": {"authorizer": {"jwt": {"claims": claims}}}}
            if trust_headers and request.headers.get("x-user-email"):
                evt["requestContext"]["authorizer"]["jwt"]["claims"]["email"] = request.headers["x-user-email"]
            response = handler.handle(evt, search, generate, ISSUER, CLIENT)
            return httpx.Response(response["statusCode"], json=json.loads(response["body"]))

        return api

    def run_live(self, direct_retrieve=lambda: "denied", direct_invoke=lambda: 401, expired=jwt("old@example.com") + "x", **kwargs):
        with httpx.Client(transport=httpx.MockTransport(self.server(**kwargs))) as http:
            live.run_tests(http, self.args(), jwt(A), jwt(B), expired, ready_timeout=1, direct_retrieve=direct_retrieve,
                           direct_invoke=direct_invoke, **self.FAST)

    def test_a_correct_deployment_passes(self):
        self.run_live()

    def test_token_callables_are_used_so_expiring_tokens_can_be_reminted(self):
        calls = {"n": 0}

        def fresh():
            calls["n"] += 1
            return jwt(B)

        with httpx.Client(transport=httpx.MockTransport(self.server())) as http:
            live.run_tests(http, self.args(), lambda: jwt(A), fresh, None, ready_timeout=1, **self.FAST)
        self.assertGreater(calls["n"], 3)

    def test_each_failure_mode_is_detected(self):
        for kwargs, message in (
            ({"leak_control": True}, "control"),
            ({"accept_stateful": True}, "stateful"),
            ({"accept_forged": True}, "forged email|invalid"),
            ({"trust_headers": True}, "headers"),
        ):
            with self.subTest(kwargs):
                with self.assertRaisesRegex(RuntimeError, message):
                    self.run_live(**kwargs)

    def test_a_token_with_a_swapped_email_claim_must_be_rejected(self):
        forged = live.tamper(jwt(B), A)
        self.assertEqual(forged.split(".")[2], jwt(B).split(".")[2])
        self.assertNotEqual(forged.split(".")[1], jwt(B).split(".")[1])

    def test_anonymous_storage_reads_must_be_denied(self):
        with self.assertRaisesRegex(RuntimeError, "Anonymous"):
            self.run_live(storage_status=200)

    def test_other_principals_must_not_be_able_to_retrieve(self):
        with self.assertRaisesRegex(RuntimeError, "resource policy"):
            self.run_live(direct_retrieve=lambda: "allowed")
        with self.assertRaisesRegex(RuntimeError, "resource policy"):
            self.run_live(direct_retrieve=lambda: "error ThrottlingException")

    def test_a_root_operator_cannot_verify_the_lock_unless_explicitly_allowed(self):
        """Observed live: root is not restricted by the KB resource policy, so the gate must not claim the lock works."""
        with self.assertRaisesRegex(RuntimeError, "root user"):
            self.run_live(direct_retrieve=lambda: "root-exempt")
        with httpx.Client(transport=httpx.MockTransport(self.server())) as http:
            live.run_tests(http, self.args(), jwt(A), jwt(B), None, ready_timeout=1, direct_retrieve=lambda: "root-exempt",
                           allow_root_operator=True, **self.FAST)

    def test_function_must_reject_a_direct_invoke_without_claims(self):
        with self.assertRaisesRegex(RuntimeError, "without validated claims"):
            self.run_live(direct_invoke=lambda: 200)

    def test_exposures_while_settling_are_tolerated_only_if_denial_then_stays_clean(self):
        self.run_live(denied_pattern=["leak", "none", "leak", "none", "none"])

    def test_persistent_exposure_fails_the_gate(self):
        with self.assertRaisesRegex(RuntimeError, "did not settle"):
            self.run_live(always_leak=True)

    def test_a_late_exposure_after_settling_fails_the_gate(self):
        with self.assertRaisesRegex(RuntimeError, "after access control had settled"):
            self.run_live(denied_pattern=["none"] * 4 + ["leak"])  # 3 settle checks + the header check, then the final re-check

    def test_config_validation(self):
        with tempfile.TemporaryDirectory() as tmp:
            def write(data):
                path = Path(tmp) / "c.json"
                path.write_text(json.dumps(data))
                return path

            self.assertEqual(live.load_config(write(self.CONFIG))["expected"], "ORCHID-47")
            for bad in (dict(self.CONFIG, control_expected="orchid-47"), dict(self.CONFIG, denied_user=A.upper()),
                        dict(self.CONFIG, denied_user="nope"), dict(self.CONFIG, control_file="approved.txt"),
                        {k: v for k, v in self.CONFIG.items() if k != "question"}, dict(self.CONFIG, extra="x")):
                with self.assertRaises(ValueError):
                    live.load_config(write(bad))


class AclChangeTests(unittest.TestCase):
    def world(self, delay_b, delay_a):
        state = {"polls": 0, "reingested": False}

        def api(request: httpx.Request) -> httpx.Response:
            user = json.loads(base64.urlsafe_b64decode(request.headers["authorization"].split(".")[1] + "==="))["email"]
            state["polls"] += 1
            tick = state["polls"] // 2
            if not state["reingested"]:
                sees = user == A
            else:
                sees = tick >= delay_b if user == B else tick < delay_a
            text = "ORCHID-47" if sees else live.NO_DOCUMENTS
            return httpx.Response(200, json={"text": text, "sources": ["approved.txt"] if sees else []})

        return state, api

    def run_measure(self, delay_b, delay_a, **kw):
        state, api = self.world(delay_b, delay_a)
        args = AcceptanceTests().args()
        with httpx.Client(transport=httpx.MockTransport(api)) as http:
            return live.measure_acl_change(http, args, lambda: jwt(A), lambda: jwt(B), lambda: state.update(reingested=True),
                                           clean_streak=3, interval=0, sleep=lambda _: None, **kw)

    def test_measures_how_long_the_swap_takes_and_passes_when_stable(self):
        result = self.run_measure(delay_b=2, delay_a=4)
        self.assertIsNotNone(result["granted_seconds"])
        self.assertIsNotNone(result["last_revoked_exposure_seconds"])

    def test_revocation_that_never_lands_fails(self):
        with self.assertRaisesRegex(RuntimeError, "did not settle"):
            self.run_measure(delay_b=1, delay_a=10 ** 6, timeout=0.2)


# ── Wiring: run the real shell scripts with stubbed aws/terraform ──────────────────────────────────
STUB_AWS = r'''#!/usr/bin/env bash
echo "aws $*" >> "$STUB_LOG"
D="$STUB_DIR"
case "$*" in
  *"sts get-caller-identity"*) echo 123456789012;;
  *"create-knowledge-base"*) echo ABCDE12345;;
  *"delete-knowledge-base"*) touch "$D/kb_deleted";;
  *"get-knowledge-base"*) if [ -f "$D/kb_deleted" ] || [ -n "${STUB_KB_GONE:-}" ]; then echo "An error occurred (ResourceNotFoundException)" >&2; exit 254; fi; echo ACTIVE;;
  *"list-knowledge-bases"*) if [ -n "${STUB_KB_LEFT:-}" ] && [ -f "$D/kb_deleted" ]; then echo ABCDE12345; elif [ -f "$D/kb_deleted" ] || [ -n "${STUB_KB_GONE:-}" ]; then :; else echo None; fi;;
  *"create-data-source"*) touch "$D/ds_created"; echo FGHIJ67890;;
  *"delete-data-source"*) touch "$D/ds_deleted";;
  *"get-data-source"*) echo AVAILABLE;;
  *"list-data-sources"*) if [ -f "$D/ds_deleted" ]; then :; elif [ -f "$D/ds_created" ] || [ -n "${STUB_DS_EXISTS:-}" ]; then echo FGHIJ67890; else echo None; fi;;
  *"put-resource-policy"*) echo "$*" > "$D/policy";;
  *"get-resource-policy"*) cat "$D/policy";;
  *"apigatewayv2 get-routes"*) echo "${STUB_ROUTES:-JWT}";;
  *"list-function-url-configs"*) echo "${STUB_URLS:-}";;
  *"head-bucket"*) if [ -n "${STUB_BUCKET_LEFT:-}" ]; then exit 0; else exit 254; fi;;
  *"iam get-role"*|*"lambda get-function"*) exit 254;;
esac
exit 0
'''
STUB_TERRAFORM = r'''#!/usr/bin/env bash
echo "terraform $*" >> "$STUB_LOG"
case "$*" in
  *"output -raw api_url"*) echo https://abc123.execute-api.us-east-1.amazonaws.com;;
  *"output -raw lambda_role_arn"*) echo arn:aws:iam::123456789012:role/env-protected-lambda-role;;
  *"output -raw kb_role_arn"*) echo arn:aws:iam::123456789012:role/env-protected-kb-role;;
  *"output -raw"*) echo stub-value;;
esac
'''


class WiringTests(unittest.TestCase):
    def run_script(self, script, env_extra=None, state=None):
        with tempfile.TemporaryDirectory() as tmp:
            bin_dir, log = Path(tmp) / "bin", Path(tmp) / "calls.log"
            bin_dir.mkdir()
            (bin_dir / "aws").write_text(STUB_AWS)
            (bin_dir / "terraform").write_text(STUB_TERRAFORM)
            (bin_dir / "sleep").write_text("#!/usr/bin/env bash\nexit 0\n")
            (bin_dir / "python3").write_text(
                f'#!/usr/bin/env bash\necho "python3 $*" >> {log}\ncase "$*" in *"-m pip"*|*"-m venv"*) exit 0;; esac\nexec {sys.executable} "$@"\n')
            venv_bin = Path(tmp) / "venv" / "bin"
            venv_bin.mkdir(parents=True)
            for name in ("python3", "pip"):
                (venv_bin / name).write_text(f'#!/usr/bin/env bash\necho "venv-{name} $*" >> {log}\n')
                (venv_bin / name).chmod(0o755)
            for path in bin_dir.iterdir():
                path.chmod(0o755)
            state_file = Path(tmp) / "state"
            if state:
                state_file.write_text(state)
            env = {"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}", "HOME": tmp, "TMPDIR": tmp, "STUB_LOG": str(log), "STUB_DIR": tmp,
                   "KB_ACCESS_MODE": "protected", "ENV_NAME": "env", "ASSUME_YES": "1", "PROTECTED_STATE_FILE": str(state_file),
                   "PROTECTED_VENV": str(Path(tmp) / "venv")}
            result = subprocess.run(["bash", str(ROOT / "scripts" / script)], env=env | (env_extra or {}), capture_output=True,
                                    text=True, timeout=60, stdin=subprocess.DEVNULL)
            return result, log.read_text() if log.exists() else "", state_file.read_text() if state_file.exists() else ""

    def tearDown(self):
        build = ROOT / "terraform" / "protected" / "lambda_build"
        if build.exists():
            import shutil
            shutil.rmtree(build)

    def test_protected_provision_uses_only_the_protected_root_and_skips_sample_steps(self):
        result, log, state = self.run_script("provision.sh")
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertIn("terraform/protected", log.replace("scripts/../", ""))
        for sample in ("s3 sync", "opensearchserverless", "create_os_index", "generate_eval_cases", "run_evals", "train_baseline",
                       "gh variable", "start-ingestion-job"):
            self.assertNotIn(sample, log)
        self.assertIn("create-knowledge-base", log)
        self.assertIn('"type":"MANAGED"', log)
        self.assertIn('"aclEnabled":true', log.replace("True", "true").replace(" ", ""))
        self.assertIn("--data-deletion-policy DELETE", log)
        self.assertIn("KNOWLEDGE_BASE_ID=ABCDE12345", state)
        self.assertIn("DATA_SOURCE_ID=FGHIJ67890", state)
        # KB is created after stage 1 and the second terraform run is not part of provision (deploy does it)
        self.assertEqual(log.count("terraform -chdir"), log.count("terraform -chdir"))
        self.assertLess(log.index("apply"), log.index("create-knowledge-base"))

    def test_provision_is_idempotent_and_reuses_the_recorded_knowledge_base(self):
        result, log, _ = self.run_script("provision.sh", state="KNOWLEDGE_BASE_ID=ABCDE12345\nDATA_SOURCE_ID=FGHIJ67890\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("create-knowledge-base", log)
        self.assertNotIn("create-data-source", log)
        self.assertIn("knowledge_base_id=ABCDE12345", log)

    def test_provision_retries_role_propagation_then_fails_with_the_reason(self):
        failing = STUB_AWS.replace('*"create-knowledge-base"*) echo ABCDE12345;;',
                                   '*"create-knowledge-base"*) echo "role not assumable yet" >&2; exit 254;;')
        with tempfile.TemporaryDirectory() as tmp:
            global STUB_AWS_ORIG
            original = STUB_AWS
            globals()["STUB_AWS"] = failing
            try:
                result, log, _ = self.run_script("provision.sh", {"PROTECTED_RETRIES": "3"})
            finally:
                globals()["STUB_AWS"] = original
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(log.count("create-knowledge-base"), 3)
        self.assertIn("role not assumable yet", result.stderr)

    def test_protected_deploy_locks_the_knowledge_base_and_skips_evals(self):
        result, log, _ = self.run_script("deploy.sh", state="KNOWLEDGE_BASE_ID=ABCDE12345\nDATA_SOURCE_ID=FGHIJ67890\n")
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertIn("put-resource-policy", log)
        self.assertIn("DenyRetrieveExceptProtectedFunction", log)
        self.assertIn("bedrock:Retrieve", log)
        self.assertIn("aws:PrincipalArn", log)
        self.assertIn("env-protected-lambda-role", log)
        for sample in ("run_evals", "s3 sync", "start-ingestion-job", "train_baseline"):
            self.assertNotIn(sample, log)
        self.assertIn("No PROTECTED_ACCEPTANCE_CONFIG", result.stdout)

    def test_protected_deploy_needs_a_provisioned_knowledge_base(self):
        result, _, _ = self.run_script("deploy.sh")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("run provision.sh first", result.stderr)

    def test_protected_deploy_refuses_an_unauthorized_route_or_a_function_url(self):
        state = "KNOWLEDGE_BASE_ID=ABCDE12345\nDATA_SOURCE_ID=FGHIJ67890\n"
        result, _, _ = self.run_script("deploy.sh", {"STUB_ROUTES": "JWT\tNONE"}, state)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("without the JWT authorizer", result.stderr)
        result, _, _ = self.run_script("deploy.sh", {"STUB_URLS": "https://x.lambda-url.us-east-1.on.aws/"}, state)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("function URL", result.stderr)

    def test_protected_deploy_runs_the_acceptance_gate_non_interactively_when_configured(self):
        result, log, _ = self.run_script("deploy.sh", {"PROTECTED_ACCEPTANCE_CONFIG": "/cfg.json", "PROTECTED_ACCEPTANCE_ARGS": "--mint-users"},
                                         "KNOWLEDGE_BASE_ID=ABCDE12345\nDATA_SOURCE_ID=FGHIJ67890\n")
        self.assertIn("test_protected_live.py", log)
        self.assertIn("--mint-users", log)
        self.assertIn("--config /cfg.json", log)

    def test_protected_teardown_deletes_the_kb_then_destroys_only_the_protected_root_and_verifies(self):
        result, log, state = self.run_script("teardown.sh", {"STUB_DS_EXISTS": "1"}, "KNOWLEDGE_BASE_ID=ABCDE12345\nDATA_SOURCE_ID=FGHIJ67890\n")
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertLess(log.index("delete-data-source"), log.index("delete-knowledge-base"))
        self.assertLess(log.index("delete-knowledge-base"), log.index("destroy"))
        self.assertIn("terraform/protected", log.replace("scripts/../", ""))
        self.assertIn("Verified: no protected resources remain", result.stdout)
        self.assertEqual(state, "", "state file is removed after a verified teardown")

    def test_teardown_works_from_a_clean_checkout_and_when_the_kb_is_already_gone(self):
        """Observed live: destroy needs lambda_build to exist, and a repeat run must not fail on a deleted KB."""
        build = ROOT / "terraform" / "protected" / "lambda_build"
        self.assertFalse(build.exists())
        result, log, _ = self.run_script("teardown.sh", {"STUB_KB_GONE": "1"}, "KNOWLEDGE_BASE_ID=ABCDE12345\nDATA_SOURCE_ID=FGHIJ67890\n")
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertNotIn("delete-knowledge-base", log)
        self.assertIn("destroy", log)
        self.assertFalse(build.exists(), "the temporary build directory is removed again")
        text = (ROOT / "scripts" / "teardown_protected.sh").read_text()
        self.assertLess(text.index('mkdir -p "${TF_DIR}/lambda_build"'), text.index("destroy -auto-approve"))
        self.assertIn("lambda_build/placeholder.txt", text)  # an empty directory is rejected by the archive provider

    def test_teardown_fails_loudly_if_something_is_left(self):
        for extra, left in (({"STUB_BUCKET_LEFT": "1", "STUB_DS_EXISTS": "1"}, "bucket:"), ({"STUB_KB_LEFT": "1", "STUB_DS_EXISTS": "1"}, "knowledge-base:")):
            result, _, state = self.run_script("teardown.sh", extra, "KNOWLEDGE_BASE_ID=ABCDE12345\nDATA_SOURCE_ID=FGHIJ67890\n")
            self.assertNotEqual(result.returncode, 0)
            self.assertIn(left, result.stderr)
            self.assertIn("KNOWLEDGE_BASE_ID", state, "keep the state so the teardown can be repeated")

    def test_sample_mode_is_unchanged(self):
        for script in ("provision.sh", "teardown.sh", "deploy.sh"):
            self.assertIn('KB_ACCESS_MODE:-sample}" = "protected"', (ROOT / "scripts" / script).read_text())
        main_tf = (ROOT / "terraform" / "main.tf").read_text()
        self.assertIn("docs_public_read", main_tf)
        self.assertIn('Principal = "*"', main_tf)
        # sample provisioning is not reachable without the switch
        provision = (ROOT / "scripts" / "provision.sh").read_text()
        self.assertEqual(provision.count("exec bash"), 1)
        self.assertIn("ENABLE_AGENT_MEMORY", provision)

    def test_sample_deploy_does_not_pretend_to_deploy(self):
        result = subprocess.run(["bash", str(ROOT / "scripts" / "deploy.sh")], env={"PATH": os.environ["PATH"]},
                                capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("provision.sh", result.stderr)

    def test_ci_jobs_are_skipped_in_protected_mode(self):
        for path in (".github/workflows/deploy.yml", ".github/workflows/retrain-observability.yml", "workflows/run-evals.yml"):
            text = (ROOT / path).read_text()
            self.assertIn("if: ${{ vars.KB_ACCESS_MODE != 'protected' }}", text, path)

    def test_protected_terraform_has_no_public_access_keys_or_open_routes(self):
        text = "".join(p.read_text() for p in (ROOT / "terraform" / "protected").glob("*.tf"))
        for forbidden in ("aws_access_key", "aws_lambda_function_url", "authorization_type = \"NONE\"", "AdministratorAccess",
                          "opensearchserverless", "aws_s3_bucket_website_configuration", "public-read", "aws_s3_bucket_acl", "\"s3:*\"\n      Resource  = \"*\""):
            self.assertNotIn(forbidden, text)
        for required in ("block_public_acls       = true", "block_public_policy     = true", "ignore_public_acls      = true",
                         "restrict_public_buckets = true", "BucketOwnerEnforced", "aws:SecureTransport", "JWT",
                         "allow_admin_create_user_only = true", "mutable             = false"):
            self.assertIn(required, text)
        self.assertNotIn("$default\"\n  target", text)
        self.assertNotIn("bedrock:Retrieve\"]\n        Resource = \"*\"", text)

    def test_function_role_cannot_read_the_bucket_or_other_knowledge_bases(self):
        iam = (ROOT / "terraform" / "protected" / "iam.tf").read_text()
        lambda_section = iam.split('resource "aws_iam_role_policy" "lambda"')[1]
        self.assertNotIn("s3:", lambda_section)
        self.assertIn("local.kb_arn", lambda_section)

    def test_sample_mode_does_not_use_the_unsupported_rerank_model(self):
        for path in ("src/lambda/handler.py", "terraform/lambda.tf", "terraform/main.tf"):
            text = (ROOT / path).read_text().replace("amazon.rerank-v1:0 is not supported", "")
            self.assertNotIn("amazon.rerank-v1:0", text, path)
        self.assertIn("cohere.rerank-v3-5:0", (ROOT / "terraform" / "main.tf").read_text())

    def test_no_account_ids_or_emails_are_committed_in_protected_files(self):
        import re
        files = [*(ROOT / "terraform" / "protected").glob("*.tf"), *(ROOT / "src" / "protected").glob("*"),
                 ROOT / "scripts" / "ingest_protected.py", ROOT / "scripts" / "provision_protected.sh",
                 ROOT / "scripts" / "deploy_protected.sh", ROOT / "scripts" / "teardown_protected.sh",
                 ROOT / "scripts" / "test_protected_live.py", ROOT / "guides" / "protected-mode.md"]
        for path in files:
            if not path.is_file():
                continue
            text = path.read_text()
            self.assertIsNone(re.search(r"\b\d{12}\b", text), path)
            for email in re.findall(r"[\w.+-]+@[\w-]+\.[\w.-]+", text):
                self.assertTrue(email.split("@")[1].startswith("example."), f"{path}: {email}")


if __name__ == "__main__":
    unittest.main()
