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

- `terraform/` — VM instance + mgmt networking + bootstrap storage; SCM folders + snippet stacks (`scm` provider)
- `bootstrap/` — `init-cfg.txt` / minimal `bootstrap.xml` templates (ZTP handshake)
- `python/` — auth-key gen, licensing activation, content updates, registration glue,
  "wait until connected" verification, and any SCM object the `scm` provider doesn't cover

**Sequencing:** build the spine on VM-Series first (disposable, loop-in-code), then add the
PA-series ZTP front door. Idempotency: re-runs must not re-bootstrap — onboard/baseline state
is a runtime SCM query, not TF state. Evidence: emits CM-2 (baseline) + CM-6 (settings).
