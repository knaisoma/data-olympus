import pytest

from data_olympus.format.frontmatter import parse_frontmatter


def test_parses_frontmatter_and_body():
    text = "---\ntype: standard\nid: STD-U-002\n---\n# Body\n\nhello\n"
    fm, body = parse_frontmatter(text)
    assert fm == {"type": "standard", "id": "STD-U-002"}
    assert body == "# Body\n\nhello\n"


def test_no_frontmatter_returns_empty_dict_and_unchanged_body():
    text = "# Just a heading\n\nno frontmatter here\n"
    fm, body = parse_frontmatter(text)
    assert fm == {}
    assert body == text


def test_unterminated_frontmatter_raises():
    with pytest.raises(ValueError):
        parse_frontmatter("---\ntype: standard\n# never closed\n")


def test_non_mapping_frontmatter_raises():
    with pytest.raises(ValueError):
        parse_frontmatter("---\n- just\n- a\n- list\n---\nbody\n")


def test_invalid_yaml_raises_value_error():
    with pytest.raises(ValueError, match="invalid YAML"):
        parse_frontmatter("---\nkey: : bad syntax\n---\nbody\n")
