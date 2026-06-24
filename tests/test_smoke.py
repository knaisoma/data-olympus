import data_olympus


def test_package_imports_and_exposes_version():
    assert data_olympus.__version__ == "0.1.0"
