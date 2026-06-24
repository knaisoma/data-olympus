from data_olympus.cli.main import main


def test_lint_missing_path_returns_nonzero(capsys):
    assert main(["lint", "/no/such/dir"]) == 1
    assert "not a directory" in capsys.readouterr().err


def test_index_missing_path_returns_nonzero():
    assert main(["index", "/no/such/dir"]) == 1


def test_visualize_missing_path_returns_nonzero():
    assert main(["visualize", "/no/such/dir"]) == 1
