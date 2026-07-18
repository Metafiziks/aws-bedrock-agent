variable "region" {
  description = "AWS region"
  type        = string
  default     = "us-east-1"
}

variable "account_id" {
  description = "AWS account ID (used for resource naming and IAM conditions)"
  type        = string
}

variable "env_name" {
  description = "Environment name — drives all resource names"
  type        = string
  default     = "search-agent"
}

variable "github_repo" {
  description = "GitHub repository in owner/repo format (for OIDC federation)"
  type        = string
  default     = ""
}

variable "alert_email" {
  description = "Email address for budget alert notifications. Leave empty to disable budget alerts."
  type        = string
  default     = ""
}

variable "enable_firehose" {
  description = "Enable CloudWatch → Kinesis Firehose → S3 telemetry export. Requires a Firehose service subscription. Eval runner writes telemetry directly to S3 regardless of this setting."
  type        = bool
  default     = false
}
