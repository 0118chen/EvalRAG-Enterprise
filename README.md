# EvalRAG Enterprise

评测驱动的多租户企业 RAG 平台，面向政务、金融和合规文档问答。项目采用 FastAPI、Vue 3、PostgreSQL、Redis 和 Celery，支持 SQLite 本地开发及 PostgreSQL 生产运行。

## 已实现能力

### 阶段一：产品闭环

- 多租户知识库和文档管理。
- PDF、DOCX、TXT、Markdown 异步解析与结构化分块。
- 文档处理进度、失败原因和版本管理。
- Hybrid、Dense、Sparse 检索。
- SSE 流式回答、页码和分数引用、用户反馈。
- Vue 工作台覆盖知识库、文档、问答和运行诊断。

### 阶段二：统一 RAG Trace

- HTTP、RAG、Query Rewrite、Retrieval、Rerank、Generation、Ingestion、Evaluation 和 Dataset 同步 Span。
- 每个请求返回 `X-Request-ID` 与 `X-Trace-ID`。
- Span 保留父子关系、耗时、输入输出摘要和错误信息。
- LangSmith 不可用时自动降级为本地 Trace 记录，不阻塞主业务。
- 手机号、身份证号和租户标识在 Trace metadata 中脱敏或哈希处理。

### 阶段三：离线评测

- 持久化评测数据集，每个样例包含问题、期望文档、页码和类别。
- 本地 Experiment 计算 Recall@K、Precision@K、MRR、nDCG@K 和页码命中率。
- Celery 异步执行实验，结果保存到数据库。
- 可选同步 Dataset 到 LangSmith，并使用 LangSmith `evaluate` 执行 Experiment。
- CLI 支持运行实验和同步 Dataset。

```bash
python -m app.cli run-evaluation <evaluation_id>
python -m app.cli sync-dataset <dataset_id> <tenant_id>
python -m app.cli check-llm
python -m app.cli check-langsmith
python -m app.cli reindex-document <document_id> --force
```

运行中的 API 还提供真实连通检查：

```bash
curl http://localhost:8000/health/llm
curl http://localhost:8000/health/langsmith
```

### 阶段四：评测 API 与前端

- Dataset 创建、列表和详情接口。
- Evaluation 创建、状态、结果和基线对比接口。
- 前端支持 Dataset 编辑、实验配置、异步进度、指标卡片、逐题结果和基线差值。

### 阶段五：企业级检索

- Dense/Sparse 并行检索与 RRF 融合。
- 可插拔 Query Rewrite，本地默认使用规则扩展。
- 可插拔 Reranker，本地默认使用词项覆盖与融合分数重排。
- 支持 Milvus 和 Elasticsearch 适配边界。
- 文档和 Chunk 版本过滤。
- Memory/Redis 检索缓存。
- Retriever、Reranker 和缓存故障时自动降级。

### 阶段六：公网部署

- PostgreSQL 运行时存储与 Alembic 迁移。
- Redis 缓存、限流和 Celery Broker。
- API Key 认证和租户绑定。
- 固定窗口限流，支持 Memory/Redis 后端。
- Nginx HTTPS、HSTS 和反向代理配置。
- Staging/Production Compose、GitHub Actions CI/CD。
- PostgreSQL 备份与恢复脚本。
- `/health/live`、`/health/ready` 和 Prometheus `/metrics`。

## 本地开发

创建后端环境：

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
alembic upgrade head
uvicorn app.main:app --reload
```

Windows PowerShell：

```powershell
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
alembic upgrade head
uvicorn app.main:app --reload
```

启动前端：

```bash
cd frontend
npm install
npm run dev
```

本地开发默认使用 SQLite、`hash` embedding、`local` dense/sparse 检索，并允许在 Celery 不可用时以内联后台任务运行评测。上传文档仍需要 Redis 和 Celery Worker。

## Docker Compose

开发环境：

```bash
cp .env.example .env
docker compose up --build
```

- 前端：`http://localhost:8080`
- API 文档：`http://localhost:8000/docs`

Compose 会先执行 `alembic upgrade head`，然后启动 API 和 Worker。

PowerShell 可覆盖宿主机端口：

```powershell
$env:API_PORT="18000"
$env:FRONTEND_PORT="18080"
docker compose -p evalrag-integration up --build -d
```

## Staging 与 Production

复制并填写环境变量：

```bash
cp .env.staging.example .env.staging
cp .env.production.example .env.production
```

Staging：

```bash
POSTGRES_PASSWORD=replace-me \
docker compose --env-file .env.staging \
  -f deploy/docker-compose.staging.yml up --build -d
```

Production：

1. 将 TLS 证书放入 `deploy/certs/fullchain.pem` 和 `deploy/certs/privkey.pem`。
2. 在 `.env.production` 中配置强随机 `API_KEYS`、LLM/embedding 密钥和域名。
3. 执行部署：

```bash
POSTGRES_PASSWORD=replace-me \
docker compose --env-file .env.production \
  -f deploy/docker-compose.production.yml up --build -d
```

生产环境必须设置：

```text
APP_ENV=production
AUTH_ENABLED=true
API_KEYS={"long-random-key":"tenant-id"}
RATE_LIMIT_ENABLED=true
CACHE_BACKEND=redis
RATE_LIMIT_BACKEND=redis
DENSE_RETRIEVAL_BACKEND=milvus
SPARSE_RETRIEVAL_BACKEND=elasticsearch
EMBEDDING_PROVIDER=openai-compatible
```

Production Compose **不会**创建 Milvus 或 Elasticsearch。生产部署必须另外提供 API/Worker 均可访问的 Milvus、Elasticsearch 和 OpenAI-compatible embedding 服务，并设置对应 URI、认证、collection/index、模型及 `EMBEDDING_DIMENSIONS`。首次切换后端、embedding 模型或维度后，对每个已 ready 文档执行 `python -m app.cli reindex-document <document_id> --force`；该命令先按文档删除外部旧索引，再写入新索引。建议生产保持 `EXTERNAL_RETRIEVAL_FALLBACK=false`，避免外部后端配置/schema/auth 错误被本地结果掩盖；若显式开启，只有连接/超时类不可用错误会降级。

## 备份与恢复

Linux/macOS：

```bash
./scripts/backup_postgres.sh
./scripts/restore_postgres.sh backups/evalrag-YYYYmmdd-HHMMSS.sql
```

Windows PowerShell：

```powershell
.\scripts\backup_postgres.ps1
.\scripts\restore_postgres.ps1 -BackupFile .\backups\evalrag-YYYYmmdd-HHMMSS.sql
```

生产环境应把备份目录同步到独立对象存储，并定期执行恢复演练。

## 验证

```bash
ruff check app tests alembic
pytest -q
cd frontend && npm run build
```

LangSmith 仅发送脱敏 metadata、问题文本、文档 ID、页码和受控摘要，不上传完整原文。
