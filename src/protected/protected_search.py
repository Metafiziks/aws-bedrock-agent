"""Stateless retrieval from one ACL-enabled Amazon Bedrock Managed Knowledge Base.

Bedrock filters results against the per-document ACL (allowed/denied user emails) for
the ``userContext.userId`` it is given. It does NOT authenticate that user: it trusts
the value. So this module only ever receives an email taken from a JWT claim that API
Gateway has already validated (see ``handler.verified_email``); it never accepts an
identity from the request body, headers or the model. It makes no access decisions of
its own: it checks that it is pointed at the intended protected knowledge base (fail
closed) and passes through only what the platform returned.

The knowledge base resource policy (provision/deploy scripts) additionally denies
``bedrock:Retrieve`` to every principal except this function's role, so no other caller
can retrieve with an arbitrary ``userContext``.
"""

import json
import logging
import re
import time
from urllib.parse import unquote, urlsplit

logger = logging.getLogger(__name__)

MAX_EXCERPT = 6000
MAX_RESULTS = 5
CONFIG_TTL = 30  # seconds a successful configuration check is trusted
EMAIL = re.compile(r"[A-Za-z0-9._%+'-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")
ID_PATTERN = re.compile(r"[0-9A-Za-z]{10}")
BUCKET_PATTERN = re.compile(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]")


class ProtectedRetrievalError(RuntimeError):
    """Protected retrieval could not be completed safely (the caller answers 503)."""


class IdentityRejected(ProtectedRetrievalError):
    """There is no verified end-user identity (the caller answers 401)."""


def validate_ids(knowledge_base_id: str, data_source_id: str, bucket: str) -> None:
    for label, value, pattern in (
        ("knowledge base id", knowledge_base_id, ID_PATTERN),
        ("data source id", data_source_id, ID_PATTERN),
        ("bucket", bucket, BUCKET_PATTERN),
    ):
        if not isinstance(value, str) or not pattern.fullmatch(value):
            raise ValueError(f"Invalid {label}")


def is_protected_kb(response: object, knowledge_base_id: str) -> bool:
    kb = response.get("knowledgeBase") if isinstance(response, dict) else None
    return (
        isinstance(kb, dict)
        and kb.get("knowledgeBaseId") == knowledge_base_id
        and kb.get("status") == "ACTIVE"
        and isinstance(kb.get("knowledgeBaseConfiguration"), dict)
        and kb["knowledgeBaseConfiguration"].get("type") == "MANAGED"
    )


def acl_enabled_on_bucket(response: object, data_source_id: str, bucket: str) -> bool:
    """True only for an S3 data source on the protected bucket with ``aclEnabled`` set."""
    ds = response.get("dataSource") if isinstance(response, dict) else None
    if not isinstance(ds, dict) or ds.get("dataSourceId") != data_source_id:
        return False
    config = ds.get("dataSourceConfiguration")
    managed = config.get("managedKnowledgeBaseConnectorConfiguration") if isinstance(config, dict) else None
    params = managed.get("connectorParameters") if isinstance(managed, dict) else None
    if isinstance(params, str):  # observed live: the API returns the parameters as a JSON string
        try:
            params = json.loads(params)
        except ValueError:
            return False
    if not isinstance(params, dict) or params.get("aclEnabled") is not True or params.get("type") != "S3":
        return False
    connection = params.get("connectionConfiguration")
    return isinstance(connection, dict) and connection.get("bucketName") == bucket


def source_name(uri: object, bucket: str) -> str | None:
    """Return the filename only for objects in the protected bucket, else None.

    Observed live: managed knowledge bases return ``https://<bucket>.s3.amazonaws.com/<key>`` as the
    location (``s3://`` is accepted too). The host or bucket must be exactly the protected bucket.
    """
    if not isinstance(uri, str):
        return None
    parts = urlsplit(uri)
    host = parts.netloc.lower()
    if parts.scheme == "s3":
        ok = host == bucket
    elif parts.scheme == "https":
        ok = bool(re.fullmatch(re.escape(bucket) + r"\.s3(\.[a-z0-9-]+)?\.amazonaws\.com", host))
    else:
        ok = False
    if not ok:
        return None
    name = unquote(parts.path.lstrip("/").split("/")[-1])
    return name or None


class ProtectedRetrieval:
    def __init__(self, knowledge_base_id: str, data_source_id: str, bucket: str, agent, runtime, clock=time.monotonic):
        """``agent`` is a bedrock-agent client (metadata only), ``runtime`` a bedrock-agent-runtime client."""
        validate_ids(knowledge_base_id, data_source_id, bucket)
        self.kb_id, self.ds_id, self.bucket = knowledge_base_id, data_source_id, bucket
        self.agent, self.runtime, self.clock = agent, runtime, clock
        self._verified_at: float | None = None

    def verify_config(self) -> None:
        """Fail closed unless the configured KB is a managed KB whose data source has ACLs enabled."""
        now = self.clock()
        if self._verified_at is not None and now - self._verified_at < CONFIG_TTL:
            return
        try:
            kb = self.agent.get_knowledge_base(knowledgeBaseId=self.kb_id)
            ds = self.agent.get_data_source(knowledgeBaseId=self.kb_id, dataSourceId=self.ds_id)
        except Exception as exc:  # noqa: BLE001 - never leak provider details
            logger.warning("Protected configuration check failed (%s)", type(exc).__name__)
            self._verified_at = None
            raise ProtectedRetrievalError("Protected knowledge base configuration could not be verified") from None
        if not is_protected_kb(kb, self.kb_id):
            self._verified_at = None
            raise ProtectedRetrievalError("The knowledge base is not an active managed knowledge base")
        if not acl_enabled_on_bucket(ds, self.ds_id, self.bucket):
            self._verified_at = None
            raise ProtectedRetrievalError("The data source is not an ACL-enabled source on the protected bucket")
        self._verified_at = now

    def excerpts(self, question: str, user_email: str) -> list[dict[str, str]]:
        if not isinstance(user_email, str) or not EMAIL.fullmatch(user_email) or len(user_email) > 254:
            raise IdentityRejected("No verified user identity")
        self.verify_config()
        try:
            response = self.runtime.retrieve(
                knowledgeBaseId=self.kb_id,
                retrievalQuery={"text": question},
                userContext={"userId": user_email},  # the END USER, set only from a validated token claim
                retrievalConfiguration={"managedSearchConfiguration": {"numberOfResults": MAX_RESULTS}},
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Protected retrieval failed (%s)", type(exc).__name__)
            raise ProtectedRetrievalError("Protected document retrieval failed") from None
        results = response.get("retrievalResults") if isinstance(response, dict) else None
        if not isinstance(results, list):
            raise ProtectedRetrievalError("Invalid protected retrieval response")

        documents = []
        for result in results[:MAX_RESULTS]:
            if not isinstance(result, dict):
                continue
            location = result.get("location")
            s3 = location.get("s3Location") if isinstance(location, dict) else None
            name = source_name(s3.get("uri") if isinstance(s3, dict) else None, self.bucket)
            content = result.get("content")
            text = content.get("text") if isinstance(content, dict) else None
            # Ground only on references that come from the protected bucket.
            if name and isinstance(text, str) and text.strip():
                documents.append({"source": name, "excerpt": text[:MAX_EXCERPT]})
        return documents
