from pathlib import Path

from data_olympus.viewer.generator import generate_visualization


def _write(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def test_visualization_excludes_template_md_from_node_count(tmp_path):
    """template.md is reserved (format/validate.py RESERVED) like index.md and
    log.md, so it must not be counted as a concept node in the generated
    visualization graph."""
    _write(
        tmp_path / "a.md",
        "---\nid: A\ntype: standard\nstatus: active\ntier: T1\n"
        "title: A\ndescription: doc a.\n---\nbody\n",
    )
    _write(tmp_path / "template.md", "---\nid: TEMPLATE\n---\nplaceholder\n")
    out = tmp_path / "viz.html"
    stats = generate_visualization(tmp_path, out)
    assert stats["nodes"] == 1
