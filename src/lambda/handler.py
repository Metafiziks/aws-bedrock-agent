import json
import os
import uuid
import boto3

bedrock = boto3.client("bedrock-agent-runtime", region_name=os.environ.get("AWS_REGION", "us-east-1"))

AGENT_ID       = os.environ["AGENT_ID"]
AGENT_ALIAS_ID = os.environ["AGENT_ALIAS_ID"]


def _s3_to_https(uri):
    """Convert s3://bucket/key to public HTTPS URL."""
    if not uri or not uri.startswith("s3://"):
        return uri
    parts = uri[5:].split("/", 1)
    bucket = parts[0]
    key = parts[1] if len(parts) > 1 else ""
    return f"https://{bucket}.s3.amazonaws.com/{key}"


def lambda_handler(event, context):
    body = json.loads(event.get("body") or "{}")
    message = body.get("message", "")

    if not message:
        return {"statusCode": 400, "body": json.dumps({"error": "message field required"})}

    session_id = body.get("sessionId", str(uuid.uuid4()))

    response = bedrock.invoke_agent(
        agentId=AGENT_ID,
        agentAliasId=AGENT_ALIAS_ID,
        sessionId=session_id,
        inputText=message,
    )

    answer = ""
    seen_urls = []

    for evt in response["completion"]:
        if "chunk" not in evt:
            continue
        chunk = evt["chunk"]
        answer += chunk["bytes"].decode("utf-8")

        # Extract citations from attribution metadata
        for citation in chunk.get("attribution", {}).get("citations", []):
            for ref in citation.get("retrievedReferences", []):
                loc = ref.get("location", {})
                uri = loc.get("s3Location", {}).get("uri") or loc.get("uri", "")
                url = _s3_to_https(uri)
                title = ref.get("metadata", {}).get("title") or uri.rsplit("/", 1)[-1]
                if url and url not in seen_urls:
                    seen_urls.append(url)
                    answer += f"\n- [{title}]({url})"

    return {
        "statusCode": 200,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps({"answer": answer, "sessionId": session_id}),
    }
