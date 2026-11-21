output "docs_bucket" {
  value = aws_s3_bucket.docs.bucket
}

output "kb_role_arn" {
  value = aws_iam_role.kb.arn
}

output "lambda_role_arn" {
  value = aws_iam_role.lambda.arn
}

output "user_pool_id" {
  value = aws_cognito_user_pool.users.id
}

output "user_pool_client_id" {
  value = aws_cognito_user_pool_client.app.id
}

output "api_url" {
  value = local.backend ? aws_apigatewayv2_api.ask[0].api_endpoint : ""
}

output "function_name" {
  value = local.backend ? aws_lambda_function.ask[0].function_name : ""
}
