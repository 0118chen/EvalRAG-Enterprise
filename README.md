# EvalRAG Enterprise

评测驱动的多租户企业 RAG 平台，面向政务、金融和合规文档问答。项目采用 FastAPI、Vue 3、PostgreSQL、Redis 和 Celery，支持 SQLite 本地开发及 PostgreSQL 生产运行。

## 已实现能力

### 阶段一：产品闭环

- 多租户知识库和文档管理。
- PDF、DOCX、HTML、XLSX、TXT、Markdown 异步解析与结构化分块（网页公文走正文抽取去掉导航与页脚样板；表格按行序列化成自描述文本，不丢列语义）。
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

- 持久化评测数据集，每个样例包含问题、期望文档、页码、证据引文和类别。
- 本地 Experiment 一次检索同时计算 Recall@{1,3,5}、Precision@K、MRR、nDCG@{3,5} 和页码命中率，
  并给出 `latency_ms_p50/p95`（同一排名算多个截断点，避免重跑导致的候选池变化）。
- 带证据引文的样例额外计算 passage 级指标（`passage_hit`/`passage_at_1`/`passage_mrr` 与每题 `passage_rank`）：
  文档级指标在少量文档上会饱和，passage 级才能看出"答段排在第几"。
- 支持多跳样例（`expected_evidence`：每题一跳一个引文，另计严格的 `all_targets@k`）与应拒答样例
  （`should_refuse`：不参与检索指标，单独报 `negative_retrieved_rate`）。
- Celery 异步执行实验，结果保存到数据库；每条 retrieved 记录带 `chunk_id` 与 `text`，结果可复核。
- 可选同步 Dataset 到 LangSmith，并使用 LangSmith `evaluate` 执行 Experiment。
- CLI 支持运行实验和同步 Dataset。

仓库自带两套评测集与六组配置的实测结果。

v1（13 份法规、52 道页面级问题）：

```bash
python -m scripts.audit_golden_set --json docs/evaluation/golden-set-audit.json   # 标注回验（含 v2 段）
python -m scripts.run_golden_experiment --json docs/evaluation/run.json           # 六组配置对比
python -m scripts.profile_retrieval --database data/experiments/golden.db         # 延迟归因
python -m scripts.probe_golden_difficulty --database data/experiments/golden.db   # 饱和成因探针
```

v2（75 题：52 同义改写 + 9 跨文档多跳 + 6 等价多标签 + 8 应拒答）：

```bash
# 重新生成需要 LLM key；校验失败的题会被丢弃并把原因写进 law/golden_eval_v2.rejected.json
python -m scripts.generate_golden_set_v2 --output law/golden_eval_v2.json
python -m scripts.run_golden_experiment --golden law/golden_eval_v2.json \
    --json docs/evaluation/golden-set-v2.json --database data/experiments/golden-v2.db
```

结论与"能写/不能写"的边界见 [`docs/evaluation-report.md`](docs/evaluation-report.md)：
v1 标注 52/52 可回验，但指标已饱和（BM25+重排在文档级指标上满分），
且本地 32 维 hash 向量使 hybrid 反而低于纯 BM25；v2 去掉"问题里报法规名"的词面泄漏后
指标明显下降，两种题集的对照见该报告的 §8。

```bash
python -m app.cli run-evaluation <evaluation_id>
python -m app.cli sync-dataset <dataset_id> <tenant_id>
python -m app.cli check-llm
python -m app.cli check-langsmith
python -m app.cli reindex-document <document_id> --force
```

运行中的 API 还提供真实连通检查（`APP_ENV=development` 时可直接访问，其他环境必须带管理令牌）：

```bash
curl http://localhost:8000/health/llm
curl http://localhost:8000/health/langsmith
curl -H "X-Health-Token: $HEALTH_ADMIN_TOKEN" https://your-host/health/llm
```

这两个端点会真实调用外部付费服务，因此非开发环境必须配置 `HEALTH_ADMIN_TOKEN`（未配置时直接返回 503 而不是放行），并且与其他路径一样计入限流；`/health/live`、`/health/ready` 和 `/metrics` 保持免限流的廉价探针。

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
- 外部连通检查（`/health/llm`、`/health/langsmith`）需要管理令牌，非开发环境未配置令牌时直接失败。
- LangSmith Dataset 远端名称按租户命名空间隔离。

## 支持的文档格式

| 文件类型 | 处理方式 |
|---|---|
| `.pdf` | PyMuPDF 逐页抽取文本，页级分块，引用里带真实页码 |
| `.docx` | python-docx 抽取段落；抽取结果只有一个逻辑页（页码恒为 1），所以 DOCX 的页级指标实际是文档级 |
| `.html` / `.htm` | 标准库 `html.parser` 抽正文：丢弃 `script/style/nav/header/footer/aside` 等元素、按 class/id 关键词丢掉站点框架，再按行过滤 `版权所有`、`ICP备` 这类样板行；抽取结果同样只有一个逻辑页（页码恒为 1） |
| `.xlsx` | openpyxl 逐工作表抽取，**页码 = 工作表序号**；每行序列化成自描述文本（`省辖市: 郑州市 \| 市县区: 巩义市`），表头是第一个非空单元格 ≥2 的行，其上方的标题行（`附件1`、`102个县（市）名单`）作为正文保留 |
| `.txt` / `.md` | 先按 UTF-8 解码（带 BOM 时去掉 BOM），失败再按 GB18030/GBK 解码；两者都不是时带替换字符解码，而不是让整个文档失败 |
| 其他（`.doc`、`.xls`、图片等） | 上传时按扩展名直接返回 400 |

白名单之外的类型被拒是因为它们各自需要额外依赖或专门的分块策略。**明确不做 `.doc`（老二进制 Word）**：读它要 `antiword` 或 LibreOffice 这类外部二进制，或者维护成本更高的纯 Python 解析，收益只是"多认一个后缀"，而正确做法是让上传方另存为 `.docx`；`.xls` 同理（`xlrd` 只读且已停止维护），入库前统一要求 `.xlsx`；图片需要 OCR 引擎，未配置 OCR 时会被标为 `needs_ocr` 而不是假装成功。决策记录见 `docs/priority-fixes.md` 事项二十。**这条白名单只看扩展名**，所以下面这种情况能通过上传检查：

扫描件或无文字层的 PDF：`extract_text` 抽出 0 个字符时，Worker 先尝试 OCR 后端。配了 `OCR_BACKEND=tesseract`（可选 `OCR_LANGUAGE=chi_sim+eng`、`OCR_MAX_PAGES`）就渲染页面并调用 `tesseract` 取文字，成功则照常分块入库，Trace 里多一个 `ingestion.ocr` span；没配、二进制不存在、或 OCR 也取不到文字时，文档状态置为 **`needs_ocr`** 而不是 `failed` —— 文件本身没问题，缺的是这个部署的 OCR 能力。失败原因会写清是"没有配置后端（`set OCR_BACKEND=tesseract to read scans`）"、"引擎不可用（`ocr backend 'tesseract' unavailable: tesseract binary not found`）"还是"OCR 运行了但没取到文字"，`GET /api/v1/documents/{id}` 直接可见，前端轮询也把 `needs_ocr` 当作终态。这两种异常都不参与 Celery 自动重试（`dont_autoretry_for`），因为它们都不是瞬时故障。OCR 后端默认关闭，`none` 时不引入任何系统依赖。

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
ruff check app tests alembic scripts
pytest -q
cd frontend && npm run build
```

LangSmith 仅发送脱敏 metadata、问题文本、文档 ID、页码和受控摘要，不上传完整原文。
