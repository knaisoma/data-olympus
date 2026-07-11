from __future__ import annotations

import io

from scripts.rc_tag import main


def test_main_prints_next_rc_tag(capsys) -> None:
    rc = main(["--base", "0.5.0"], stdin=io.StringIO("0.5.0-rc.1\nv0.5.0-rc.2\n0.4.0-rc.7\n"))
    assert rc == 0
    assert capsys.readouterr().out.strip() == "0.5.0-rc.3"


def test_main_starts_at_rc_1_with_empty_stdin(capsys) -> None:
    rc = main(["--base", "0.5.0"], stdin=io.StringIO(""))
    assert rc == 0
    assert capsys.readouterr().out.strip() == "0.5.0-rc.1"
