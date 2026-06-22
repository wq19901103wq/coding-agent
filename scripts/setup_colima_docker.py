#!/usr/bin/env python3
"""Start Colima and configure Docker for SWE-bench evaluation.

This helper is intended for macOS users who cannot install Docker Desktop.
It starts an Apple-Silicon-friendly Colima VM and points Docker at a stable
public mirror (DaoCloud) so that base images such as ``ubuntu:22.04`` can be
pulled from within China.

Usage:
    python scripts/setup_colima_docker.py
"""

from __future__ import annotations

import argparse
import json
import logging
import shutil
import subprocess
from pathlib import Path

logger = logging.getLogger("setup_colima_docker")

DAEMON_JSON = {
    "exec-opts": ["native.cgroupdriver=cgroupfs"],
    "features": {"buildkit": True, "containerd-snapshotter": True},
    "registry-mirrors": ["https://docker.m.daocloud.io"],
}


def _run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess[str]:
    logger.info("running: %s", " ".join(cmd))
    return subprocess.run(cmd, capture_output=True, text=True, check=False, **kwargs)


def _colima_installed() -> bool:
    return shutil.which("colima") is not None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cpu", type=int, default=4, help="Colima VM CPUs (default: 4)")
    parser.add_argument(
        "--memory", type=int, default=8, help="Colima VM memory in GiB (default: 8)"
    )
    parser.add_argument(
        "--disk", type=int, default=100, help="Colima VM disk in GiB (default: 100)"
    )
    parser.add_argument("--arch", default="aarch64", help="VM architecture")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if not _colima_installed():
        logger.error("colima is not installed; run 'brew install colima docker'")
        return 1

    status = _run(["colima", "status"])
    if status.returncode != 0:
        logger.info("starting colima VM (arch=%s)", args.arch)
        start = _run(
            [
                "colima",
                "start",
                "--arch",
                args.arch,
                "--cpu",
                str(args.cpu),
                "--memory",
                str(args.memory),
                "--disk",
                str(args.disk),
                "--runtime",
                "docker",
            ]
        )
        if start.returncode != 0:
            logger.error("failed to start colima:\n%s", start.stderr)
            return 1
    else:
        logger.info("colima is already running")

    logger.info("configuring docker daemon registry mirrors")
    daemon_json = json.dumps(DAEMON_JSON, indent=2)
    write = _run(
        ["colima", "ssh", "--", "sudo", "tee", "/etc/docker/daemon.json"],
        input=daemon_json + "\n",
    )
    if write.returncode != 0:
        logger.error("failed to write daemon.json:\n%s", write.stderr)
        return 1

    restart = _run(["colima", "ssh", "--", "sudo", "systemctl", "restart", "docker"])
    if restart.returncode != 0:
        logger.error("failed to restart docker:\n%s", restart.stderr)
        return 1

    sock = Path.home() / ".colima" / "default" / "docker.sock"
    logger.info("colima docker ready; export DOCKER_HOST=unix://%s", sock)
    logger.info("verify with: docker info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
