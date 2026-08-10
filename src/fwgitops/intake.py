"""Issue Form -> intent YAML. The broad-requester intake.

WHY THIS IS PYTHON AND NOT WORKFLOW SHELL. The parsing is where the bugs live: a
GitHub Issue Form arrives as RENDERED MARKDOWN, not structured data, and every
field has to survive that round trip. Doing it in a workflow makes it untestable
and unrunnable locally, which is how `.github/ISSUE_TEMPLATE/` came to be
documented as existing for three weeks while being empty.

WHAT AN ISSUE BODY LOOKS LIKE. GitHub renders each field as its LABEL followed by
the value:

    ### Change ticket

    JIRA-12345

    ### Source

    10.20.1.0/24
    10.20.2.0/24

So the parser keys on the label text. That is a coupling between this file and
`rule-request.yml`, and a test asserts the two agree — a renamed label would
otherwise silently produce an empty field.

FAIL CLOSED, AND EXPLAIN. A requester cannot read a stack trace or a compiler
error about `spec.service[0].protocol`. Every rejection here names the FORM FIELD
they filled in and what to write instead, because the alternative is a broken PR
they cannot fix.

WHAT THIS DELIBERATELY CANNOT DO. Only `AccessRequest`. Zones, interfaces and
routes are platform work with device-level consequences measured in ADR-0008 —
a form that let anyone request a default route would be a form that lets anyone
black-hole the estate.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Dict, List, Optional, Tuple

#: Form label -> intent field. The LABELS are the contract with
#: `.github/ISSUE_TEMPLATE/rule-request.yml`; a test asserts they still match.
FIELD_LABELS = {
    "Change ticket": "ticket",
    "Why do you need this?": "justification",
    "Environment": "environment",
    "Allow or deny?": "action",
    "Team or app name": "team",
    "Source": "source",
    "Destination": "destination",
    "Service": "service",
}

#: What GitHub writes when an optional field is left blank.
_NO_RESPONSE = "_no response_"


class IntakeError(Exception):
    """The form could not be turned into an intent. Messages are for a REQUESTER."""

    def __init__(self, problems: List[str]):
        self.problems = problems
        super().__init__("; ".join(problems))


def parse_form(body: str) -> Dict[str, str]:
    """`### Label` sections of a rendered Issue Form -> {field: raw value}."""
    out: Dict[str, str] = {}
    # Split on headings, keeping the heading text. `###` is what Issue Forms
    # render; anything deeper is the requester's own markdown and not ours.
    parts = re.split(r"^###\s+(.+?)\s*$", body or "", flags=re.MULTILINE)
    for i in range(1, len(parts) - 1, 2):
        label, value = parts[i].strip(), parts[i + 1].strip()
        key = FIELD_LABELS.get(label)
        if key and value.lower() != _NO_RESPONSE:
            out[key] = value
    return out


def _endpoints(raw: str, field_name: str, problems: List[str]) -> List[dict]:
    """One endpoint per line. `cidr` is inferred; `app:`/`fqdn:` are explicit."""
    out: List[dict] = []
    for line in [l.strip() for l in raw.splitlines() if l.strip()]:
        low = line.lower()
        if low.startswith("app:"):
            out.append({"app": line.split(":", 1)[1].strip()})
        elif low.startswith("fqdn:"):
            out.append({"fqdn": line.split(":", 1)[1].strip()})
        elif "/" in line and not low.startswith(("http", "tcp", "udp")):
            out.append({"cidr": line})
        elif re.fullmatch(r"[0-9.]+", line):
            # A bare host. Say what to write rather than guessing a mask —
            # guessing /24 here would silently widen the rule.
            problems.append(
                f"{field_name}: {line!r} has no prefix length. Write `{line}/32` for a "
                f"single host, or the network it belongs to, e.g. `{line.rsplit('.',1)[0]}.0/24`.")
        else:
            problems.append(
                f"{field_name}: cannot read {line!r}. Use a CIDR like `10.20.1.0/24`, "
                f"`fqdn: name.internal`, or `app: some-app` from the catalog.")
    if not out and not problems:
        problems.append(f"{field_name}: is empty.")
    return out


def _services(raw: str, problems: List[str]) -> List[dict]:
    """`tcp/443`, `udp/53`, `tcp/8000-8100`, `icmp`, or a catalog name."""
    out: List[dict] = []
    for line in [l.strip() for l in raw.splitlines() if l.strip()]:
        low = line.lower()
        if low in ("icmp", "ping"):
            # ICMP has no ports (spike/icmp-service-shape). A port written
            # alongside is rejected by the loader, so it is rejected here too,
            # with a message a requester can act on.
            out.append({"protocol": "icmp"})
        elif "/" in low:
            proto, _, port = low.partition("/")
            if proto not in ("tcp", "udp"):
                problems.append(
                    f"Service: {line!r} — protocol must be `tcp`, `udp`, or `icmp` "
                    f"(ICMP is written on its own, with no port).")
            elif not port:
                problems.append(f"Service: {line!r} is missing a port, e.g. `{proto}/443`.")
            else:
                out.append({"protocol": proto, "port": port})
        elif re.fullmatch(r"[a-z0-9][a-z0-9._-]*", low):
            out.append({"name": line})          # resolved via catalog/services.yaml
        else:
            problems.append(
                f"Service: cannot read {line!r}. Use `tcp/443`, `udp/53`, "
                f"`tcp/8000-8100`, `icmp`, or a name from catalog/services.yaml.")
    if not out and not problems:
        problems.append("Service: is empty.")
    return out


def to_yaml(intake: "Intake", *, issue_number: int, repo: Optional[str] = None) -> str:
    """The intent file, with a header saying where it came from.

    A generated file that does not say it is generated invites someone to edit it
    and wonder why the next intake run disagrees. The issue link matters more:
    `metadata.justification` is one line, and the conversation that produced the
    request is the rest of the story.
    """
    import yaml as _yaml

    link = (f"https://github.com/{repo}/issues/{issue_number}" if repo
            else f"issue #{issue_number}")
    header = (
        f"# GENERATED by `fwgitops from-issue` from {link}\n"
        f"#\n"
        f"# Edit it like any other intent — it is a normal request file from here on.\n"
        f"# Changing `spec` needs a NEW `metadata.ticket`; the pipeline rejects a\n"
        f"# modified rule that still carries the ticket which authorised the old one.\n"
    )
    return header + _yaml.safe_dump(intake.doc, sort_keys=False, default_flow_style=False)


@dataclass(frozen=True)
class Intake:
    """A generated request, and where it belongs."""

    doc: Dict[str, Any]
    path: str
    req_id: str


def build_intent(body: str, *, issue_number: int, author: str,
                 today: Optional[date] = None) -> Intake:
    """Issue body -> an intent document plus the path to write it to.

    `req_id` is `REQ-<year>-<issue number>`: unique by construction, and it
    traces the rule on the firewall back to the conversation that asked for it
    without anyone allocating an id by hand.

    `requester` is the ISSUE AUTHOR, not a form field. A requester field someone
    types is a field someone can type wrongly, and the whole audit chain hangs
    off knowing who actually asked.
    """
    today = today or date.today()
    fields = parse_form(body)
    problems: List[str] = []

    for required in ("ticket", "justification", "environment", "action", "team",
                     "source", "destination", "service"):
        if not fields.get(required):
            label = next(k for k, v in FIELD_LABELS.items() if v == required)
            problems.append(f"{label}: is required and was left blank.")

    source = _endpoints(fields.get("source", ""), "Source", problems)
    destination = _endpoints(fields.get("destination", ""), "Destination", problems)
    service = _services(fields.get("service", ""), problems)

    team = (fields.get("team") or "").strip().lower()
    if team and not re.fullmatch(r"[a-z0-9][a-z0-9-]*", team):
        problems.append(
            f"Team or app name: {team!r} — use lowercase letters, digits and hyphens, "
            f"e.g. `payments`. It only decides which folder the request file lands in.")

    if problems:
        raise IntakeError(problems)

    req_id = f"REQ-{today.year}-{issue_number}"
    env = fields["environment"].strip()
    doc = {
        "apiVersion": "fw-intent/v1",
        "kind": "AccessRequest",
        "metadata": {
            "id": req_id,
            "requester": author,
            "ticket": fields["ticket"].strip(),
            "justification": fields["justification"].strip().replace("\n", " "),
            "requested": today.isoformat(),
        },
        "spec": {
            "environment": env,
            "action": fields["action"].strip(),
            "source": source,
            "destination": destination,
            "service": service,
            "log": True,
        },
    }
    return Intake(doc=doc, path=f"intent/{env}/{team}/{req_id}.yaml", req_id=req_id)
