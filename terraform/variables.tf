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
}

variable "alert_email" {
  description = "Email for budget alerts"
  type        = string
}
