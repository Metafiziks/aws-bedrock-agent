output "docs_bucket" {
  value = aws_s3_bucket.docs.bucket
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
