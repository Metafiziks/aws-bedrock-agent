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

variable "enable_agent_memory" {
  description = "Enable Bedrock Agents Classic SESSION_SUMMARY memory. When false, Lambda omits memoryId/endSession and existing stateless behavior is preserved."
  type        = bool
  default     = false
}

variable "memory_retention_days" {
  description = "Number of days Bedrock Agents Classic retains session-summary memory. Valid only when enable_agent_memory is true."
  type        = number
  default     = 30

  validation {
    condition     = var.memory_retention_days >= 1 && var.memory_retention_days <= 365
    error_message = "memory_retention_days must be between 1 and 365."
  }
}

variable "memory_default_id_mode" {
  description = "How Lambda derives memoryId when the request omits memoryId: explicit requires memoryId, user derives from userId, session derives from sessionId."
  type        = string
  default     = "explicit"

  validation {
    condition     = contains(["explicit", "user", "session"], var.memory_default_id_mode)
    error_message = "memory_default_id_mode must be one of: explicit, user, session."
  }
}
