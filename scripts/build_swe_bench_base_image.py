#!/usr/bin/env python3
"""Build the SWE-bench base Docker image for the local Docker daemon.

This script is mainly useful on Apple Silicon Macs where the official
SWE-bench x86_64 images cannot be pulled directly.  It builds an arm64 base
image using the Tsinghua Anaconda mirror so that subsequent environment and
instance images can be built locally.

Usage:
    python scripts/build_swe_bench_base_image.py

The resulting image is tagged ``sweb.base.py.arm64:latest`` (or
``sweb.base.py.x86_64:latest`` on an x86_64 daemon).
"""

from __future__ import annotations

import argparse
import logging
import tempfile
from pathlib import Path

import docker

logger = logging.getLogger("build_swe_bench_base_image")


DOCKERFILE = """\
FROM ubuntu:22.04

ARG DEBIAN_FRONTEND=noninteractive
ENV TZ=Etc/UTC

RUN apt update && apt install -y \\
wget git build-essential libffi-dev libtiff-dev \\
python3 python3-pip python-is-python3 jq curl \\
locales locales-all tzdata && rm -rf /var/lib/apt/lists/*

RUN wget -q 'https://mirrors.tuna.tsinghua.edu.cn/anaconda/miniconda/{miniconda_installer}' \\
    -O /tmp/miniconda.sh \\
    && bash /tmp/miniconda.sh -b -p /opt/miniconda3 \\
    && rm /tmp/miniconda.sh
ENV PATH=/opt/miniconda3/bin:$PATH
RUN conda init --all && conda config --append channels conda-forge

COPY condarc /root/.condarc
RUN adduser --disabled-password --gecos 'dog' nonroot
"""


CONDARC = """\
channels:
  - defaults
  - conda-forge
show_channel_urls: true
default_channels:
  - https://mirrors.tuna.tsinghua.edu.cn/anaconda/pkgs/main
  - https://mirrors.tuna.tsinghua.edu.cn/anaconda/pkgs/r
  - https://mirrors.tuna.tsinghua.edu.cn/anaconda/pkgs/msys2
custom_channels:
  conda-forge: https://mirrors.tuna.tsinghua.edu.cn/anaconda/cloud
"""


def _docker_client(base_url: str | None) -> docker.DockerClient:
    if base_url:
        return docker.DockerClient(base_url=base_url)
    try:
        return docker.from_env()
    except docker.errors.DockerException:
        colima_sock = Path.home() / ".colima" / "default" / "docker.sock"
        if colima_sock.exists():
            return docker.DockerClient(base_url=f"unix://{colima_sock}")
        raise


def _arch(client: docker.DockerClient) -> str:
    daemon_arch = client.version().get("Arch", "").lower()
    if daemon_arch in ("arm64", "aarch64"):
        return "arm64"
    if daemon_arch == "amd64":
        return "x86_64"
    raise RuntimeError(f"unsupported docker daemon architecture: {daemon_arch}")


def _miniconda_installer(arch: str) -> str:
    if arch == "arm64":
        return "Miniconda3-py311_23.11.0-2-Linux-aarch64.sh"
    return "Miniconda3-py311_23.11.0-2-Linux-x86_64.sh"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--docker-base-url",
        default=None,
        help="Docker daemon URL (defaults to DOCKER_HOST or the Colima socket)",
    )
    parser.add_argument(
        "--push",
        action="store_true",
        help="Push the built image to a registry (not implemented)",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    client = _docker_client(args.docker_base_url)
    arch = _arch(client)
    tag = f"sweb.base.py.{arch}:latest"
    logger.info("building base image %s for docker daemon arch=%s", tag, arch)

    with tempfile.TemporaryDirectory(prefix="swe_base_") as tmp:
        tmp_path = Path(tmp)
        (tmp_path / "Dockerfile").write_text(
            DOCKERFILE.format(miniconda_installer=_miniconda_installer(arch)),
            encoding="utf-8",
        )
        (tmp_path / "condarc").write_text(CONDARC, encoding="utf-8")

        image, build_logs = client.images.build(
            path=str(tmp_path),
            dockerfile="Dockerfile",
            tag=tag,
            platform=f"linux/{arch}",
            rm=True,
        )
        for chunk in build_logs:
            if "stream" in chunk:
                line = chunk["stream"].rstrip()
                if line:
                    logger.debug("build: %s", line)

    logger.info("built base image %s (id=%s)", tag, image.id)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
