# Part-B smoke test — RETIRED 2026-08-16

**Do not run this. There is nothing left to run.** The Terraform was removed;
this file is the record of what it proved and why it stopped being runnable.

## What it was for

The first proof that the `scm` provider could create real objects in a real
tenant, by calling the **actual** `security_folder` module rather than a
parallel config. It created one address, one service and one **disabled**
`any -> any` rule in the `GitOps` sandbox — disabled deliberately, so that even
if a firewall had been attached it could not pass traffic.

It did its job: the provider worked, the module worked, and every pipeline since
has rested on that.

## Why it was retired

**It stopped compiling against the module it exists to test.** ADR-0010 moved
address and service objects out of Terraform entirely — they are created by
`fwgitops objects ensure` before the apply and swept after the push, because
Terraform ran an object DESTROY before the rule UPDATE that released it and
409'd. The module no longer declares `address_objects` or `service_objects`, so
this config fails at plan with "argument not expected".

It could not be repaired in place. A self-contained module smoke test cannot
create its own objects any more; it would have to run `objects ensure` first,
at which point it is reimplementing the apply workflow, which runs against the
real tenant several times a day and is a far better smoke test than this ever
was.

## What it left behind, and how that was found

Its objects outlived it in SCM — `spike-test-rule`, `spike-test-addr`,
`spike-test-svc` — for roughly four weeks. Nothing noticed until the tag-based
drift engine was wired up on 2026-08-15 and immediately reported:

```
malformed GitOps/spike-test-rule
```

**malformed**, not unmanaged: the spike hardcoded `gitops:managed` without a
`gitops:req` tag, so the rule claimed this platform's provenance while being
traceable to no request. That is exactly the case the classification is meant to
fail closed on.

The three objects were deleted on 2026-08-16 via `delete-scm-object.yml`.

## The lesson worth keeping

A probe that creates objects in a live tenant needs a teardown that runs even
when the probe fails, and something that notices if it does not. This one had
neither, and the gap was invisible for a month because the check that would have
seen it was not wired up.

`spike/tag-destroy-ordering` hit the same problem and was fixed for it in
`fix(spike): the tag-ordering probe left an orphan when it failed (#113)`.
