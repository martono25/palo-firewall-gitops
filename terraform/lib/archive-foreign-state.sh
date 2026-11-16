#!/usr/bin/env bash
# Archive a run-once root's LOCAL Terraform state when it does not belong to the
# account we are about to apply in.
#
# WHY THIS IS ITS OWN FILE. The first version lived inline in
# rebootstrap-account.sh and asked "which account does this state name?" by
# grepping for an IAM ARN:
#
#     old=$(grep -oE 'arn:aws:iam::[0-9]{12}' "$f" | head -1 | cut -d: -f5)
#     if [ -n "$old" ] && [ "$old" != "$ACCT" ]; then archive; else
#       ok "$d: state already belongs to $ACCT"      # <- ASSERTED, never checked
#
# `terraform/github-oidc` state holds IAM ARNs, so it archived correctly.
# `terraform/bootstrap-backend` state holds NONE — only a bucket name — so `old`
# was empty, the else branch printed a reassurance nobody had verified, and
# terraform then refreshed the DEAD account's bucket:
#
#     Error: reading S3 Bucket Versioning (fw-gitops-tfstate-975239810400):
#     403 AllAccessDisabled
#
# Reported by the operator mid-move on 2026-11-15.
#
# THE FIX IS THE QUESTION, NOT THE REGEX. Asking "does this state name the
# account I am applying in?" fails closed: state that does not mention the
# current account is archived, whatever it does contain. Archiving is reversible
# (the file is moved, never deleted) and these two roots are cheap to rebuild;
# refreshing another account's resources is neither.

# archive_foreign_state <dir> <current-account-id>
#   prints one line of outcome; returns 0 always (nothing here should stop a move)
archive_foreign_state() {
  # SEPARATE `local` STATEMENTS, deliberately. In one statement bash expands
  # `$dir` BEFORE assigning it, so `local dir="$1" f="$dir/terraform.tfstate"`
  # silently gives f=/terraform.tfstate — which reports "no local state" for
  # every root and skips the archive entirely. Caught by the tests below.
  local dir="$1"
  local acct="$2"
  local f="$dir/terraform.tfstate"
  [ -f "$f" ] || { echo "no local state"; return 0; }
  if grep -q -- "$acct" "$f" 2>/dev/null; then
    echo "state names $acct — kept"
    return 0
  fi
  # Name the archive after whatever account the state DOES name, so the
  # directory stays self-describing; `unknown` when it names none.
  local old
  old=$(grep -oE '[0-9]{12}' "$f" 2>/dev/null | sort | uniq -c | sort -rn | head -1 | awk '{print $2}')
  [ -n "$old" ] || old=unknown
  mkdir -p "$dir/.old-account-$old"
  mv "$dir"/terraform.tfstate* "$dir/.old-account-$old/" 2>/dev/null
  echo "state does NOT name $acct (names $old) — moved to .old-account-$old/ (kept)"
  return 0
}
