# ── Backend (stage 2, once the knowledge base exists) ──
# API Gateway validates the user's ID token; the function reads the email only from those claims.
data "archive_file" "lambda" {
  count       = local.backend ? 1 : 0
  type        = "zip"
  source_dir  = "${path.module}/lambda_build"
  output_path = "${path.module}/lambda.zip"
}

resource "aws_cloudwatch_log_group" "lambda" {
  count             = local.backend ? 1 : 0
  name              = "/aws/lambda/${local.name}-ask"
  retention_in_days = 14
}

resource "aws_lambda_function" "ask" {
  count            = local.backend ? 1 : 0
  function_name    = "${local.name}-ask"
  role             = aws_iam_role.lambda.arn
  handler          = "handler.lambda_handler"
  runtime          = "python3.12"
  filename         = data.archive_file.lambda[0].output_path
  source_code_hash = data.archive_file.lambda[0].output_base64sha256
  timeout          = 28
  memory_size      = 256

  environment {
    variables = {
      KNOWLEDGE_BASE_ID = var.knowledge_base_id
      DATA_SOURCE_ID    = var.data_source_id
      DOCS_BUCKET       = aws_s3_bucket.docs.bucket
      BEDROCK_MODEL_ID  = var.bedrock_model_id
      COGNITO_ISSUER    = "https://cognito-idp.${var.region}.amazonaws.com/${aws_cognito_user_pool.users.id}"
      COGNITO_CLIENT_ID = aws_cognito_user_pool_client.app.id
    }
  }

  depends_on = [aws_cloudwatch_log_group.lambda, aws_iam_role_policy.lambda, aws_iam_role_policy_attachment.lambda_logs]
}

resource "aws_apigatewayv2_api" "ask" {
  count         = local.backend ? 1 : 0
  name          = "${local.name}-api"
  protocol_type = "HTTP"
}

resource "aws_apigatewayv2_authorizer" "cognito" {
  count            = local.backend ? 1 : 0
  api_id           = aws_apigatewayv2_api.ask[0].id
  authorizer_type  = "JWT"
  name             = "${local.name}-cognito"
  identity_sources = ["$request.header.Authorization"]
  jwt_configuration {
    audience = [aws_cognito_user_pool_client.app.id]
    issuer   = "https://cognito-idp.${var.region}.amazonaws.com/${aws_cognito_user_pool.users.id}"
  }
}

resource "aws_apigatewayv2_integration" "ask" {
  count                  = local.backend ? 1 : 0
  api_id                 = aws_apigatewayv2_api.ask[0].id
  integration_type       = "AWS_PROXY"
  integration_method     = "POST"
  integration_uri        = aws_lambda_function.ask[0].invoke_arn
  payload_format_version = "2.0"
  timeout_milliseconds   = 29000
}

# Only this one authorized route exists; there is no $default route and no unauthenticated route.
resource "aws_apigatewayv2_route" "ask" {
  count              = local.backend ? 1 : 0
  api_id             = aws_apigatewayv2_api.ask[0].id
  route_key          = "POST /ask"
  target             = "integrations/${aws_apigatewayv2_integration.ask[0].id}"
  authorization_type = "JWT"
  authorizer_id      = aws_apigatewayv2_authorizer.cognito[0].id
}

resource "aws_apigatewayv2_stage" "default" {
  count       = local.backend ? 1 : 0
  api_id      = aws_apigatewayv2_api.ask[0].id
  name        = "$default"
  auto_deploy = true

  default_route_settings {
    throttling_burst_limit = var.throttle_burst
    throttling_rate_limit  = var.throttle_rate
  }
}

resource "aws_lambda_permission" "apigw" {
  count         = local.backend ? 1 : 0
  statement_id  = "AllowProtectedApiInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.ask[0].function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.ask[0].execution_arn}/*/*"
}
