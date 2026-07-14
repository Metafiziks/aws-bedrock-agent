import json
import os
import uuid
import boto3

bedrock = boto3.client("bedrock-agent-runtime", region_name=os.environ.get("AWS_REGION", "us-east-1"))

AGENT_ID       = os.environ["AGENT_ID"]
AGENT_ALIAS_ID = os.environ["AGENT_ALIAS_ID"]


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

    # Stream the completion chunks
    answer = ""
    for event in response["completion"]:
        if "chunk" in event:
            answer += event["chunk"]["bytes"].decode("utf-8")

    return {
        "statusCode": 200,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps({"answer": answer, "sessionId": session_id}),
    }
