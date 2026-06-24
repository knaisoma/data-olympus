import json
import re
from pathlib import Path

from data_olympus.cli.main import main


def test_visualize_xss_title_not_in_rendered_html(tmp_path):
    """A concept title containing raw HTML must not appear unescaped in the
    generated HTML outside the JSON data blob.

    The backlinks list and node labels previously used innerHTML with raw
    bundle-derived strings. This test catches a regression where an attacker
    could inject JavaScript via a crafted frontmatter title (stored-XSS).
    """
    xss_title = '<img src=x onerror=alert(1)>'
    (tmp_path / "a.md").write_text(
        f"---\nid: a\ntype: standard\nstatus: active\ntier: T1\ntitle: {xss_title}\n---\n"
        "Link to [b](/b.md).\n",
        encoding="utf-8",
    )
    (tmp_path / "b.md").write_text(
        "---\nid: b\ntype: standard\nstatus: active\ntier: T1\ntitle: B\n---\nbody\n",
        encoding="utf-8",
    )
    out = tmp_path / "v.html"
    main(["visualize", str(tmp_path), "-o", str(out)])
    content = out.read_text(encoding="utf-8")

    # Locate the JSON data blob boundary so we can exclude it from the check.
    json_pattern = r"window\.__BUNDLE_DATA__\s*=\s*(\{.*?\});\s*</script>"
    json_match = re.search(json_pattern, content, re.DOTALL)
    assert json_match, "embedded bundle data not found"
    json_start, json_end = json_match.start(1), json_match.end(1)

    # Build the parts of the HTML that are outside the JSON blob.
    outside_json = content[:json_start] + content[json_end:]

    # The raw XSS payload must not appear outside the data blob.
    assert xss_title not in outside_json, (
        "Raw XSS title appeared outside the JSON data blob; "
        "innerHTML with unescaped bundle-derived string detected."
    )


def test_visualize_ignores_links_in_code_fences(tmp_path):
    (tmp_path / "a.md").write_text(
        "---\nid: a\ntype: standard\nstatus: active\ntier: T1\ntitle: A\n---\n"
        "Real link to [b](/b.md).\n\n```\nexample [c](/b.md) in a code fence\n```\n",
        encoding="utf-8",
    )
    (tmp_path / "b.md").write_text(
        "---\nid: b\ntype: standard\nstatus: active\ntier: T1\ntitle: B\n---\nbody\n",
        encoding="utf-8",
    )
    out = tmp_path / "v.html"
    main(["visualize", str(tmp_path), "-o", str(out)])
    data = json.loads(
        re.search(r"__BUNDLE_DATA__\s*=\s*(\{.*?\});", out.read_text(), re.DOTALL).group(1)
    )
    edges = [(e["source"], e["target"]) for e in data["edges"]]
    # exactly one a->b edge (the code-fence link is ignored, not double-counted)
    assert edges.count(("a", "b")) == 1


def _write(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def test_visualize_writes_self_contained_html_with_nodes_and_edges(tmp_path):
    _write(
        tmp_path / "tables/orders.md",
        "---\nid: orders\ntype: standard\nstatus: active\ntier: T1\ntitle: Orders\n---\n"
        "Linked to [customers](/tables/customers.md).\n",
    )
    _write(
        tmp_path / "tables/customers.md",
        "---\nid: customers\ntype: standard\nstatus: active\ntier: T1\n"
        "title: Customers\n---\nbody\n",
    )
    out = tmp_path / "viz.html"
    code = main(["visualize", str(tmp_path), "-o", str(out)])
    assert code == 0
    html = out.read_text(encoding="utf-8")
    assert "<html" in html.lower()
    # the embedded graph JSON must contain both concepts and the orders->customers edge
    m = re.search(r"__BUNDLE_DATA__\s*=\s*(\{.*?\});", html, re.DOTALL)
    assert m, "embedded bundle data not found"
    data = json.loads(m.group(1))
    ids = {n["id"] for n in data["nodes"]}
    assert {"tables/orders", "tables/customers"} <= ids
    assert any(
        e["source"] == "tables/orders" and e["target"] == "tables/customers"
        for e in data["edges"]
    )
