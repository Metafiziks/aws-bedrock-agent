# Lambda function — HTTP invoke endpoint for the Bedrock Agent
# Earns the "Create a web app using AWS Lambda" $20 credit activity

resource "aws_iam_role" "lambda" {
  name = "${local.name}-lambda-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy_attachment" "lambda_basic" {
  role       = aws_iam_role.lambda.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

resource "aws_iam_role_policy" "lambda_bedrock" {
  role = aws_iam_role.lambda.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["bedrock:InvokeAgent"]
      Resource = "arn:aws:bedrock:${var.region}:${var.account_id}:agent-alias/${aws_bedrockagent_agent.search.agent_id}/*"
    }]
  })
}

data "archive_file" "lambda" {
  type        = "zip"
  source_file = "${path.module}/../src/lambda/handler.py"
  output_path = "${path.module}/lambda.zip"
}

resource "aws_lambda_function" "invoke" {
  function_name    = "${local.name}-invoke"
  role             = aws_iam_role.lambda.arn
  handler          = "handler.lambda_handler"
  runtime          = "python3.12"
  filename         = data.archive_file.lambda.output_path
  source_code_hash = data.archive_file.lambda.output_base64sha256
  timeout          = 120

  environment {
    variables = {
      AGENT_ID       = aws_bedrockagent_agent.search.agent_id
      AGENT_ALIAS_ID = "TSTALIASID"
    }
  }
}

resource "aws_lambda_function_url" "invoke" {
  function_name      = aws_lambda_function.invoke.function_name
  authorization_type = "NONE"

  cors {
    allow_origins = ["*"]
    allow_methods = ["POST"]
    allow_headers = ["Content-Type"]
  }
}
