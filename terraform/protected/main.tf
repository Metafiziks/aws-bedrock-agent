# Protected mode (KB_ACCESS_MODE=protected): a separate Terraform root with its own state.
# The sample deployment in ../ is untouched. The managed knowledge base and its data source are
# created by scripts/provision_protected.sh (AWS CLI) and passed in as variables; see
# guides/protected-mode.md.
terraform {
  required_version = ">= 1.5"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 5.0"
    }
    archive = {
      source  = "hashicorp/archive"
      version = ">= 2.0"
    }
  }
}

provider "aws" {
  region = var.region
  default_tags {
    tags = {
      "created-by" = "aws-bedrock-agent-protected"
      "env"        = var.env_name
    }
  }
}

locals {
  name    = "${var.env_name}-protected"
  backend = var.knowledge_base_id != "" && var.data_source_id != ""
  kb_arn  = "arn:aws:bedrock:${var.region}:${var.account_id}:knowledge-base/${var.knowledge_base_id}"
}

# ── Private document bucket: no public access, no ACLs, TLS only, no website, no keys ──
resource "aws_s3_bucket" "docs" {
  bucket        = "${var.account_id}-${local.name}-docs"
  force_destroy = true
}

resource "aws_s3_bucket_public_access_block" "docs" {
  bucket                  = aws_s3_bucket.docs.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_ownership_controls" "docs" {
  bucket = aws_s3_bucket.docs.id
  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "docs" {
  bucket = aws_s3_bucket.docs.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_policy" "docs_tls_only" {
  bucket     = aws_s3_bucket.docs.id
  depends_on = [aws_s3_bucket_public_access_block.docs]
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "DenyInsecureTransport"
      Effect    = "Deny"
      Principal = "*"
      Action    = "s3:*"
      Resource  = [aws_s3_bucket.docs.arn, "${aws_s3_bucket.docs.arn}/*"]
      Condition = { Bool = { "aws:SecureTransport" = "false" } }
    }]
  })
}
