resource "aws_cloudwatch_log_group" "agent" {
  name              = "/aws/lambda/${var.env_name}-search-agent"
  retention_in_days = 90
  tags              = { Environment = var.env_name }
}

# S3 prefix placeholders — always created so the eval runner can write telemetry directly
resource "aws_s3_object" "telemetry_prefix_placeholder" {
  bucket  = aws_s3_bucket.docs.id
  key     = "telemetry/.keep"
  content = ""
}

resource "aws_s3_object" "models_prefix_placeholder" {
  bucket  = aws_s3_bucket.docs.id
  key     = "models/.keep"
  content = ""
}

# ── Kinesis Firehose (optional) ──────────────────────────────────────────────
# Requires a Firehose service subscription. Disabled by default.
# Enable with: -var=enable_firehose=true
# When disabled, production runtime telemetry is logged to CloudWatch only.
# The eval runner writes directly to S3, so IsolationForest training still works.

resource "aws_iam_role" "firehose" {
  count = var.enable_firehose ? 1 : 0
  name  = "${var.env_name}-firehose-role"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "firehose.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "firehose_s3" {
  count = var.enable_firehose ? 1 : 0
  name  = "${var.env_name}-firehose-s3"
  role  = aws_iam_role.firehose[0].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Action = ["s3:AbortMultipartUpload", "s3:GetBucketLocation", "s3:GetObject",
      "s3:ListBucket", "s3:ListBucketMultipartUploads", "s3:PutObject"]
      Resource = [aws_s3_bucket.docs.arn, "${aws_s3_bucket.docs.arn}/*"]
    }]
  })
}

resource "aws_iam_role" "cwl_subscription" {
  count = var.enable_firehose ? 1 : 0
  name  = "${var.env_name}-cwl-subscription-role"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "logs.${var.region}.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "cwl_subscription_firehose" {
  count = var.enable_firehose ? 1 : 0
  name  = "${var.env_name}-cwl-subscription-firehose"
  role  = aws_iam_role.cwl_subscription[0].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["firehose:*"]
      Resource = [aws_kinesis_firehose_delivery_stream.telemetry[0].arn]
    }]
  })
}

resource "aws_kinesis_firehose_delivery_stream" "telemetry" {
  count       = var.enable_firehose ? 1 : 0
  name        = "${var.env_name}-telemetry"
  destination = "extended_s3"

  extended_s3_configuration {
    role_arn            = aws_iam_role.firehose[0].arn
    bucket_arn          = aws_s3_bucket.docs.arn
    prefix              = "telemetry/year=!{timestamp:yyyy}/month=!{timestamp:MM}/day=!{timestamp:dd}/"
    error_output_prefix = "telemetry-errors/"
    buffering_size      = 1
    buffering_interval  = 60
  }
}

resource "aws_cloudwatch_log_subscription_filter" "telemetry" {
  count           = var.enable_firehose ? 1 : 0
  name            = "${var.env_name}-telemetry-filter"
  log_group_name  = aws_cloudwatch_log_group.agent.name
  filter_pattern  = "TELEMETRY"
  destination_arn = aws_kinesis_firehose_delivery_stream.telemetry[0].arn
  role_arn        = aws_iam_role.cwl_subscription[0].arn
  depends_on      = [aws_cloudwatch_log_group.agent]
}
