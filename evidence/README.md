# evidence — NIST-mapped change evidence

Each change (and each provisioning run) emits a structured bundle linking intent, compiled
config, risk verdict, approver, plan diff, apply result, and timestamps.

Control coverage: AC-4, CM-2, CM-3, CM-5, CM-6, AU-2, AU-12, SC-7. Destination (Git-resident
vs SIEM/GRC/evidence store) is an open question — see design doc.
