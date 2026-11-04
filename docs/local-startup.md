# 本机全部服务启动命令

适用环境：Windows PowerShell、Docker Desktop、当前项目目录 `D:\zdsoft\project\cqyc-askme`。以下复用本机已有的 Redis、Milvus、MinIO、BGE-M3 容器；不是在新电脑上从零安装这些服务的脚本。

更新时间：2026-10-23。只在本机开发时，启动 Docker 依赖和第 3 节四个应用终端；通过公网访问时，使用第 4 节构建产物 + Caddy + Natapp 的方式，API 和 Worker 继续运行，两个 Vite 开发服务器无需启动。

完整启动顺序：Docker Desktop → Redis / etcd / MinIO → Milvus / BGE-M3 / BGE 重排 → API / Worker → 前端构建 → Caddy → Natapp。需要 PDF 解析时增加 MinerU；Attu 仅为可选管理工具。

| 服务 / 容器 | 本机端口 | 作用与依赖 |
| --- | --- | --- |
| `redis` | `127.0.0.1:6479` → 容器 6379 | 会话、永久额度、导入队列；本机配置不要误写为 6379 |
| `milvus-etcd` | 不映射到宿主机 | Milvus 元数据，先于 Milvus 启动 |
| `milvus-minio` | 9000 / 9001 | Milvus 对象存储 / MinIO 控制台；先于 Milvus 启动 |
| `milvus-standalone` | 19530 / 9091 | 向量数据库 / 健康检查 |
| `askme-bge-m3` | `127.0.0.1:7997` | 文档和查询向量化 |
| `askme-bge-reranker` | `127.0.0.1:7998` | 检索结果重排 |
| `askme-mineru-vlm` / `askme-mineru` | 内部 30000 / 本机 8001 | PDF 解析；API 等待 VLM 就绪 |
| `attu`（可选） | 3000 | Milvus 可视化管理 |
| FastAPI / Worker | 8000 / 无监听端口 | 接口与独立导入任务消费者 |
| Caddy | `127.0.0.1:8080`；管理接口 2019 | 托管两个前端构建目录、代理 API |
| Natapp | 无固定业务监听端口 | 将公网 HTTP(S) 隧道转到本机 Caddy 8080 |

Natapp 仅转发网站入口 8080，数据库、模型和 Caddy 管理端口不作为该网站隧道目标。

## 1. 启动 Docker 和基础服务

在 PowerShell 中执行：

```powershell
cd D:\zdsoft\project\cqyc-askme
docker desktop start
docker start redis milvus-etcd milvus-minio
docker start milvus-standalone askme-bge-m3
docker compose -f deploy/reranker/compose.yaml up -d
```

`docker start` 保留已有配置、数据卷和密码；不会重新初始化数据库。Milvus 依赖 etcd、MinIO，若启动后尚未就绪，先查看 `docker logs --tail 50 milvus-standalone`，不要删除数据卷。

依赖检查命令：

```powershell
# 只检查 Redis TCP 可连接性；若 Redis 要求认证，不代表认证已经通过。
Test-NetConnection 127.0.0.1 -Port 6479 -InformationLevel Quiet
docker inspect milvus-etcd --format '{{.State.Status}} {{.State.Health.Status}}'
Invoke-WebRequest http://127.0.0.1:9000/minio/health/live
Invoke-WebRequest http://127.0.0.1:9091/healthz
```

演示成功结果：Redis 返回 `True`，etcd 为 `running healthy`，两个 HTTP 健康检查返回 200。容器刚启动时可能还未就绪；先等待，再查相应 `docker logs --tail 50 <容器名>`。容器已停止时，inspect 可能显示上次运行残留的 unhealthy，必须结合 State.Status 判断。

重排首次启动需要下载模型；已下载后复用 `askme-reranker-cache`。启动命令返回不等于模型就绪，通过以下命令检查：

```powershell
docker ps --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}"
Invoke-RestMethod http://127.0.0.1:7997/models
Invoke-RestMethod http://127.0.0.1:7998/models
```

7997 应包含 `BAAI/bge-m3`，7998 应包含 `BAAI/bge-reranker-v2-m3`。服务没准备好时暂时连接失败，需要等待下载/加载结束；出现模型加载异常时查看 `docker logs --tail 60 askme-bge-reranker`。

PDF 解析服务沿用已有 MinerU 部署，需要处理 PDF 时启动：

```powershell
docker compose -f deploy/mineru/compose.yaml up -d
Invoke-RestMethod http://127.0.0.1:8001/v1/health
```

MinerU Compose 会等待 VLM 健康后启动 API；其模型文件、CUDA 库等依赖当前电脑已有的 `deploy/mineru/runtime`，新电脑不能只复制 compose.yaml。只做文本问答时不必启动 MinerU。

Milvus 可视化管理工具按需启动，不影响业务问答：

```powershell
docker start attu
```

## 2. 首次安装或依赖变化后执行

```powershell
cd D:\zdsoft\project\cqyc-askme
uv sync --locked --extra rag
npm --prefix front ci
npm --prefix frontend ci
```

依赖已安装且未变更时可以跳过。密钥放在本地 `.env`，不要复制进前端代码。后端缓存配置，修改 `.env` 后需重启 API；如修改导入相关配置，还需重启 Worker。

## 3. 四个终端分别运行应用

终端一：FastAPI 后端（公开问答和管理后台共用）。

```powershell
cd D:\zdsoft\project\cqyc-askme
uv run --locked --extra rag uvicorn askme.main:app --reload --host 127.0.0.1 --port 8000
```

终端二：导入 Worker。上传简历、项目文档后由该进程解析并入库；只启动 API 不会执行队列中的导入任务。同一环境保持一个 Worker。

```powershell
cd D:\zdsoft\project\cqyc-askme
uv run --locked --extra rag python -m askme.worker
```

终端三：公开前台。

```powershell
cd D:\zdsoft\project\cqyc-askme
npm --prefix front run dev
```

终端四：管理后台。

```powershell
cd D:\zdsoft\project\cqyc-askme
npm --prefix frontend run dev
```

| 服务 | 访问地址 | 用途 |
| --- | --- | --- |
| 公开前台 | http://127.0.0.1:5174/ | 匿名对话 |
| 管理后台 | http://127.0.0.1:5173/admin/ | 登录、文档上传和导入状态 |
| 后端健康接口 | http://127.0.0.1:8000/api/v1/health | 仅检查 API 进程，不代表所有外部依赖可用 |
| API 文档 | http://127.0.0.1:8000/docs | 接口查看 |
| BGE-M3 | http://127.0.0.1:7997/models | 向量模型列表 |
| BGE Reranker | http://127.0.0.1:7998/models | 重排模型列表 |
| Attu（可选） | http://127.0.0.1:3000/ | Milvus 管理工具 |

百炼 EnhancedSearch 与回答 LLM 是云端服务，不需要额外本机启动命令。它们仍依赖 `.env` 中的地址、凭据和应用预算。重排使用 `RERANK_BASE_URL=http://127.0.0.1:7998`、`RERANK_MODEL=BAAI/bge-reranker-v2-m3`。

## 4. Caddy + Natapp 公网入口

本机已安装的路径：

- Caddy 程序：`D:\zdsoft\caddy\caddy.exe`。
- 生效配置：`D:\zdsoft\caddy\Caddyfile`；项目内可维护副本：`deploy/Caddyfile`。
- Natapp 程序：`D:\zdsoft\natapp\natapp.exe`。
- Natapp 现有启动脚本：`D:\zdsoft\natapp\run_natapp.bat`，已包含本机隧道凭据，直接使用，不把凭据复制进文档。

### 4.1 构建前台和管理后台

```powershell
cd D:\zdsoft\project\cqyc-askme
npm --prefix front run build
npm --prefix frontend run build
```

Caddy 托管 `front/dist` 和 `frontend/dist`。前端源码修改后必须重新构建，单独重启 Caddy 不会更新旧构建产物。

### 4.2 启动 API、Worker 和 Caddy

API 使用下面的启动命令替代第 3 节的 API 命令，不要重复占用 8000 端口。Worker 仍使用第 3 节命令。

```powershell
cd D:\zdsoft\project\cqyc-askme
uv run --locked --extra rag uvicorn askme.main:app --host 127.0.0.1 --port 8000 --proxy-headers --forwarded-allow-ips 127.0.0.1
```

在另一个 PowerShell 终端启动 Caddy：

```powershell
& D:\zdsoft\caddy\caddy.exe validate --config D:\zdsoft\caddy\Caddyfile --adapter caddyfile
# 校验成功后执行；保持该终端运行。
& D:\zdsoft\caddy\caddy.exe run --config D:\zdsoft\caddy\Caddyfile --adapter caddyfile
```

当前路由：

| 路径 | 转发 / 托管目标 |
| --- | --- |
| `/` 及公开前端路由 | `D:/zdsoft/project/cqyc-askme/front/dist` |
| `/admin` | 308 跳转 `/admin/` |
| `/admin/*` | 去掉 `/admin` 前缀后读取 `frontend/dist`，SPA 路由回退管理端 index.html |
| `/api/*` | 保留完整路径代理至 `127.0.0.1:8000`，SSE 立即刷新 |

配置调整后，先验证，再在另一个终端平滑重载正在运行的 Caddy：

```powershell
& D:\zdsoft\caddy\caddy.exe validate --config D:\zdsoft\caddy\Caddyfile --adapter caddyfile
& D:\zdsoft\caddy\caddy.exe reload --config D:\zdsoft\caddy\Caddyfile --adapter caddyfile
```

`reload` 要求 Caddy 已运行；未运行时使用 `run`。运行日志位于 `D:\zdsoft\caddy\logs\runtime.log`。项目内副本与实际文件不会自动同步，后续编辑时注意同步。

### 4.3 启动 Natapp

先在 Natapp 控制台核对该脚本对应隧道的本地目标为 `127.0.0.1:8080`，公网 HTTPS 在 Natapp 侧配置。脚本本身只含隧道凭据，不能从中确认云端域名、目标端口或证书状态。

另开一个 PowerShell 终端：

```powershell
cd D:\zdsoft\natapp
& .\run_natapp.bat
```

保持该终端运行，以 Natapp 实际显示的在线状态和公网地址为准。不要同时重复运行同一隧道脚本，也无需额外传 `-authtoken`。

本地 `.env` 当前允许来源包含 `https://askme.cqycaskeme.asia`；这只是应用允许来源记录，不代表 Natapp 当前云端绑定已核验。如果隧道地址不同或变化，把实际协议 + 域名加入 `ALLOWED_ORIGINS` 并保留原本开发来源，然后重启 API，否则提交问题/登录可能返回 403。公网 HTTPS 部署时应设置 `COOKIE_SECURE=true`；该设置也意味着普通 HTTP 下不能依赖管理会话 Cookie，应使用 HTTPS 管理入口。

### 4.4 联通性与真实 IP 检查

先本地，再公网逐层验证：

```powershell
Invoke-WebRequest http://127.0.0.1:8080/
Invoke-WebRequest http://127.0.0.1:8080/admin/
Invoke-RestMethod http://127.0.0.1:8080/api/v1/health
```

前两项应分别显示公开前台和管理端，第三项应是后端 JSON，不能是前端 HTML。然后通过 Natapp 实际公网地址访问同样路径。首页可打开而接口 502 时优先检查 API；接口 403 时检查来源配置；本地可用但公网失败时检查 Natapp 在线状态、云端隧道目标与证书。

真实 IP 链路为：Natapp 提供 `X-Real-Ip` → Caddy 仅信任本机代理并解析 `client_ip` → 覆盖后端 `X-Forwarded-For` → Uvicorn 仅信任 127.0.0.1 → 应用按解析后的 IP 生成匿名身份。这个信任边界也信任本机进程，8080 和 8000 均保持回环监听；不要将可信代理设为 `*`。

上线前需用不同公网 IP 的网络分别访问，确认历史与额度不会错误合并；同一个公网 IP 共享则是既定规则。这里不通过日志打印原始 IP 或完整请求头。当前文档不把本地代理验证当作真实公网验证。

参考：[Caddy 官方命令说明](https://caddyserver.com/docs/command-line)、[Natapp 官方访客真实 IP 说明](https://natapp.cn/article/client_real_ip)。

## 5. 停止与下次启动

四个应用终端分别按 `Ctrl+C`。按需停止重排容器：

```powershell
docker compose -f deploy/reranker/compose.yaml stop
```

下次仍用 `docker compose -f deploy/reranker/compose.yaml up -d` 启动。不要为日常停止添加 `down -v`，该参数会删除本 Compose 的模型缓存卷并导致重新下载。数据库与 Redis 数据卷同样需要保留，尤其 Redis 中保存了永久提问额度。

关闭公网入口时，先在 Natapp 终端按 `Ctrl+C`，再在 Caddy 终端按 `Ctrl+C`，最后停止 API 与 Worker。只需更新 Caddy 配置时使用 reload，无需关闭隧道。

需要停止本项目全部 Docker 依赖时，在应用进程停止后执行：

```powershell
cd D:\zdsoft\project\cqyc-askme
docker compose -f deploy/mineru/compose.yaml stop
docker compose -f deploy/reranker/compose.yaml stop
docker stop attu askme-bge-m3 milvus-standalone
docker stop milvus-minio milvus-etcd redis
```

上述是当前电脑的实际容器名。如果其他项目共用这些数据库容器，先确认它们也已停止访问再执行；不执行 Docker 全局停止或清理命令。

## 6. 验证范围

模型部署、真实 `/rerank` 评分、项目重排适配器验证记录见 [公开问答运行说明](public-chat-operations.md)。完整回答质量和阈值校准需要另行完成，不能把模型启动成功视作端到端验收完成。

2026-10-23 已检查本机容器端口、Caddy 2.11.7 命令及 Natapp 现有启动脚本；更新实际 Caddyfile 前已备份到 `D:\zdsoft\caddy\Caddyfile.backup-20261023-152711`。新配置通过 `caddy validate`，临时本机运行验证了 `/`、`/admin/`、`/admin/login` 对应正确构建文件，以及 `/admin` 的 308 跳转。

使用临时演示上游验证 `/api/` 原路径保留、演示 X-Real-Ip 正确传递并覆盖外来 XFF、没有 X-Real-Ip 时不会采信外来 XFF。该演示没有连接真实业务 API、没有产生提问或扣次；验证后临时 Caddy 和演示上游均停止。没有启动 Natapp，也没有验证公网域名、证书或隧道在线状态。
