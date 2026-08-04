# Ordering on EXISTING rules — RUN, and the answer is DO NOT WIRE IT (2026-08-04)

`spike/beta4-ordering` answered *"create a rule in position"*: `top`, `bottom`
and `before`+`target_rule` all land correctly on CREATE. This probe answers the
question that actually gates wiring ordering into the module — **what does
`relative_position` do to rules that already exist?** — because the compiler
defaults every rule to `bottom`.

## Result: a first-time add RE-STACKS the rulebase, unpredictably

| phase | rulebase before | action | rulebase after |
|---|---|---|---|
| 1 | — | create, no `relative_position` | `charlie, bravo, alpha` |
| 2 | `charlie, bravo, alpha` | **add** `relative_position = "bottom"` to all three | **`alpha, charlie, bravo`** |

The order changed, and it is **not** the for_each order either — alphabetical
would give `alpha, bravo, charlie`. Each rule's "move to bottom" is applied in
whatever order Terraform happens to process the map, which is not a guaranteed
stable ordering. Two runs need not agree.

**Rule order is policy.** A permissive rule landing above a deny is a different
firewall, and nothing in the plan says so — the diff reads only:

```
+ relative_position = "bottom"
```

## Two supporting findings

**A no-change value is a no-op.** Re-applying `bottom` when state already says
`bottom` produces `No changes`; Terraform does not act, so the rulebase is left
alone. The danger is confined to the transition (`null -> "bottom"`, or any
value change).

**Terraform cannot see ordering drift.** `relative_position` is a create/update
*instruction*, not a stored property of the rule. A rule moved out-of-band —
here, `charlie` promoted to top via the REST move endpoint — produced
`No changes` on the next plan. So Terraform will neither detect nor correct
someone reordering the rulebase by hand.

**Changing the value DOES move the rule.** `bottom -> top` on an existing rule
moved it from last to first, cleanly, no warning. So the mechanism works; it is
the blanket default that is unsafe.

## Consequence

`position` / `relative_position` stay **unwired** in
`terraform/modules/security_folder/main.tf`. Ordering remains with
`fwgitops enrich`, which applies moves in one deliberate, sorted pass rather than
as a side effect of every rule's update.

Wiring them would need the compiler to emit `relative_position` **only when the
intent explicitly asked for a position** — and the intent model cannot express
that today, because `position` defaults to `bottom`, so "unspecified" and
"deliberately bottom" are the same value. That distinction has to exist first.

## Re-running

```bash
set -a; source ~/.fwgitops/scm.env; set +a
terraform -chdir=spike/ordering-existing init
terraform -chdir=spike/ordering-existing apply -var 'ordering_mode=none'
python3 spike/ordering-existing/order.py          # creation order
# reorder out-of-band so rulebase order != for_each order, then:
terraform -chdir=spike/ordering-existing apply -var 'ordering_mode=all_bottom'
python3 spike/ordering-existing/order.py          # re-stacked
terraform -chdir=spike/ordering-existing destroy -auto-approve
```

`GitOps` only — zero devices, nothing inherits from it, never pushed.
