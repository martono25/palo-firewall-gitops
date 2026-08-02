"""The compiler → Terraform data contract, checked in Python.

The compiler emits `*.auto.tfvars.json`; a Terraform root module is supposed to
consume it. Nothing enforced that, and there are THREE ways it silently fails:

    intent -> compile -> *.auto.tfvars.json
                              |
                    HOLE 1: variables.tf declares no such variable
                            -> Terraform WARNS and exits 0. Ignored.
                              |
                    HOLE 2: variable declared but never referenced
                            -> NO diagnostic at all. Ignored.
                              |
                    HOLE 3: object TYPE omits an emitted attribute
                            -> Terraform DISCARDS it silently. Ignored.
                              |
                         module -> resource -> SCM -> device

Hole 1 shipped a whole release: `zones.auto.tfvars.json` was written on every
compile, plan and apply stayed green, and the zone never reached the firewall.
Hole 2 is quieter still — Terraform says nothing whatsoever. Hole 3 was ALSO
live in v1.0 and is the sneakiest: the key is declared and wired, so a
key-level check reports green, while `application`, `profile_group` and
`log_setting` were dropped at the root boundary and rules were built with
`["any"]` instead of the intent's App-ID.

All three are cheap to check without a Terraform binary or cloud credentials, so
they are checked here: at compile time (fail-closed, see `cli.py`) and in the
test suite (`tests/test_tfcontract.py`).

Deliberately a small regex parser rather than a full HCL parser: the inputs are
this repo's own hand-written root modules, and adding an HCL dependency to catch
a malformed file we would notice anyway is not worth it. Two things make that
safe:

  * String literals are MASKED before anything else. Without this, a `}` inside
    a string collapsed the brace depth (so nested keys looked like top-level
    module arguments, defeating HOLE 2), and a `//` inside a URL was treated as
    a comment (eating the rest of the line, including a closing brace, and
    falsely rejecting a correctly wired module). Both were real, both are now
    regression-tested.
  * The HOLE 2 check asks "is `var.<key>` referenced anywhere in this root?"
    rather than "is there a module argument with this exact name?". That is the
    property we actually care about — the value reaching something — and it
    holds regardless of argument naming or whether the root uses a module block
    or a bare resource.

Known limitation: heredocs (`<<EOT ... EOT`) are not parsed. None of this repo's
root modules use them; a root that did could mis-count braces.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Iterable, List, Optional, Set

#: `variable "name" {`
_VARIABLE_RE = re.compile(r'^\s*variable\s+"([^"]+)"\s*\{', re.MULTILINE)
#: `module "name" {`
_MODULE_RE = re.compile(r'^\s*module\s+"([^"]+)"\s*\{', re.MULTILINE)
#: `name = ...` at the start of a line (a module-block argument)
_ARG_RE = re.compile(r'^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=', re.MULTILINE)
#: `var.name` anywhere
_VAR_REF_RE = re.compile(r'\bvar\.([A-Za-z_][A-Za-z0-9_]*)')


#: Every character `str.splitlines()` treats as a line break. Masking must never
#: replace one of these — see `_mask_strings`.
_LINE_BREAKS = frozenset("\n\r\v\f\x1c\x1d\x1e\x85  ")


def _mask_strings(text: str) -> str:
    """Blank out the CONTENTS of double-quoted strings, preserving length.

    Quotes and line breaks survive; every other character inside a string
    becomes `x`. So `"unbalanced } here"` stops affecting brace depth and
    `"https://x//y"` stops looking like a comment, while offsets and line
    numbers stay exactly as they were.

    THE LINE-BREAK RULE IS LOad-BEARING. Output is a 1:1 character mapping, so
    line counts match the input only while every break character is preserved.
    `_strip_comments` zips this text against the original line by line; drop one
    break and every later line pairs with the wrong mask. That is not a
    near-miss — it truncated `module "n" {` to `mo` in testing, and dropped the
    trailing line entirely, falsely rejecting a correctly wired module.

    Escaped characters are masked EXCEPT breaks: `"abc\\<newline>def"` keeps its
    newline (and closes the string there). An unterminated quote is likewise
    closed at the break rather than swallowing the rest of the file — a
    malformed .tf must not silently disable the checks.
    """
    out: List[str] = []
    in_string = False
    escaped = False
    for ch in text:
        if not in_string:
            out.append(ch)
            if ch == '"':
                in_string = True
            continue
        if ch in _LINE_BREAKS:
            # Never mask a break, even an escaped one — see the docstring.
            out.append(ch)
            in_string = False
            escaped = False
        elif escaped:
            out.append("x")
            escaped = False
        elif ch == "\\":
            out.append("\\")
            escaped = True
        elif ch == '"':
            out.append('"')
            in_string = False
        else:
            out.append("x")
    return "".join(out)


def _strip_comments(original: str, masked: str) -> str:
    """Cut `#` / `//` comments from `original`, locating them in `masked`.

    Comment markers are found in the masked view so a `//` inside a URL is not
    mistaken for a comment, but the text returned is the ORIGINAL — masking
    blanks the inside of quotes, which is exactly where `variable "name"` keeps
    its name. `_mask_strings` preserves length and newlines, so the indices line
    up one-for-one.
    """
    orig_lines = original.splitlines()
    masked_lines = masked.splitlines()
    if len(orig_lines) != len(masked_lines):
        # The masker broke its own invariant. zip() would silently pair each
        # line with the WRONG mask and truncate the tail, corrupting code rather
        # than just losing comments.
        #
        # Return NOTHING rather than the original. Returning the original looks
        # conservative but is not: both `declared_variables` and
        # `wired_variables` read this text, so a surviving `# zones = var.zones`
        # would count as a real reference and let HOLE 2 pass — the hole
        # Terraform gives no diagnostic for. Empty text means nothing is
        # declared, so every emitted key becomes a HOLE 1 violation and the
        # compile is rejected. Loud and wrong beats quiet and wrong.
        return ""
    out: List[str] = []
    for orig_line, masked_line in zip(orig_lines, masked_lines):
        cut = len(orig_line)
        for marker in ("#", "//"):
            idx = masked_line.find(marker)
            if idx != -1:
                cut = min(cut, idx)
        out.append(orig_line[:cut])
    return "\n".join(out)


def _read_tf(module_dir: Path) -> str:
    """Root module source with comments cut. String contents INTACT (names live there)."""
    parts = [p.read_text(encoding="utf-8") for p in sorted(module_dir.glob("*.tf"))]
    joined = "\n".join(parts)
    return _strip_comments(joined, _mask_strings(joined))


def _read_tf_masked(module_dir: Path) -> str:
    """Same text, but with string CONTENTS blanked — safe for brace counting."""
    return _mask_strings(_read_tf(module_dir))


def is_terraform_root(module_dir: Path) -> bool:
    """True when the directory actually holds a Terraform root module."""
    return module_dir.is_dir() and any(module_dir.glob("*.tf"))


def declared_variables(module_dir: Path) -> Set[str]:
    """Variable names declared anywhere in the root module (HOLE 1 input)."""
    return set(_VARIABLE_RE.findall(_read_tf(module_dir)))


def wired_variables(module_dir: Path) -> Set[str]:
    """Variable names actually REFERENCED as `var.<name>` (HOLE 2 input).

    Deliberately broader than "is a module argument called this": a root may
    pass the value under a different argument name (`zone_data = var.zones`) or
    consume it directly in a resource (`for_each = var.zones`). Both genuinely
    use the value. What we are detecting is a variable nothing reads at all.

    Reads the masked view so a `var.x` mentioned inside a string literal does
    not count as a real reference.
    """
    return set(_VAR_REF_RE.findall(_read_tf_masked(module_dir)))


def module_arguments(module_dir: Path) -> Set[str]:
    """Argument names passed to any `module "..."` block.

    Not used by `check_contract` (see `wired_variables`), but kept for
    diagnostics and tests. Brace-matched so only top-level arguments of the
    module block count, not keys nested inside an object value.
    """
    text = _read_tf_masked(module_dir)
    args: Set[str] = set()
    for match in _MODULE_RE.finditer(text):
        depth, i, n = 0, match.end() - 1, len(text)
        start = match.end()
        while i < n:  # find the matching closing brace
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
                if depth == 0:
                    break
            i += 1
        body = text[start:i]
        # Only depth-1 lines are module arguments; skip nested object contents.
        depth = 0
        for line in body.splitlines():
            if depth == 0:
                found = _ARG_RE.match(line)
                if found:
                    args.add(found.group(1))
            depth += line.count("{") + line.count("[")
            depth -= line.count("}") + line.count("]")
    return args


def _matching_brace(text: str, open_idx: int) -> int:
    """Index of the `}` / `)` closing the bracket at `open_idx`, or -1."""
    pairs = {"{": "}", "(": ")"}
    closer = pairs[text[open_idx]]
    depth = 0
    for i in range(open_idx, len(text)):
        if text[i] == text[open_idx]:
            depth += 1
        elif text[i] == closer:
            depth -= 1
            if depth == 0:
                return i
    return -1


def declared_object_attributes(module_dir: Path, variable: str) -> Optional[Set[str]]:
    """Attribute names of a variable's `object({...})` type, or None.

    Returns None when the variable is absent or its type is not object-shaped
    (`string`, `bool`, `any`, a bare `map(string)`), which means there is no
    attribute contract to enforce.

    This is HOLE 3. Terraform's object-to-object conversion SILENTLY DISCARDS
    attributes the target type does not declare — no warning, no diagnostic,
    exit 0. A root variable typed more narrowly than the module it feeds drops
    the extra fields and the module falls back to its own `optional(...)`
    defaults. Key-level checking cannot see it: the key is declared and wired.

    Live example: the root's `security_rules` omitted the six ADR-0003
    attributes, so `application`, `profile_group` and `log_setting` never
    reached the module and rules were built with `["any"]` instead of the
    intent's App-ID.
    """
    # The variable NAME lives inside quotes, which masking blanks — so locate the
    # declaration in the unmasked text, then do every structural scan on the
    # masked view. `_mask_strings` is a 1:1 character map over the same
    # comment-stripped source, so the two are index-aligned.
    text = _read_tf(module_dir)
    masked = _mask_strings(text)
    decl = re.search(r'^\s*variable\s+"' + re.escape(variable) + r'"\s*\{', text, re.MULTILINE)
    if decl is None:
        return None
    end = _matching_brace(masked, decl.end() - 1)
    block = masked[decl.end() - 1: end if end != -1 else len(masked)]

    obj = re.search(r"\bobject\s*\(", block)
    if obj is None:
        return None
    paren_close = _matching_brace(block, obj.end() - 1)
    inner = block[obj.end(): paren_close if paren_close != -1 else len(block)]
    brace = inner.find("{")
    if brace == -1:
        return None
    body = inner[brace + 1: _matching_brace(inner, brace)]

    attrs = _object_body_attributes(body)
    return attrs or None


def _object_body_attributes(body: str, prefix: str = "") -> Set[str]:
    """Attribute paths in an `object({...})` body, RECURSING into nested objects.

    Returns dotted paths: `network`, `network.layer3`, `network.log_setting`, …

    Recursion is the point. `layer3` on an interface and `network` on a zone are
    nested objects, and HOLE 3 lives exactly there: Terraform discards an
    undeclared attribute at ANY depth, silently. Checking only the top level
    would pass a root whose nested type is narrower than the module's.
    """
    attrs: Set[str] = set()
    lines = body.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        found = _ARG_RE.match(line)
        if not found:
            i += 1
            continue
        name = found.group(1)
        path = f"{prefix}.{name}" if prefix else name
        attrs.add(path)

        # Does this attribute's TYPE open a nested object({...})? Scan from the
        # `=` to the matching close, over the remainder of the body.
        rest = "\n".join(lines[i:])
        eq = rest.find("=")
        nested = re.search(r"\bobject\s*\(", rest[eq:]) if eq != -1 else None
        consumed = 1
        if nested is not None:
            # Only treat it as THIS attribute's type if no other attribute
            # declaration intervenes before the `object(`.
            between = rest[eq + 1: eq + nested.start()]
            if not _ARG_RE.search(between):
                start = eq + nested.end() - 1                 # the `(`
                close = _matching_brace(rest, start)
                if close != -1:
                    inner = rest[nested.end() + eq: close]
                    brace = inner.find("{")
                    if brace != -1:
                        end_brace = _matching_brace(inner, brace)
                        if end_brace != -1:
                            attrs |= _object_body_attributes(
                                inner[brace + 1: end_brace], path)
                    consumed = rest[:close].count("\n") + 1
        i += consumed
    return attrs


def _emitted_paths(value: Any, prefix: str = "") -> Set[str]:
    """Dotted attribute paths in an emitted object, matching the declared form.

    A `None` value contributes its own path but no children — the compiler emits
    `"user_acl": null` for an unset optional object, which asserts nothing about
    the nested type.

    LISTS OF OBJECTS RECURSE AT THE SAME PREFIX. `declared_object_attributes`
    collapses the list level (`list(object({name=...}))` under `vrf` declares
    `vrf.name`, not `vrf.*.name`), so the emitted side must collapse it too or
    the two never line up. Without this, HOLE 3 passed VACUOUSLY for every
    list-of-object attribute: `routers` compared only {name, folder, vrf} and
    never looked at a single static-route field, four levels down. A list of
    scalars (e.g. layer3.ip) contributes nothing, which is correct — a string
    asserts no attribute names.
    """
    paths: Set[str] = set()
    if isinstance(value, list):
        for item in value:
            paths |= _emitted_paths(item, prefix)
        return paths
    if not isinstance(value, dict):
        return paths
    for k, v in value.items():
        path = f"{prefix}.{k}" if prefix else k
        paths.add(path)
        if isinstance(v, (dict, list)):
            paths |= _emitted_paths(v, path)
    return paths


def check_object_attributes(module_dir: Path, key: str, payload: Any) -> List[str]:
    """Every attribute the compiler emits for `key` must be declared. HOLE 3.

    `payload` is the emitted value for that tfvars key. For the map-of-object
    shape the compiler produces, the attributes are the union of the inner
    dicts' keys.
    """
    declared = declared_object_attributes(module_dir, key)
    if declared is None:
        return []
    if isinstance(payload, dict):
        values = [v for v in payload.values() if isinstance(v, dict)]
    elif isinstance(payload, list):
        values = [v for v in payload if isinstance(v, dict)]
    else:
        return []
    emitted: Set[str] = set()
    for v in values:
        emitted |= _emitted_paths(v)

    missing = sorted(emitted - declared)
    if not missing:
        return []
    return [
        f"{module_dir}: `variable \"{key}\"` does not declare attribute(s) {missing}, "
        f"but the compiler emits them — Terraform DISCARDS undeclared object "
        f"attributes silently (no warning, exit 0) and the module falls back to "
        f"its own defaults. Add them to the object type in variables.tf."
    ]


def check_contract(module_dir: Path, emitted_keys: Iterable[str]) -> List[str]:
    """Verify emitted tfvars keys are both DECLARED and REFERENCED. Fail-closed.

    Returns actionable violation messages; an empty list means the contract
    holds. Checking only the declaration would still let HOLE 2 through.
    """
    keys = sorted(set(emitted_keys))
    if not keys:
        return []
    if not is_terraform_root(module_dir):
        return [
            f"{module_dir}: no Terraform root module here, but the compiler emitted "
            f"{keys} — the data would be written where nothing reads it"
        ]

    declared = declared_variables(module_dir)
    wired = wired_variables(module_dir)

    violations: List[str] = []
    for key in keys:
        if key not in declared:
            violations.append(
                f"{module_dir}: compiled data sets {key!r} but no `variable \"{key}\"` is "
                f"declared — Terraform would WARN and silently ignore it (exit 0). "
                f"Add it to variables.tf and pass it to the module block."
            )
        elif key not in wired:
            violations.append(
                f"{module_dir}: `variable \"{key}\"` is declared but `var.{key}` is never "
                f"referenced — Terraform gives NO diagnostic and the data is silently "
                f"ignored. Add `{key} = var.{key}` to the module block."
            )
    return violations
