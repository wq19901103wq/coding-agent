# SWE-bench Docker 模式搭建与使用

本仓库支持两种 SWE-bench 评估方式：

1. **本地 conda 模式**（默认）：在宿主机构建 conda 环境并运行 pytest。
2. **Docker 模式**（`--use-docker`）：使用 SWE-bench 官方容器镜像运行评估。

Docker 模式可以绕开 macOS 上部分旧版本仓库（如 astropy、django）C 扩展编译失败的问题，但在中国内网环境下通常无法直接拉取 Docker Hub 上的官方镜像，需要本地构建。

## 环境要求

- macOS（Apple Silicon 或 Intel）
- [Homebrew](https://brew.sh/)
- 已安装 `colima` 与 `docker` CLI

```bash
brew install colima docker qemu
```

> `qemu` 仅在需要运行 x86_64 VM 时才必须；在 Apple Silicon 上使用 arm64 容器时不需要。

## 1. 启动并配置 Colima

```bash
python scripts/setup_colima_docker.py
```

该脚本会：

- 启动一个 aarch64 Colima VM（默认 4 CPU / 8 GiB 内存 / 100 GiB 磁盘）。
- 配置 Docker daemon 使用 DaoCloud 镜像加速，以便拉取 `ubuntu:22.04` 等基础镜像。

配置完成后，设置环境变量：

```bash
export DOCKER_HOST=unix://$HOME/.colima/default/docker.sock
```

验证：

```bash
docker info
docker run --rm ubuntu:22.04 uname -m
```

## 2. 构建 SWE-bench 基础镜像

由于官方 `swebench/sweb.eval.x86_64.*` 镜像在 Docker Hub，国内无法直接拉取，我们在本地构建 arm64 基础镜像：

```bash
python scripts/build_swe_bench_base_image.py
```

该镜像使用清华 Anaconda 镜像安装 Miniconda，避免 `repo.anaconda.com` 连接失败。

## 3. 运行单个任务（Docker 模式）

```bash
python -m swe_bench.cli \
  --dataset data/swe-bench-lite-test.json \
  --output output/swe-lite-docker \
  --use-docker \
  --timeout 600 \
  --limit 1
```

- 首次运行某个任务时，会自动构建该任务对应的 env image 与 instance image（基于已存在的基础镜像）。
- 已构建的镜像会被复用，后续运行相同任务时无需重新构建。

## 4. 运行全量数据集

```bash
python -m swe_bench.cli \
  --dataset data/swe-bench-lite-test.json \
  --output output/swe-lite-docker \
  --use-docker \
  --timeout 900
```

> 注意：Docker 模式下每个任务首次运行时都需要本地构建 instance image，因此全量 300 任务会非常慢。建议先小批量验证，再决定是否全量运行。

## 5. SWE-agent 对比环境（`swe_agent_py311`）

三系统对比中的 SWE-agent（v0.7.0）需要一个独立的 conda 环境 `swe_agent_py311`：

```bash
conda create -n swe_agent_py311 python=3.11 -y
conda activate swe_agent_py311
# SWE-agent 装在 /tmp/SWE-agent-0.7.0，editable install
pip install -e /tmp/SWE-agent-0.7.0
pip install python-dotenv unidiff
```

**关键依赖修复**：SWE-agent 0.7.0 依赖 pandas，而 pandas 3.x 要求 `numpy>=1.26`。若环境里是 numpy 1.24，`import sweagent` 会因 `numpy/pandas` 冲突直接崩（表现为 20 任务全部 `exit code 1`、2 秒即退）。必须升级：

```bash
pip install "numpy>=1.26.0"
```

**API key**：SWE-agent 0.7.0 的 `keys_config` 优先读环境变量，无需建 `keys.cfg`。`scripts/compare_three_systems.py` 会从 `.env` 的 `CODING_AGENT_LLM_API_KEY`/`CODING_AGENT_LLM_BASE_URL` 注入为 `DEEPSEEK_API_KEY`/`DEEPSEEK_API_BASE_URL`。

> SWE-agent 单任务较慢（clone repo + 多轮 LLM + bash 交互），20 任务全量对比预计 1-2 小时。

## 6. 常见问题

### `docker pull swebench/...` 403 Forbidden

这是正常现象。官方镜像在 Docker Hub，国内镜像站通常只缓存公共 library 镜像。Docker 评估器会自动 fallback 到本地构建。

### conda 创建环境超时

如果构建 env image 时报 `CondaHTTPError`，说明基础镜像里的 `.condarc` 没有配置好。重新运行 `scripts/build_swe_bench_base_image.py` 即可。

### x86_64 官方镜像

如果你的 Docker daemon 运行在 x86_64 Linux 上且可以访问 Docker Hub，Docker 评估器会优先尝试拉取官方 `swebench/sweb.eval.x86_64.*` 镜像，只有在拉取失败时才会 fallback 到本地构建。
