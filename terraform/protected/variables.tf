variable "region" {
  description = "AWS region (Bedrock Managed Knowledge Base with ACLs must be available there)"
  type        = string
  default     = "us-east-1"
}

variable "account_id" {
  description = "AWS account ID (bucket naming and IAM conditions)"
  type        = string
}

variable "env_name" {
  description = "Environment name; all protected resources are named <env_name>-protected-*"
  type        = string
  default     = "search-agent"
}

variable "knowledge_base_id" {
  description = "ID of the managed knowledge base created by provision_protected.sh. Empty = stage 1 (bucket, roles, Cognito only)."
  type        = string
  default     = ""
}

variable "data_source_id" {
  description = "ID of the ACL-enabled S3 data source created by provision_protected.sh."
  type        = string
  default     = ""
}

variable "bedrock_model_id" {
  description = "Foundation model that writes the answer from retrieved excerpts. Check https://docs.aws.amazon.com/bedrock/latest/userguide/model-lifecycle.html before pinning."
  type        = string
  default     = "amazon.nova-lite-v1:0"
}

variable "id_token_minutes" {
  description = "Lifetime of Cognito ID tokens (5-1440). Use 5 to exercise expiry in the acceptance gate."
  type        = number
  default     = 60

  validation {
    condition     = var.id_token_minutes >= 5 && var.id_token_minutes <= 1440
    error_message = "id_token_minutes must be between 5 and 1440."
  }
}

variable "throttle_burst" {
  description = "API Gateway burst limit for the protected route"
  type        = number
  default     = 10
}

variable "throttle_rate" {
  description = "API Gateway steady-state requests per second for the protected route"
  type        = number
  default     = 5
}
