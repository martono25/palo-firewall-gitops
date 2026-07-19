# provisioning — Day-1 firewall bring-up + SCM onboarding

Gets a firewall from "boxed or booted" to "SCM-managed and enforcing a baseline," ideally
zero-touch. See `docs/DESIGN.md` → "Day-1 Provisioning & SCM Onboarding".

**Single management plane: Strata Cloud Manager (SCM).** Two form factors, two front doors,
one shared onboard+baseline spine:

- **VM-Series (cloud):** Terraform instantiates + bootstrap package → auto phone-home to SCM.
- **PA-series (hardware):** manual rack/cable → ZTP → claim into SCM tenant (serial pre-registered).

Shared spine: SCM onboard (device auth-key → folder/label) → baseline snippet stack.

Steps: instantiate → phone-home/bootstrap → license/subscriptions (retry loop) → SCM onboard
→ baseline snippet stack (start from Iron-Skillet / BPA) → content + PAN-OS floor.

## What's built vs scaffolded

- **Orchestration (BUILT + TESTED):** `src/fwgitops/provision.py` — the re-entrant state
  machine (resume from any stage), the license retry loop (the flaky step), and the bounded
  connect poll (T3). 8 tests with a fake SCM client. The **real SCM client** behind the
  `ProvisionClient` protocol is the unvalidated part (built during the SCM spike).
- **`bootstrap/` (SCAFFOLD):** `init-cfg.sample.txt` — the ZTP phone-home template. SCM
  onboarding keys marked VERIFY (rendered files with auth material are gitignored).
- **`terraform/aws|gcp/` (POINTERS, not blind HCL):** do NOT hand-roll VM-Series cloud
  modules. Use Palo's maintained reference modules:
  - AWS: `PaloAltoNetworks/terraform-aws-vmseries-modules`
  - GCP: `PaloAltoNetworks/terraform-google-vmseries-modules`
  Wire their bootstrap input to `bootstrap/init-cfg`, then hand the running device to
  `fwgitops.provision`.

**Sequencing:** build the spine on VM-Series first (disposable, loop-in-code), then add the
PA-series ZTP front door. Idempotency: re-runs must not re-bootstrap — onboard/baseline state
is a runtime SCM query (see `provision.current_stage`), not TF state. Evidence: emits CM-2
(baseline) + CM-6 (settings).
