# ── Identity: a Cognito user pool the app signs users into. Admin-created users only. ──
resource "aws_cognito_user_pool" "users" {
  name                     = "${local.name}-users"
  username_attributes      = ["email"]
  mfa_configuration        = "OFF"
  deletion_protection      = "INACTIVE"
  auto_verified_attributes = ["email"]

  admin_create_user_config {
    allow_admin_create_user_only = true
  }

  password_policy {
    minimum_length    = 14
    require_lowercase = true
    require_uppercase = true
    require_numbers   = true
    require_symbols   = true
  }

  # Immutable: ACLs match the exact email, so a user must not be able to change it.
  schema {
    name                = "email"
    attribute_data_type = "String"
    required            = true
    mutable             = false
    string_attribute_constraints {
      min_length = 5
      max_length = 254
    }
  }
}

resource "aws_cognito_user_pool_client" "app" {
  name            = "${local.name}-app"
  user_pool_id    = aws_cognito_user_pool.users.id
  generate_secret = false

  # SRP for a real app; admin auth needs IAM credentials and is how the acceptance gate mints tokens.
  explicit_auth_flows           = ["ALLOW_USER_SRP_AUTH", "ALLOW_ADMIN_USER_PASSWORD_AUTH", "ALLOW_REFRESH_TOKEN_AUTH"]
  prevent_user_existence_errors = "ENABLED"
  enable_token_revocation       = true

  id_token_validity      = var.id_token_minutes
  access_token_validity  = 60
  refresh_token_validity = 1
  token_validity_units {
    id_token      = "minutes"
    access_token  = "minutes"
    refresh_token = "days"
  }
}
