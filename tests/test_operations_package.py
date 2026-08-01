import subprocess
import sys
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def test_release_runtime_imports_from_the_repository_scripts_package() -> None:
    process = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "from pathlib import Path; "
                "import scripts; "
                "from scripts.operations import release_runtime; "
                "print(Path(scripts.__file__).resolve()); "
                "print(Path(release_runtime.__file__).resolve())"
            ),
        ],
        cwd=REPOSITORY_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert process.returncode == 0, process.stderr
    assert process.stdout.splitlines() == [
        str(REPOSITORY_ROOT / "scripts" / "__init__.py"),
        str(REPOSITORY_ROOT / "scripts" / "operations" / "release_runtime.py"),
    ]
