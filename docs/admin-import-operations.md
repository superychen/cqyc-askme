# 管理员登录和文档导入运行说明

## 1. 实现状态与验证范围

本模块已实现 Vue 管理页面、FastAPI 认证和导入接口、Redis Streams Worker、LangGraph 导入节点、图片增强、BGE-M3 token 分块及向量写入代码。当前没有本地外部服务，不声称已完成真实文档端到端导入验收。

本次不部署中间件、不下载模型权重、不创建测试用例。模块导入、Python 编译、OpenAPI 生成、前端类型检查和构建可以独立检查。登录成功路径、队列消费、PDF 解析、图片读写和真实入库，需要下述服务在线后验证。

## 2. 云端服务契约

使用部署时的最新稳定服务可以，但“最新版”不保证永远保持接口不变。每次部署记录实际版本，先核对 OpenAPI 再联调。客户端依赖由 uv.lock 与 package-lock.json 固定，不能直接用远程升级代替兼容性检查。

### 2.1 MinerU

适配 MinerU 4 自部署 V1 便捷任务接口：

- `POST {MINERU_BASE_URL}/v1/tasks`：原始 PDF 字节，查询参数携带文件名、tier 和 `output_formats`，请求 ZIP、Markdown 和 middle JSON。
- 保存返回的 `task_id`；通过 `GET /v1/tasks/{task_id}/result?response_format=zip` 读取结果。202 表示继续等待，200 表示下载结果包。
- ZIP 必须包含一个完整 `.md` 和图片资源，部分成功不能标记为成功导入。
- API Key 使用 Bearer 鉴权，不跟随任意跨域跳转，不将凭据传给对象存储链接。

地址为 `/v1` 之前的服务根路径。MinerU 重启可能使旧任务 ID 失效，请求被拒绝时清除保存的 ID，后续显式重试重新提交。

参考 [MinerU V1 HTTP API](https://opendatalab.github.io/MinerU/usage/http_api/)。旧版本根路径 `/file_parse` 不属于本适配器支持的协议。

### 2.2 BGE-M3

推荐独立部署 [Infinity](https://github.com/michaelfeil/infinity)，使用 BAAI/bge-m3 稠密向量能力。请求约定：

```text
POST {EMBEDDING_BASE_URL}/embeddings
Authorization: Bearer <配置后携带，不要写入日志>
Content-Type: application/json

{"model":"BAAI/bge-m3","input":["文档分块文本"]}
```

返回 `data` 列表，每条包含 `index` 与 1024 维 `embedding`。若服务设置了 `/v1` 前缀，在 BASE_URL 中包含 `/v1`。后端按返回索引排序，校验条数、维度、有限数值和非零范数，并 L2 归一化。

服务和客户端必须使用同一模型 revision 的 tokenizer。服务端禁止静默截断；客户端默认每块最多 1000 tokens，包含标题与特殊 token。不要把同维度的不同模型向量放到一个集合。

### 2.3 准备 tokenizer

项目只需要轻量 `tokenizer.json`，不加载 BGE-M3 权重。默认 `TOKENIZER_LOCAL_ONLY=true`，Worker 不会在任务中自动下载文件。未来服务器可以由你显式执行：

```bash
uv run --extra rag hf download BAAI/bge-m3 tokenizer.json --local-dir data/models/bge-m3
```

生产建议选择固定模型 commit，下载时增加 `--revision <模型提交号>`，并把 `.env` 中 `EMBEDDING_REVISION` 设为同一提交号。文件就位后设置：

```dotenv
TOKENIZER_PATH=data/models/bge-m3/tokenizer.json
TOKENIZER_LOCAL_ONLY=true
```

也可同步模型服务使用的 tokenizer 文件。缺少文件时任务明确失败，不会退回字符数近似或伪造 token 数。

### 2.4 MinIO、Milvus 与 Redis

MinIO endpoint 不带协议前缀，TLS 由 `MINIO_SECURE` 控制。提前创建私有桶并授予服务账号最小读写权限；程序不修改桶公开策略或自动创建桶。

Milvus 使用项目专用集合。首次写入创建 HNSW / COSINE 索引及 1024 维 schema，已有不兼容集合会报错，不删除重建。服务端应与项目锁定的 PyMilvus 兼容，升级时单独验证。

Redis 建议开启 AOF 和持久化卷。不要与随意淘汰键的临时缓存混用，会话、队列、状态与提交登记都是业务数据。SSE 保留最近约 1000 条事件，连接时先发完整快照，页面不依赖历史事件重建状态。

## 3. 管理员配置

在根目录 `.env` 填写管理员账号密码和至少 32 字符的随机 `SESSION_SECRET`。密码不存数据库、不写日志；Redis 保存会话摘要，Cookie 为 HttpOnly。更换账号、密码或会话密钥后需重启 API，旧会话将失效。

本地 HTTP 设置 `COOKIE_SECURE=false`。生产 HTTPS 设置 `APP_ENV=production`、`COOKIE_SECURE=true`，`ALLOWED_ORIGINS` 填实际浏览器访问 Origin，包括非标准端口且不带路径，例如 `https://knowledge.example.com`。不使用通配符 CORS。

会话默认 8 小时，退出后失效。浏览器通过同源代理访问，修改状态需要会话绑定的 `X-CSRF-Token`。直接调用登录 API 时也必须携带允许的 Origin。

## 4. 运行命令

```bash
uv sync --locked --extra rag
uv run --locked --extra rag uvicorn askme.main:app --host 127.0.0.1 --port 8000
```

单独终端启动 Worker：

```bash
uv run --locked --extra rag python -m askme.worker
```

前端在 `frontend` 下执行 `npm ci` 和 `npm run dev`；生产执行 `npm run build`，托管生成的 `dist`。

没有 Worker 时文件可入队，但页面将持续显示排队中。Redis 不在线时登录和导入拒绝处理，不退回内存队列。健康检查只表示 Web 进程存活，不代表模型、向量库或 Worker 已就绪。

## 5. 同源反向代理示例

以下为服务器对应 location 的示例，TLS 由部署环境配置，不是本机部署命令：

```nginx
root /srv/cqyc-askme/frontend/dist;
client_max_body_size 22m;

location /api/ {
    proxy_pass http://127.0.0.1:8000;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_http_version 1.1;
    proxy_buffering off;
    proxy_read_timeout 3600s;
}

location / {
    try_files $uri $uri/ /index.html;
}
```

公网域名需要 HTTPS，并应限制 API 文档访问。Redis、模型服务、MinIO 控制台和 Milvus 优先通过云内网访问。

## 6. 数据、重试与恢复

- `docs/pdfToMd/{document_id}/{ingest_id}/original.md`：解析原文；同目录 `enriched.md` 保存图片增强结果。
- `data/imports/{task_id}`：源文件、任务清单、阶段标记、结构结果、图片映射、分块及向量批次。
- MinIO 私有桶：图片字节。
- Redis `askme:committed`：已完成校验的版本，未来公开检索必须按此登记过滤。

备份覆盖这些目录及外部服务持久化数据。导出的 Markdown 图片使用管理员鉴权代理地址，离开本应用不会自动变成独立的离线图片包。

相同文件哈希、扩展名和流水线指纹复用已有任务，失败时可点击重试。重试不对完成阶段重复调用模型；chunk 主键稳定，重复 upsert 不产生额外实体。改变模型、切分参数或批大小后不能直接续跑旧任务，需要重新上传创建新版本。

Worker 通过全局租约保持单实例，每 10 秒续期，失去执行权时停止推进。重启时对账磁盘清单，恢复完成记录，继续排队/处理中任务。部分写入的失败版本不进入可用登记，不能被未来的公开问答直接查询。

## 7. 当前边界

- 外部服务尚未部署，跨服务成功路径、恢复语义及实际返回仍需云端联调验收。
- 默认单文件 20 MiB、PDF 200 页，单 Worker 顺序处理；图片也顺序识别。
- MD 必须为 UTF-8，不抓取远程图片或读取服务器任意路径；页面给出图片未增强提示。
- 保留标题树、正文行号、图片 ID 和 MinerU 原始结构 JSON；当前不猜测 PDF 页码，chunk 的 `page_numbers=null`。精确逐页引用需后续依据实际 MinerU 产物补充。
- Markdown 表格拆分重复表头，极长行继续递归切分；代码拆分重复围栏，结构拆分写入元数据。图片摘要进入向量文本。
- PDF 图片引用无法匹配时任务失败，避免静默漏图；未知格式需按真实产物调整适配器。
- 尚未实现覆盖、删除、取消任务、公开问答或 20 次额度。
- 按用户要求没有编写自动化测试，构建和无依赖启动不能替代真实服务联调。
