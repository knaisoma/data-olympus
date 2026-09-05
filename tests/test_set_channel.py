import os
import subprocess
from pathlib import Path

import pytest
import yaml


@pytest.mark.parametrize("channel", ["v0.7.2", "0.7.2-rc.1", "", "custom"])
def test_channel_dispatch_rejects_nonchannel_tags(channel, tmp_path):
    workflow = yaml.safe_load(
        (Path(__file__).resolve().parents[1] / ".github/workflows/set-channel.yml").read_text()
    )
    script = next(
        step["run"] for step in workflow["jobs"]["retag"]["steps"]
        if step.get("name") == "Re-point channel at source digest"
    )
    marker = tmp_path / "docker-called"
    docker = tmp_path / "docker"
    docker.write_text('#!/bin/sh\ntouch "$MARKER"\nexit 0\n')
    docker.chmod(0o755)
    result = subprocess.run(
        ["/bin/bash", "-e", "-c", script],
        env={**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}",
             "CHANNEL": channel, "SOURCE": "v0.7.1", "REPO": "example/image",
             "MARKER": str(marker)},
        capture_output=True, text=True,
    )
    assert result.returncode != 0
    assert not marker.exists(), "invalid channels must fail before registry access"
