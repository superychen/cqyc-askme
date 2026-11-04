# MinerU 独立服务镜像；不向 cqyc-askme 的 .venv 安装模型推理依赖。
# Ubuntu 24.04 的 glibc 与官方 llama.cpp CUDA 预编译程序兼容。
# 仅使用轻量基础系统，CUDA 库和模型挂载在 D 盘，避免大型 vLLM 镜像占满 C 盘。
FROM ubuntu@sha256:68ee22004a9109b44973313b02ff7b1de250f1de0f5ecb628d73f579aaff33ac

# 官方 Ubuntu 最小镜像尚未安装 CA；先使用现有 Ubuntu 镜像导出的公共 CA。
# 软件源改用 HTTPS，避免当前网络代理拒绝明文 HTTP；不关闭证书或签名校验。
COPY ca-certificates.crt /etc/ssl/certs/ca-certificates.crt
# OpenCV 读取 PDF 页面图像需要这些系统库；不把 apt 缓存留在镜像层中。
RUN sed -i 's|http://|https://|g' /etc/apt/sources.list.d/ubuntu.sources \
    && apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends python3 python3-venv ca-certificates libgl1 libglib2.0-0t64 libgomp1 libstdc++6 libssl3t64 \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# 固定已核对 V1 tasks 协议的发布版本。基础包提供 CPU ONNX 小模型推理，
# 不安装 Torch/vLLM；VLM 通过内网调用下面单独配置的 llama.cpp CUDA 服务。
# 权重在部署后显式下载到 D 盘挂载目录，不在构建时复制进系统盘镜像。
RUN python3 -m venv /opt/mineru \
    && /opt/mineru/bin/pip install --no-cache-dir uv \
    && /opt/mineru/bin/uv pip install --no-cache --python /opt/mineru/bin/python "mineru==4.0.12"

ENV PATH=/opt/mineru/bin:$PATH \
    MINERU_HOME=/var/lib/mineru \
    MINERU_MODEL_SOURCE=local \
    MINERU_MODEL_SMALL_BACKEND=onnx \
    MINERU_MODEL_VLM_ENGINE=llama-cpp \
    HF_HOME=/var/lib/mineru/cache/huggingface \
    HF_HUB_DISABLE_TELEMETRY=1 \
    DO_NOT_TRACK=1 \
    PYTHONUNBUFFERED=1

ENTRYPOINT ["mineru-kit"]
