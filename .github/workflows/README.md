# CI/CD — the governance layer

This is where run history, RBAC, scheduling, and OIDC-to-Palo live (replacing Ansible/Tower).

Pipelines (to build): provisioning (Day-1) and the Day-2 change flow
(compile → classify → plan → risk-tier gate → apply). Use environment protection rules for
the high-risk approval gate and OIDC so no long-lived firewall credentials sit in CI.
