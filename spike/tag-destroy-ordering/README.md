# Tag-destroy ordering — REPRODUCTION READY (first attempt FAILED on a probe bug, fixed)

**Does Terraform order "UPDATE the rule to drop a tag" before "DESTROY that tag"?**

`TODOS.md` says it does not, and that the destroy runs first and hits
`409 NON_ZERO_REFS`. That has been the basis for calling this unfixable
declaratively. It deserves a measurement before anyone designs around it.

## Why the claim is in doubt

It rests on one observation during a migration on 2026-08-05. The stated
mechanism — *"Terraform has no reason to order them, because after the change the
rule's config no longer REFERENCES the tag"* — is an inference, not something
that was tested.

**On 2026-08-09 the opposite was measured.** Retiring `REQ-2026-07302` destroyed
a rule and its `gitops:req:` tag in one apply, and Terraform ordered it
correctly:

```
scm_security_rule.this["REQ-2026-07302"]: Destruction complete after 3s
scm_tag.this["gitops:req:REQ-2026-07302"]: Destruction complete after 0s
```

No 409. That does not contradict the original report — destroying a rule keeps
the dependency edge that an in-place UPDATE dissolves — but it does narrow the
bug considerably. If it is real, it fires on a **tag VALUE change**: a corrected
ticket number, where the rule is updated in place and the old tag object is
destroyed in the same apply.

That is a smaller and more precisely stated problem than TODOS describes, and it
changes what a fix has to cover.

## Why this is a script and not a result

`terraform apply` writes to the live tenant, so it is gated. The reproduction is
packaged here rather than guessed at.

**It cannot be reproduced offline.** The failure requires a resource that updates
IN PLACE while another is destroyed; `null_resource`, `terraform_data` and
`local_file` all REPLACE on any change, which is the case that already works. An
offline analogue would prove the wrong thing.

## Running it

```sh
set -a; source ~/.fwgitops/scm.env; set +a
./spike/tag-destroy-ordering/run.sh
```

Prints `REPRODUCED`, `NOT REPRODUCED`, or `FAILED for another reason` — the last
being a real outcome, not a script bug: a failure for an unrelated reason must
not be read as confirmation.

## Safety

* **Never pushes.** Everything stays in the SCM candidate; nothing reaches a
  firewall.
* **The probe rule is `deny` on TEST-NET-1** (`192.0.2.0/24`, RFC 5737
  documentation space), so even if it were somehow committed it matches no real
  traffic and grants nothing.
* **Local state in a scratch directory** — the real `prod-edge` state is never
  opened, so a failed run cannot corrupt it.
* **Cleans up on the way out, INCLUDING FAILURE** (`trap cleanup EXIT`).

## First attempt failed — two bugs in this probe, not in the thing being probed

Run on 2026-08-09, phase 1:

```
Error: 400  OBJECT_ALREADY_EXISTS
  .../container/entry[@name='prod-edge']/tag/entry[@name='gitops:managed']
```

**1. The probe shared a tag with live objects.** Its address and service objects
carried `gitops:managed`, which already exists in `prod-edge` from the real
pipeline. The scratch root has EMPTY state, so Terraform tried to create it and
SCM refused. Every tag here is now owned by this probe alone
(`gitops:ticket:PROBE-*`).

**2. Cleanup ran only on success.** `set -euo pipefail` aborted phase 1 AFTER
`gitops:ticket:PROBE-AAAA` had been created, so `terraform destroy` never ran and
the tag was left orphaned in the live candidate (removed by hand afterwards).

A probe that can only tidy up when it succeeds has it exactly backwards: failure
is when objects get left behind. Now a `trap`, so it runs on every exit path.

Neither bug says anything about the question being probed — phase 1 never
completed, so nothing was learned about ordering.

## What the answer changes

**If REPRODUCED** — the fix must break the coupling, and the honest options are
narrow: stop letting Terraform destroy tag objects at all (they are inert when
unreferenced) and sweep them separately, which is what TODOS proposes. That moves
tag lifecycle partly out of Terraform and needs drift to know unreferenced
`gitops:` tags are expected.

**If NOT REPRODUCED** — the 2026-08-05 failure had a different cause, TODOS is
wrong about the mechanism, and the item shrinks to "re-measure and rewrite".
Designing the sweep before knowing which is the shape of the plan_rc mistake
made on 2026-08-09: a fix for a mechanism nobody had confirmed.
