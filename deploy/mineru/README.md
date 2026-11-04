# MinerU 本机 Docker 部署

本配置用于 Windows + Docker Desktop（Linux 容器）+ NVIDIA GPU。MinerU 4.0.12 提供项目使用的 V1 tasks 接口；量化 VLM 使用 llama.cpp b11669 的 CUDA 12.8 构建。模型、CUDA 库、下载缓存和解析结果挂载到本目录下的 `runtime/`，随项目位于 D 盘，不提交 Git。

## 已安装环境的日常操作

在项目根目录 `D:\zdsoft\project\cqyc-askme` 执行：

```powershell
# 同时启动 API 和 VLM；等待 VLM 健康后才启动 API。
docker compose -f deploy/mineru/compose.yaml up -d
docker compose -f deploy/mineru/compose.yaml ps
Invoke-RestMethod http://127.0.0.1:8001/v1/health

# 查看解析任务日志或模型推理日志。
docker compose -f deploy/mineru/compose.yaml logs --tail 100 mineru-api
docker compose -f deploy/mineru/compose.yaml logs --tail 100 mineru-vlm

# 不处理 PDF 时停止这两个容器，释放 VLM 显存；保留模型和解析产物。
docker compose -f deploy/mineru/compose.yaml stop
```

项目 `.env` 对应配置：

```dotenv
MINERU_BASE_URL=http://127.0.0.1:8001
MINERU_API_KEY=
MINERU_TIER=standard
MINERU_TIMEOUT_SECONDS=1800
```

API 仅绑定宿主机回环地址；这里没有启用鉴权。`mineru-vlm:30000` 仅供 Compose 内网访问。宿主机运行的 Worker 使用 `127.0.0.1:8001`，如果以后把 Worker 也放进 Docker，需要按实际容器网络修改地址。MinerU 重启会丢失内存中的任务索引，正在执行的导入应通过项目界面显式重试。

## 共享显存的配置含义

调用顺序：项目导入 Worker → MinerU API → CPU 布局/OCR/表格小模型 + CUDA VLM → Markdown/图片/结构信息 → 项目后续分块及 BGE-M3 向量化。

- `mineru-api` 不申请 GPU，小模型固定使用 ONNX CPU 后端。
- `mineru-vlm` 使用 Q8_0 量化权重，单个 8192 token 上下文槽位。
- API 文档并发 `--concurrency 1`，文档内 VLM 并发 `--vlm-max-concurrency 1`，推理服务 `--parallel 1`。例如同时提交两个演示 PDF，第二个在 API 中排队，不同时扩展两份模型推理缓存。
- GPU 设备声明只是允许访问 GPU，不构成显存硬上限。BGE-M3 的长文本批次、其他 GPU 软件仍会增加总显存占用；调大上述参数之前应观察真实峰值。
- `--special` 保留模型输出中的版面特殊 token；不能因输出包含特殊符号就删除该选项。`--grammar "root ::= .*"` 与 MinerU 官方封装器保持一致。

```powershell
# 持续查看整张显卡的占用；Ctrl+C 结束观察。
nvidia-smi --query-gpu=memory.total,memory.used,memory.free,utilization.gpu --format=csv -l 1
```

遇到 CUDA OOM 时先停止当前导入并检查其他 GPU 程序；保持 MinerU 并发为 1，可减少 BGE 服务的批量大小。不要用增加 Docker 普通内存限制来解决显存不足；RAM 和 GPU VRAM 是不同资源。

## 从零重建所需文件

`compose.yaml` 会直接加载以下文件，只有构建镜像还不能启动服务：

```text
runtime/
  llama/llama-server             # 以及同压缩包的共享库
  cuda/libcudart.so.12           # 以及 libcublas 等 CUDA 共享库
  models/MinerU-4_models_onnx/
  models/MinerU2.5-Pro-2605-1.2B-GGUF/
    MinerU2.5-Pro-2605-1.2B-Q8_0.gguf
    mmproj-MinerU2.5-Pro-2605-1.2B-Q8_0.gguf
```

llama.cpp 文件来自 [官方 b11669 发布页](https://github.com/ggml-org/llama.cpp/releases/tag/b11669)，固定文件及 SHA256：

| 文件 | SHA256 |
| --- | --- |
| `llama-b11669-bin-ubuntu-cuda-12.8-x64.tar.gz` | `4703ed1a3cd6e8504affb6cb9866f501e96d634d68a002dc9a8251c9e2819b49` |
| `cudart-llama-b11669-bin-ubuntu-cuda-12.8-x64.tar.gz` | `ab3eda89a82096bcba00b6448218c3c11eec2016b0f38d64884dc29736549f26` |

下载到 `runtime/downloads/` 后，先用 `Get-FileHash -Algorithm SHA256` 核对，再在 Linux 容器中用 `tar --strip-components=1` 分别解压到 `runtime/llama`、`runtime/cuda`，保留 Linux 可执行权限和库符号链接。不要以 Windows 解压工具替代这一步。

`Dockerfile` 中的 Ubuntu 镜像固定到 digest。`ca-certificates.crt` 是从本机已有 Ubuntu 容器导出的公共 CA 证书集合，用于最小基础系统首次通过 HTTPS 安装 CA 包；构建过程仍校验证书和软件仓库签名。

```powershell
# 构建轻量 Python/ONNX 镜像；不安装 Torch 或 vLLM。
docker compose -f deploy/mineru/compose.yaml build mineru-vlm

# 官方下载器准备所需模型，并生成完整性标记；已有文件会按下载器规则复用。
docker compose -f deploy/mineru/compose.yaml run --rm --no-deps --entrypoint mineru-kit mineru-vlm models download --tier standard --small-backend onnx --vlm-engine llama-cpp --source huggingface
docker compose -f deploy/mineru/compose.yaml run --rm --no-deps --entrypoint mineru-kit mineru-vlm models verify --tier standard --small-backend onnx --vlm-engine llama-cpp
```

模型仓库分别是 [MinerU ONNX 模型](https://huggingface.co/opendatalab/MinerU-4_models_onnx) 和 [MinerU GGUF 模型](https://huggingface.co/jinzhenj/MinerU2.5-Pro-2605-1.2B-GGUF)。不要手工伪造 `.mineru_complete` 标记；应由官方模型下载器完成校验和标记。

Docker Desktop 的镜像存储位置与上述模型挂载目录相互独立。2026-10-21 已将本机完整 WSL 数据目录迁移到 `D:\docker-data\docker-desktop`，其中 `disk/docker_data.vhdx` 保存镜像、容器、命名卷及构建缓存，`main/ext4.vhdx` 为 Docker 的 WSL 系统磁盘。

本机通过 Windows 目录联接保留兼容访问路径：`C:\Users\cqyc\AppData\Local\Docker\wsl` → `D:\docker-data\docker-desktop`。因此部分工具仍显示 C 盘逻辑路径，但虚拟磁盘实际占用 D 盘。迁移前后两个磁盘文件均通过 SHA256 校验，容器 ID、镜像、卷和挂载保持一致；C 盘旧副本已在服务验证通过后删除。迁移清单及校验记录位于 `D:\docker-data\migration-20261021`。

MinerU 的 `runtime/` 仍挂载项目内原有的 D 盘目录，不需更改本页启动命令。Docker 程序及少量用户配置仍在 C 盘；后续拉取镜像、创建命名卷、构建镜像的主要数据增长发生在 D 盘。

## 本次安装的验证记录（2026-10-21）

以下为本机实际验证结果，输入均为一页合成演示 PDF 和合成向量化文本，不涉及业务文档或数据库写入。

- 两个容器均为 `healthy`，`GET /v1/health` 返回 `status=ok`、`version=4.0.12`。
- 官方 `models verify` 检查 ONNX、GGUF 两个模型仓库均为 `ok`。10 个大型模型文件的 SHA256 与 Hugging Face 下载元数据中的校验值一致。
- 使用项目 `askme.integrations.clients.MinerU.parse` 完成 standard 请求、任务轮询和 ZIP 解包，约 4.27 秒返回 606 字符 Markdown，并包含图片及 middle JSON；演示标题、表格、服务名称完整。
- 为确认视觉推理确实执行，额外使用仅对该请求生效的 advanced 档位验证：约 4.86 秒完成，VLM 日志记录了实际 prompt/decode；同期 BGE-M3 成功完成 6 批、每批 16 条的 1024 维向量请求。项目 `.env` 仍使用 standard。
- 上述视觉推理与 BGE 并发时，每约 0.25 秒采样一次，共 17 次；整卡观测峰值为 **4903 MiB（约 4.79 GiB）**，最低空闲 **11205 MiB（约 10.94 GiB）**。数据包含桌面及其他 GPU 程序占用，且只是该演示工作负载的采样结果，不代表所有复杂 PDF 的上限。

原始结果保存在本机忽略目录：

```text
runtime/verification/adapter-1792566966/report.json
runtime/verification/adapter-1792566966/result.md
runtime/verification/visual-1792567052/report.json
runtime/verification/visual-1792567052/result.md
```

本次模型下载版本：ONNX 仓库 `358310b4f64b95f9fefc372ad899356e4111f376`，GGUF 仓库 `9185688a0495e1577d521a757c7c0b62dd38ca48`。以后显式重新下载模型可能获得新版本，应重新进行解析验证。
