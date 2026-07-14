# OpenSearch Serverless — vector store for Knowledge Base embeddings
resource "aws_opensearchserverless_security_policy" "encryption" {
  name   = "${local.name}-enc"
  type   = "encryption"
  policy = jsonencode({
    Rules = [{ Resource = ["collection/${local.name}-kb"], ResourceType = "collection" }]
    AWSOwnedKey = true
  })
}

resource "aws_opensearchserverless_security_policy" "network" {
  name   = "${local.name}-net"
  type   = "network"
  policy = jsonencode([{
    Rules = [
      { Resource = ["collection/${local.name}-kb"], ResourceType = "collection" },
      { Resource = ["dashboard/${local.name}-kb"], ResourceType = "dashboard" }
    ]
    AllowFromPublic = true
  }])
}

resource "aws_opensearchserverless_access_policy" "kb" {
  name   = "${local.name}-access"
  type   = "data"
  policy = jsonencode([{
    Rules = [{
      Resource     = ["collection/${local.name}-kb", "index/${local.name}-kb/*"]
      Permission   = ["aoss:*"]
      ResourceType = "collection"
    }, {
      Resource     = ["index/${local.name}-kb/*"]
      Permission   = ["aoss:*"]
      ResourceType = "index"
    }]
    Principal = [
      aws_iam_role.kb.arn,
      "arn:aws:iam::${var.account_id}:root"
    ]
  }])
}

resource "aws_opensearchserverless_collection" "kb" {
  name = "${local.name}-kb"
  type = "VECTORSEARCH"

  depends_on = [
    aws_opensearchserverless_security_policy.encryption,
    aws_opensearchserverless_security_policy.network,
  ]
}
