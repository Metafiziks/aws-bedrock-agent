output "docs_bucket" {
  value = aws_s3_bucket.docs.bucket
}

output "collection_endpoint" {
  value = aws_opensearchserverless_collection.kb.collection_endpoint
}

output "knowledge_base_id" {
  value = aws_bedrockagent_knowledge_base.docs.id
}

output "data_source_id" {
  value = aws_bedrockagent_data_source.docs.data_source_id
}

output "agent_id" {
  value = aws_bedrockagent_agent.search.agent_id
}

output "agent_alias_id" {
  value = aws_bedrockagent_agent_alias.live.agent_alias_id
}

output "lambda_url" {
  value = aws_lambda_function_url.invoke.function_url
}

output "github_actions_role_arn" {
  value = aws_iam_role.github_actions.arn
}

output "s3_model_bucket" {
  description = "S3 bucket that stores the IsolationForest model and telemetry exports"
  value       = aws_s3_bucket.docs.bucket
}

output "s3_model_key" {
  description = "S3 key for the trained IsolationForest model"
  value       = "models/iforest.pkl"
}

output "s3_telemetry_prefix" {
  description = "S3 prefix where CloudWatch Firehose writes telemetry JSONL files"
  value       = "telemetry/"
}
