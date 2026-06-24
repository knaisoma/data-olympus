from pathlib import Path

from data_olympus.format.document import Document


def test_load_reads_frontmatter_and_exposes_accessors(tmp_path: Path):
    p = tmp_path / "STD-U-002-writing-style.md"
    p.write_text(
        "---\nid: STD-U-002\ntype: standard\nstatus: active\ntier: T1\n---\n# Writing\n",
        encoding="utf-8",
    )
    doc = Document.load(p)
    assert doc.id == "STD-U-002"
    assert doc.type == "standard"
    assert doc.status == "active"
    assert doc.tier == "T1"
    assert doc.body == "# Writing\n"


def test_accessors_default_to_none_when_absent(tmp_path: Path):
    p = tmp_path / "note.md"
    p.write_text("# no frontmatter\n", encoding="utf-8")
    doc = Document.load(p)
    assert doc.id is None
    assert doc.type is None
    assert doc.frontmatter == {}
