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
  agent_name              = "${local.name}-agent"
  agent_resource_role_arn = aws_iam_role.agent.arn
  foundation_model        = "amazon.nova-lite-v1:0"
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
