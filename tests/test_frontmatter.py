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


# issue #317: only a --- line at column zero closes the block -----------------


def test_indented_closer_is_content_not_the_end_of_the_block():
    text = (
        "---\n"
        "type: memory\n"
        "notes: 'x\n"
        "\n"
        "  ---\n"
        "\n"
        "  '\n"
        "status: proposed\n"
        "---\n"
        "body\n"
    )
    fm, body = parse_frontmatter(text)
    assert fm == {"type": "memory", "notes": "x\n---\n", "status": "proposed"}
    assert body == "body\n"


def test_trailing_whitespace_and_cr_on_the_closer_still_close():
    fm, body = parse_frontmatter("---\ntype: standard  \n---   \nbody\n")
    assert fm == {"type": "standard"}
    assert body == "body\n"
    crlf = "---\r\ntype: standard\r\n---\r\nbody\r\n"
    fm, body = parse_frontmatter(crlf)
    assert fm == {"type": "standard"}
    assert body == "body\r\n"


def test_unterminated_when_only_indented_closers_exist():
    with pytest.raises(ValueError, match="unterminated"):
        parse_frontmatter("---\nkey: 'a\n\n  ---\n\n  '\n")


@pytest.mark.parametrize("field", ["created_by", "tags", "evidence", "capture"])
def test_rendered_free_text_field_with_newline_dash_dash_parses_back(field):
    """A free-text value holding a newline followed by '---' survives the
    round trip: safe_dump renders it as a quoted scalar with indented
    continuation lines, and the parser must not close the block there."""
    from data_olympus.tools_write import _render_memory

    payload = "prefix\n---\nsuffix"
    kwargs: dict = {"text": "body", "tags": [], "agent_identity": "agent"}
    if field == "created_by":
        kwargs["agent_identity"] = payload
    elif field == "tags":
        kwargs["tags"] = [payload]
    elif field == "evidence":
        kwargs["evidence"] = [payload]
    else:
        envelope = {
            "capture_source": "claude_code.hook",
            "capture_event_id": payload,
            "source_event_hash": "h",
            "transformation": "t",
            "raw_retention": "discarded",
            "derived_memory_hash": "d" * 64,
        }
        kwargs["capture"] = envelope
    out = _render_memory(**kwargs)
    fm, parsed_body = parse_frontmatter(out)
    assert fm["type"] == "memory"
    assert fm["status"] == "proposed"
    if field == "created_by":
        assert fm["created_by"] == payload
    elif field == "tags":
        assert fm["tags"] == [payload]
    elif field == "evidence":
        assert fm["evidence"] == [payload]
    else:
        assert fm["capture"]["capture_event_id"] == payload
    assert parsed_body == "\nbody\n"
