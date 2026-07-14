# ── IAM role for Bedrock Knowledge Base ────────────────────────────────────
resource "aws_iam_role" "kb" {
  name = "${local.name}-kb-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Principal = { Service = "bedrock.amazonaws.com" }
      Action = "sts:AssumeRole"
      Condition = {
        StringEquals = { "aws:SourceAccount" = var.account_id }
      }
    }]
  })
}

resource "aws_iam_role_policy" "kb_s3" {
  role = aws_iam_role.kb.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["s3:GetObject", "s3:ListBucket"]
      Resource = [aws_s3_bucket.docs.arn, "${aws_s3_bucket.docs.arn}/*"]
    }, {
      Effect   = "Allow"
      Action   = ["bedrock:InvokeModel"]
      Resource = "*"
    }, {
      Effect   = "Allow"
      Action   = ["aoss:APIAccessAll"]
      Resource = aws_opensearchserverless_collection.kb.arn
    }]
  })
}

resource "time_sleep" "wait_for_collection" {
  create_duration = "60s"
  depends_on      = [aws_opensearchserverless_collection.kb, aws_opensearchserverless_access_policy.kb]
}

# Bedrock does NOT auto-create the vector index — we must pre-create it via HTTP.
resource "null_resource" "create_os_index" {
  depends_on = [time_sleep.wait_for_collection, aws_iam_role_policy.kb_s3]

  triggers = {
    collection_endpoint = aws_opensearchserverless_collection.kb.collection_endpoint
  }

  provisioner "local-exec" {
    environment = {
      COLLECTION_ENDPOINT = aws_opensearchserverless_collection.kb.collection_endpoint
      AWS_REGION          = var.region
    }
    command = <<-SHELL
      python3 << 'PYEOF'
import os, boto3, json, http.client, ssl
from urllib.parse import urlparse
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest

session = boto3.Session()
creds = session.get_credentials().get_frozen_credentials()
endpoint = os.environ["COLLECTION_ENDPOINT"]
region = os.environ["AWS_REGION"]
index = "bedrock-kb-index"
url = endpoint + "/" + index

body = json.dumps({
    "settings": {"index": {"knn": True}},
    "mappings": {
        "properties": {
            "bedrock-knowledge-base-default-vector": {
                "type": "knn_vector",
                "dimension": 1536,
                "method": {"name": "hnsw", "engine": "faiss", "space_type": "l2"}
            },
            "AMAZON_BEDROCK_TEXT_CHUNK": {"type": "text"},
            "AMAZON_BEDROCK_METADATA": {"type": "text", "index": False}
        }
    }
}).encode("utf-8")

req = AWSRequest(method="PUT", url=url, data=body, headers={"Content-Type": "application/json"})
SigV4Auth(creds, "aoss", region).add_auth(req)
p = urlparse(url)
conn = http.client.HTTPSConnection(p.netloc, context=ssl.create_default_context())
conn.request("PUT", "/" + index, body=body, headers=dict(req.headers))
resp = conn.getresponse()
data = resp.read().decode()
print(resp.status, data)
try:
    err = json.loads(data).get("error", {})
    if err.get("type") == "resource_already_exists_exception":
        print("Index already exists, skipping")
        exit(0)
except Exception:
    pass
if resp.status not in (200, 201):
    raise Exception("Index creation failed: " + data)
print("Index created successfully")
PYEOF
    SHELL
  }
}

# ── Bedrock Knowledge Base ──────────────────────────────────────────────────
resource "aws_bedrockagent_knowledge_base" "docs" {
  name     = "${local.name}-kb"
  role_arn = aws_iam_role.kb.arn

  knowledge_base_configuration {
    type = "VECTOR"
    vector_knowledge_base_configuration {
      embedding_model_arn = "arn:aws:bedrock:${var.region}::foundation-model/amazon.titan-embed-text-v2:0"
    }
  }

  storage_configuration {
    type = "OPENSEARCH_SERVERLESS"
    opensearch_serverless_configuration {
      collection_arn    = aws_opensearchserverless_collection.kb.arn
      vector_index_name = "bedrock-kb-index"
      field_mapping {
        vector_field   = "bedrock-knowledge-base-default-vector"
        text_field     = "AMAZON_BEDROCK_TEXT_CHUNK"
        metadata_field = "AMAZON_BEDROCK_METADATA"
      }
    }
  }

  depends_on = [
    null_resource.create_os_index,
    aws_opensearchserverless_access_policy.kb,
    aws_opensearchserverless_collection.kb,
    time_sleep.wait_for_collection,
    aws_iam_role_policy.kb_s3,
  ]
}

resource "aws_bedrockagent_data_source" "docs" {
  knowledge_base_id = aws_bedrockagent_knowledge_base.docs.id
  name              = "${local.name}-s3-source"

  data_source_configuration {
    type = "S3"
    s3_configuration {
      bucket_arn = aws_s3_bucket.docs.arn
    }
  }
}

# ── IAM role for Bedrock Agent ──────────────────────────────────────────────
resource "aws_iam_role" "agent" {
  name = "${local.name}-agent-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "bedrock.amazonaws.com" }
      Action    = "sts:AssumeRole"
      Condition = {
        StringEquals = { "aws:SourceAccount" = var.account_id }
      }
    }]
  })
}

resource "aws_iam_role_policy" "agent_bedrock" {
  role = aws_iam_role.agent.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["bedrock:InvokeModel", "bedrock:Retrieve", "bedrock:RetrieveAndGenerate"]
      Resource = "*"
    }]
  })
}

# ── Bedrock Agent ───────────────────────────────────────────────────────────
resource "aws_bedrockagent_agent" "search" {
  agent_name              = "${local.name}-agent"
  agent_resource_role_arn = aws_iam_role.agent.arn
  foundation_model        = "anthropic.claude-3-5-sonnet-20241022-v2:0"
  idle_session_ttl_in_seconds = 600

  instruction = <<-EOT
    You are a knowledgeable assistant that answers questions based on the organization's
    documents and procedures.

    Answering style:
    - Synthesize and summarize information in your own words — do not quote documents verbatim.
    - When procedures or steps are involved, present them clearly in order.
    - If the knowledge base does not contain the answer, say so — never guess or use general knowledge.

    Citations:
    - Always cite your sources at the end using markdown links: [Document Title](URL)
    - Use the source URL provided in the retrieved document metadata.
  EOT
}

resource "aws_bedrockagent_agent_knowledge_base_association" "search" {
  agent_id             = aws_bedrockagent_agent.search.agent_id
  description          = "Manufacturing procedure documents"
  knowledge_base_id    = aws_bedrockagent_knowledge_base.docs.id
  knowledge_base_state = "ENABLED"
}

resource "aws_bedrockagent_agent_alias" "live" {
  agent_alias_name = "live"
  agent_id         = aws_bedrockagent_agent.search.agent_id
  depends_on       = [aws_bedrockagent_agent_knowledge_base_association.search]
}
