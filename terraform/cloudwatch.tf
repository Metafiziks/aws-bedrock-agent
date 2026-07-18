resource "aws_cloudwatch_log_group" "agent" {
  name              = "/aws/lambda/${var.env_name}-search-agent"
  retention_in_days = 90
  tags = { Environment = var.env_name }
}

# S3 prefix for CloudWatch → S3 telemetry exports
resource "aws_s3_object" "telemetry_prefix_placeholder" {
  bucket  = aws_s3_bucket.docs.id
  key     = "telemetry/.keep"
  content = ""
}

# S3 prefix for trained IsolationForest model
resource "aws_s3_object" "models_prefix_placeholder" {
  bucket  = aws_s3_bucket.docs.id
  key     = "models/.keep"
  content = ""
}

# CloudWatch log subscription filter → Kinesis Data Firehose → S3 for telemetry
resource "aws_iam_role" "firehose" {
  name = "${var.env_name}-firehose-role"
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
  name = "${var.env_name}-firehose-s3"
  role = aws_iam_role.firehose.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Action = ["s3:AbortMultipartUpload", "s3:GetBucketLocation", "s3:GetObject",
                "s3:ListBucket", "s3:ListBucketMultipartUploads", "s3:PutObject"]
      Resource = [
        aws_s3_bucket.docs.arn,
        "${aws_s3_bucket.docs.arn}/*"
      ]
    }]
  })
}

resource "aws_iam_role" "cwl_subscription" {
  name = "${var.env_name}-cwl-subscription-role"
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
  name = "${var.env_name}-cwl-subscription-firehose"
  role = aws_iam_role.cwl_subscription.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["firehose:*"]
      Resource = [aws_kinesis_firehose_delivery_stream.telemetry.arn]
    }]
  })
}

resource "aws_kinesis_firehose_delivery_stream" "telemetry" {
  name        = "${var.env_name}-telemetry"
  destination = "extended_s3"

  extended_s3_configuration {
    role_arn            = aws_iam_role.firehose.arn
    bucket_arn          = aws_s3_bucket.docs.arn
    prefix              = "telemetry/year=!{timestamp:yyyy}/month=!{timestamp:MM}/day=!{timestamp:dd}/"
    error_output_prefix = "telemetry-errors/"
    buffering_size      = 1
    buffering_interval  = 60
  }
}

resource "aws_cloudwatch_log_subscription_filter" "telemetry" {
  name            = "${var.env_name}-telemetry-filter"
  log_group_name  = aws_cloudwatch_log_group.agent.name
  filter_pattern  = "TELEMETRY"
  destination_arn = aws_kinesis_firehose_delivery_stream.telemetry.arn
  role_arn        = aws_iam_role.cwl_subscription.arn
  depends_on      = [aws_cloudwatch_log_group.agent]
}
