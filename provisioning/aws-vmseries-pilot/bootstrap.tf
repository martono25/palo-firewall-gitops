# Bootstrap package in S3. VM-Series reads this on first boot (over mgmt, via the
# instance profile below) to self-configure and phone home to SCM.
#
# Structure the firewall expects: config/ (init-cfg.txt), license/ (authcodes),
# and empty content/ + software/. The init-cfg carries the SCM onboarding keys;
# license/authcodes carries the BYOL auth code.

data "aws_caller_identity" "current" {}

resource "aws_s3_bucket" "bootstrap" {
  bucket        = "${var.project}-bootstrap-${data.aws_caller_identity.current.account_id}"
  force_destroy = true # pilot: allow `terraform destroy` to remove the bucket + contents
  tags          = { Project = var.project }
}

resource "aws_s3_bucket_public_access_block" "bootstrap" {
  bucket                  = aws_s3_bucket.bootstrap.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "bootstrap" {
  bucket = aws_s3_bucket.bootstrap.id
  rule {
    apply_server_side_encryption_by_default { sse_algorithm = "AES256" }
  }
}

# config/init-cfg.txt — SCM onboarding (keys confirmed 2026-07-23).
resource "aws_s3_object" "init_cfg" {
  bucket = aws_s3_bucket.bootstrap.id
  key    = "config/init-cfg.txt"
  content = templatefile("${path.module}/init-cfg.txt.tftpl", {
    hostname   = var.project
    pin_id     = var.scm_registration_pin_id
    pin_value  = var.scm_registration_pin_value
    scm_folder = var.scm_folder
  })
}

# license/authcodes — BYOL activation on first boot.
resource "aws_s3_object" "authcodes" {
  bucket  = aws_s3_bucket.bootstrap.id
  key     = "license/authcodes"
  content = var.vmseries_authcode
}

# Empty folders the bootstrap agent looks for.
resource "aws_s3_object" "content_dir" {
  bucket  = aws_s3_bucket.bootstrap.id
  key     = "content/"
  content = ""
}

resource "aws_s3_object" "software_dir" {
  bucket  = aws_s3_bucket.bootstrap.id
  key     = "software/"
  content = ""
}

# ── IAM: let the instance read the bootstrap bucket ───────────────────────
data "aws_iam_policy_document" "assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "bootstrap" {
  name_prefix        = "${var.project}-bootstrap-"
  assume_role_policy = data.aws_iam_policy_document.assume.json
  tags               = { Project = var.project }
}

data "aws_iam_policy_document" "bootstrap_read" {
  statement {
    actions   = ["s3:GetObject"]
    resources = ["${aws_s3_bucket.bootstrap.arn}/*"]
  }
  statement {
    actions   = ["s3:ListBucket"]
    resources = [aws_s3_bucket.bootstrap.arn]
  }
}

resource "aws_iam_role_policy" "bootstrap_read" {
  name_prefix = "bootstrap-read-"
  role        = aws_iam_role.bootstrap.id
  policy      = data.aws_iam_policy_document.bootstrap_read.json
}

resource "aws_iam_instance_profile" "bootstrap" {
  name_prefix = "${var.project}-bootstrap-"
  role        = aws_iam_role.bootstrap.name
}
