output "ci_role_arn" {
  description = "Set this as the AWS_OIDC_ROLE_ARN repo variable (gh variable set)."
  value       = aws_iam_role.ci.arn
}
