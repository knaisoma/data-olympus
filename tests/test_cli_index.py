from pathlib import Path

from data_olympus.cli.main import main


def _write(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def test_index_generates_index_md_from_frontmatter(tmp_path):
    _write(
        tmp_path / "tables/orders.md",
        "---\nid: T1\ntype: standard\nstatus: active\ntier: T1\n"
        "title: Orders\ndescription: One row per order.\n---\nbody\n",
    )
    _write(
        tmp_path / "tables/customers.md",
        "---\nid: T2\ntype: standard\nstatus: active\ntier: T1\n"
        "title: Customers\ndescription: One row per customer.\n---\nbody\n",
    )
    code = main(["index", str(tmp_path)])
    assert code == 0
    idx = (tmp_path / "tables" / "index.md").read_text(encoding="utf-8")
    assert "[Orders](orders.md)" in idx
    assert "One row per order." in idx
    assert "[Customers](customers.md)" in idx


def test_index_lists_subdirectories(tmp_path):
    _write(
        tmp_path / "a.md",
        "---\nid: A\ntype: standard\nstatus: active\ntier: T1\n"
        "title: A\ndescription: doc a.\n---\n",
    )
    _write(
        tmp_path / "sub/b.md",
        "---\nid: B\ntype: standard\nstatus: active\ntier: T1\n"
        "title: B\ndescription: doc b.\n---\n",
    )
    main(["index", str(tmp_path)])
    root_idx = (tmp_path / "index.md").read_text(encoding="utf-8")
    assert "[sub/](sub/)" in root_idx or "(sub/)" in root_idx
