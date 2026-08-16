"""An address created by hand in a managed folder must not be invisible.

THE FINDING THIS EXISTS FOR. On 2026-08-16 a fixture address was created
directly in the GitOps folder and left live while the nightly job ran. It
reported "No drift of any kind — SCM matches the declared policy." Nothing looks
at address or service objects: they are not registry kinds, and `objectsweep`
only ever asks "did WE mint this", which is the right question for deleting and
the wrong one for detecting.

The rows below are the tenant's real shapes, from probe run 31934912556.
"""

from __future__ import annotations

from fwgitops.objectdrift import classify, detect

# ── exactly what SCM returns for the GitOps folder ──────────────────────────
SINKHOLE = {"name": "Palo Alto Networks Sinkhole", "folder": "All",
            "snippet": "default", "fqdn": "sinkhole.paloaltonetworks.com",
            "description": "..."}
PREDEF_HTTP = {"name": "service-http", "folder": "All",
               "snippet": "predefined-snippet",
               "protocol": {"tcp": {"port": "80"}}}
OURS_ADDR = {"name": "addr-10.90.1.10_32-88ef17b7", "folder": "GitOps",
             "ip_netmask": "10.90.1.10/32", "tag": ["gitops:managed"]}
OURS_SVC = {"name": "svc-tcp_8443-02a1b12c", "folder": "GitOps",
            "protocol": {"tcp": {"port": "8443"}}, "tag": ["gitops:managed"]}
HANDMADE = {"name": "fixture-unmanaged-31934276561", "folder": "GitOps",
            "ip_netmask": "192.0.2.1/32"}


def _prov(rows, kind="address", scope="GitOps"):
    return {c.name: c.provenance for c in classify(rows, scope=scope, kind=kind)}


def test_the_hand_made_address_that_slipped_through_is_UNMANAGED():
    """The exact object, with the exact fields SCM returned for it, live in the
    folder while the job said everything matched."""
    assert _prov([HANDMADE])["fixture-unmanaged-31934276561"] == "unmanaged"


def test_an_object_SCM_ITSELF_PROVIDES_is_not_drift():
    """`Palo Alto Networks Sinkhole` and the predefined services are shipped by
    the platform. Reporting them would make the check cry wolf on its first run
    and teach everyone to ignore it."""
    assert _prov([SINKHOLE])["Palo Alto Networks Sinkhole"] == "scm"
    assert _prov([PREDEF_HTTP], kind="service")["service-http"] == "scm"


def test_SNIPPET_IS_CHECKED_BEFORE_FOLDER():
    """Order of the tests, pinned, because both fields are present at once.

    A predefined object reports `folder: All` AND `snippet: default`. Testing
    the folder first calls it INHERITED — a different and wrong story about
    where it came from, and one that would quietly survive since both classes
    are treated as "not drift".
    """
    c = classify([SINKHOLE], scope="GitOps", kind="address")[0]
    assert c.provenance == "scm", (
        "snippet must win over folder; both are set on a predefined object")
    assert c.folder == "All" and c.snippet == "default"


def test_an_ancestors_object_is_INHERITED_not_drift():
    """A folder read returns the tree above it. Somebody else's config, seen
    from here — flagging it would report every parent object in every child."""
    row = dict(OURS_ADDR, folder="prod-edge", name="addr-1.2.3.4_32-deadbeef")
    assert _prov([row])["addr-1.2.3.4_32-deadbeef"] == "inherited"


def test_an_object_THIS_PLATFORM_MINTED_is_ours():
    assert _prov([OURS_ADDR])["addr-10.90.1.10_32-88ef17b7"] == "ours"
    assert _prov([OURS_SVC], kind="service")["svc-tcp_8443-02a1b12c"] == "ours"


def test_a_gitops_LOOKING_name_that_does_not_hash_to_its_value_is_unmanaged():
    """The forgery case, and the reason ownership is proven rather than read.

    Anyone can name an object `addr-10.90.1.10_32-88ef17b7`. Only the object
    whose value actually hashes to that name IS it — so a hand-made object
    wearing our naming scheme, with different contents, is caught.
    """
    forged = {"name": "addr-10.90.1.10_32-88ef17b7", "folder": "GitOps",
              "ip_netmask": "10.0.0.99/32"}       # not the value it claims
    assert _prov([forged])["addr-10.90.1.10_32-88ef17b7"] == "unmanaged"


def test_a_tag_alone_does_NOT_make_an_object_ours():
    """`gitops:managed` is a label anyone can type in the console. Ownership is
    proven from the name-to-value relationship, which cannot be forged without
    also making the object into the thing it claims to be."""
    liar = {"name": "totally-legit", "folder": "GitOps",
            "ip_netmask": "10.0.0.1/32", "tag": ["gitops:managed"]}
    assert _prov([liar])["totally-legit"] == "unmanaged"


def test_the_whole_folder_as_it_actually_stands_is_CLEAN():
    """The tenant on 2026-08-16, once the fixture was deleted. The check has to
    be able to pass on real data, or it is noise nobody will action."""
    report = detect({"address": [SINKHOLE, OURS_ADDR],
                     "service": [OURS_SVC, PREDEF_HTTP]}, scope="GitOps")
    assert report.is_clean, report.summary()
    assert "nothing unaccounted for" in report.summary()


def test_the_same_folder_WITH_the_fixture_is_not_clean():
    report = detect({"address": [SINKHOLE, OURS_ADDR, HANDMADE],
                     "service": [OURS_SVC, PREDEF_HTTP]}, scope="GitOps")
    assert not report.is_clean
    assert [o.name for o in report.unmanaged] == ["fixture-unmanaged-31934276561"]
    assert "UNMANAGED OBJECT(S)" in report.summary()


def test_a_row_with_no_name_is_skipped_rather_than_crashing():
    assert classify([{"folder": "GitOps"}, "nonsense", None],
                    scope="GitOps", kind="address") == []


def test_an_object_whose_VALUE_SCM_DOES_NOT_REPORT_is_unmanaged_not_ours():
    """Ownership cannot be proven without the value, and the safe direction here
    is the opposite of the sweep's.

    `objectsweep` treats unprovable as NOT ours so it will not delete it — the
    fail-safe when the consequence is destruction. Here the consequence is a
    report, so unprovable must mean FLAGGED: staying silent about an object
    nobody can account for is the failure this module exists to fix.
    """
    opaque = {"name": "addr-10.90.1.10_32-88ef17b7", "folder": "GitOps"}
    assert _prov([opaque])["addr-10.90.1.10_32-88ef17b7"] == "unmanaged"
