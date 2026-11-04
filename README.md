# cqyc-askme

公开对话前台现位于 `front/`，管理端继续位于 `frontend/`（入口改为 `/admin/`）。公开问答新增 LangGraph 两路并行检索、SSE、IP 永久 20 次额度、Redis 历史与摘要、低分澄清。BGE 重排已部署，百炼 EnhancedSearch 和重排适配器均通过真实服务验证，完整问答与阈值校准仍待完成；全部启动命令见 [本机启动指南](docs/local-startup.md)，配置及验证范围见 [公开问答运行说明](docs/public-chat-operations.md)，设计见 [技术方案](docs/public-chat-design.md)。

面向求职场景的个人知识库系统。当前实现管理员登录、PDF / Markdown 导入、实时任务进度、语义分块、Milvus 写入以及公开问答代码；上传入口接受 `.pdf` 和 UTF-8 编码的 `.md` 文件，PDF 通过配置的本机 MinerU 解析。

部署时需配置 Redis、BGE-M3 和 Milvus 等服务；代码不会使用伪造解析结果或随机向量代替真实服务。新建 Markdown 导入不调用 MinerU 或图片摘要接口。

## 环境与启动

Python 由 uv 管理，依赖安装在本项目 `.venv`，开发基线 Python 3.11。前端使用 Node.js 24.14+、Vue 3、Vite、Element Plus 和 TypeScript。TypeScript 固定为 5.9.3，避免与当前 vue-tsc 不兼容。

项目根目录执行：

```bash
uv sync --locked --extra rag
cp .env.example .env
```

已有 `.env` 时不要覆盖，按照 [.env.example](.env.example) 补齐配置。登录必须填写管理员账号密码、随机 `SESSION_SECRET` 和 Redis 地址。密钥可在本机生成，不要把输出提交 Git：

```bash
uv run --extra rag python -c 'import secrets; print(secrets.token_urlsafe(48))'
```

在三个终端分别启动：

```bash
# 终端 1：API
uv run --locked --extra rag uvicorn askme.main:app --reload --host 127.0.0.1 --port 8000
```

```bash
# 终端 2：独立导入 Worker，需要 Redis 在线，只能启动一个。
uv run --locked --extra rag python -m askme.worker
```

```bash
# 终端 3：Vue 管理页面
cd frontend
npm ci
npm run dev
```

打开管理端 `http://127.0.0.1:5173/admin/`。公开前台在 `front/` 执行 `npm ci`、`npm run dev` 后访问 `http://127.0.0.1:5174/`。两套 Vite 均将 `/api` 代理到 `127.0.0.1:8000`。服务未配置时页面可展示，业务请求返回明确的配置或服务错误。

- 进程健康检查：`http://127.0.0.1:8000/api/v1/health`。
- 接口文档：`http://127.0.0.1:8000/docs`。
- 管理接口：`/api/v1/admin`，使用 HttpOnly Cookie 和 CSRF Token。

PyCharm 选择 `.venv/bin/python` 作为解释器，将 `src` 标记为 Sources Root，排除 `.uv`、`.venv`。命令行和 IDE 解释器需要保持一致。

## 服务和数据

BGE-M3 调用 Infinity 或同协议服务的 `/embeddings`；Milvus 保存向量；Redis 管理会话、队列与进度。当前 Milvus Docker 部署配套的 etcd、MinIO 仍需运行。仅处理新 Markdown 任务时，不需要启动 MinerU，也不调用 DeepSeek 图片摘要或应用侧图片桶。MD 中的图片引用不会自动抓取或识别，重要图片内容请先整理成正文或图片说明。

新上传和历史 PDF 任务均使用 MinerU 4.x V1 API；解析出图片时还需要 MinIO 图片存储和 DeepSeek 图片摘要服务。服务地址和凭据全部从环境配置读取。

原始和增强 Markdown 位于 `docs/pdfToMd/{document_id}/{ingest_id}/`，任务、源文件、阶段产物及向量批次位于 `data/imports/`。这两类目录均忽略 Git，部署时必须持久化和备份。

具体服务协议、tokenizer 准备、反向代理及当前边界见 [运行与接入说明](docs/admin-import-operations.md)。

## 依赖与构建

```bash
uv add <包名>
uv add --optional rag <导入处理依赖>
uv remove <包名>
uv sync --locked --extra rag
```

提交 `pyproject.toml`、`uv.lock`、`frontend/package.json`、`frontend/package-lock.json`；不要提交虚拟环境、node_modules、.env 或个人资料。模型服务使用各自独立环境，本项目不安装模型权重或 GPU 推理框架。

前端生产构建：

```bash
cd frontend
npm run build
```

产物为 `frontend/dist`。生产由同源反向代理托管静态页面和 `/api`，不使用 Vite 开发服务器承载生产流量。

## 目录

```text
front/                       # Vue 公开对话，根路径无需登录
frontend/                    # Vue 登录与导入工作台，/admin/ 基路径
src/askme/
  main.py                    # API 生命周期及错误处理
  worker.py                  # 独立导入 Worker
  api/routes/admin.py        # 登录、上传、SSE、任务与产物
  core/                      # 配置、错误、请求体限制
  services/                  # 认证、磁盘清单和 Redis 状态
  integrations/clients.py    # 外部服务适配
  rag/                       # LangGraph 节点与结构切分
docs/                        # 计划、技术方案与运行说明
```

背景见 [总计划](docs/development-plan.md) 与 [模块方案](docs/admin-import-design.md)。依用户要求，本阶段不创建测试用例，仅进行构建、模块加载和人工运行检查。


uv run --locked --extra rag uvicorn askme.main:app --reload --host 127.0.0.1 --port 8000
sssuv run --locked --extra rag python -m askme.worker

npm --prefix front run dev

npm --prefix frontend run dev

canddy启动，记得替换caddy对应的文件
& D:\zdsoft\caddy\caddy.exe run --config D:\zdsoft\caddy\Caddyfile --adapter caddyfile
