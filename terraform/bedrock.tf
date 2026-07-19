# ── IAM role for Bedrock Knowledge Base ────────────────────────────────────
resource "aws_iam_role" "kb" {
  name = "${local.name}-kb-role"

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
  agent_name                  = "${local.name}-agent"
  agent_resource_role_arn     = aws_iam_role.agent.arn
  foundation_model            = "amazon.nova-lite-v1:0"
  idle_session_ttl_in_seconds = 600

  instruction = <<-EOT
    You are a manufacturing documentation assistant. You answer questions ONLY
    using either:
    - information retrieved from the organization's procedure documents, or
    - the user's scoped conversation memory when the user asks about their own
      previously stated preferences, context, or conversation history.

    Memory rules:
    - Treat memory as continuity only. Never use memory as the source of truth
      for manufacturing procedures, safety requirements, quality standards, or
      troubleshooting facts.
    - For document/procedure questions, the Knowledge Base remains the source
      of truth and citations are required.
    - For user preference or prior-context questions, answer from session or
      memory context and do not invent citations.

    STRICT RULES — follow these without exception:
    1. If the question is about manufacturing documentation and the knowledge
       base returns no relevant documents, respond with exactly:
       "I couldn't find information about that in the available documentation.
       Please check with your supervisor or try rephrasing your question with
       more specific terms (e.g. include the equipment name)."
    2. Never use general knowledge, training data, or assumptions to fill gaps.
    3. Never ask the user clarifying questions. Either answer from retrieved
       documents, or return the not-found message above. Do not ask for more
       details before attempting to answer.
    4. Never speculate or suggest what the answer "might" be.
    5. Never say you need more information to answer — if retrieval returns
       nothing, use the not-found message.

    Answering style (when documents are retrieved):
    - Synthesize information in your own words — do not quote documents verbatim.
    - For procedures or steps, present them clearly in order.
    - Always cite your sources at the end: [filename.txt](URL)
  EOT

  dynamic "memory_configuration" {
    for_each = var.enable_agent_memory ? [1] : []
    content {
      enabled_memory_types = ["SESSION_SUMMARY"]
      storage_days         = var.memory_retention_days
    }
  }
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
