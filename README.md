# EvalRAG Enterprise

评测驱动的多租户企业 RAG 平台，面向政务、金融和合规文档问答。项目采用 FastAPI、Vue 3、PostgreSQL、Redis 和 Celery，支持 SQLite 本地开发及 PostgreSQL 生产运行。

## 已实现能力

### 阶段一：产品闭环

- 多租户知识库和文档管理。
- PDF、DOCX、HTML、XLSX、TXT、Markdown 异步解析与结构化分块（网页公文走正文抽取去掉导航与页脚样板；表格按行序列化成自描述文本，不丢列语义）。
- 文档处理进度、失败原因和版本管理。
- Hybrid、Dense、Sparse 检索。
- SSE 流式回答、页码和分数引用、用户反馈。
  回答里的引用会逐条对照本次检索到的 (文档, 页码)：出现未检索到的文档或页码时，回答被拒绝而不是
  照常返回；流式路径因为已发出的 token 无法撤回，改为以 `event: error`（`ungrounded_citation`）收尾。
  "完全不带引用"默认不算拒绝（mock 与部分本地模型不写引用），可用 `CITATION_REQUIRED=true` 收紧。
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
- **口径说明（2026-10-04 修正）**：多跳题的 `nDCG@k` 现按理想排名（IDCG）归一，完美排名得 1.0
  （此前除以跳数，两跳上限 0.8155）；`mode="any"` 等价多标签题的 `recall@k` 与 `any_target@k` 一致，
  不再把命中任一等价标签记成 1/3。`docs/evaluation/*.json` 里 2026-09-28 及更早的产物仍是**旧口径**，
  重跑命令见各节；CI 质量门禁不受影响（夹具全为单跳题，逐项差值 0）。
- 带证据引文的样例额外计算 passage 级指标（`passage_hit`/`passage_at_1`/`passage_mrr` 与每题 `passage_rank`）：
  文档级指标在少量文档上会饱和，passage 级才能看出"答段排在第几"。
- 支持多跳样例（`expected_evidence`：每题一跳一个引文，另计严格的 `all_targets@k`）与应拒答样例
  （`should_refuse`：不参与检索指标，单独报 `negative_retrieved_rate`）。
- Celery 异步执行实验，结果保存到数据库；每条 retrieved 记录带 `chunk_id` 与 `text`，结果可复核。
- **执行方式（2026-10-05 起）**：有界并发（`EVALUATION_CONCURRENCY=4`，每题一次检索 + 可选两次 LLM
  调用，串行跑 75 题是分钟到小时级的差别）+ 每题超时（`EVALUATION_EXAMPLE_TIMEOUT_SECONDS=120`，
  卡死的题记为 `timed_out`、不进任何均值，另有 `timed_out_example_count` 说明几个数参与了平均）
  + 逐题 checkpoint：每完成一题就把该题的行写进 `results.examples`，所以失败/重试时
  `GET /evaluations/{id}` 能看到进度，Celery 用同一个 evaluation_id 重跑只补跑缺的题
  （`results.progress` 报 `completed/total/resumed`）。checkpoint 带实验指纹（题集、检索设置、
  rag/prompt 版本），换了设置就整轮重跑，不会把两套设置的逐题行混在一起。
- 可选同步 Dataset 到 LangSmith，并使用 LangSmith `evaluate` 执行 Experiment。
- CLI 支持运行实验和同步 Dataset。

仓库自带两套评测集与六组配置的实测结果。**语料自 2026-09-27 起为 18 份**
（13 份 PDF/DOCX + 新增 2 份 PDF、2 份 HTML、1 份 XLSX，共 386 chunk；扩容前那轮的产物保留在
`docs/evaluation/golden-set-2026-09-27.*` 与 `docs/evaluation/golden-set-v2-2026-09-27.*`，
两轮对照见评测报告 §9）。

v1（18 份语料、52 道页面级问题）：

```bash
python -m scripts.audit_golden_set --json docs/evaluation/golden-set-audit.json   # 标注回验（含 v2 段）
python -m scripts.run_golden_experiment \
    --json docs/evaluation/golden-set-v1-corpus18-2026-09-27.json \
    --markdown docs/evaluation/golden-set-v1-corpus18-2026-09-27.md              # 六组配置对比
python -m scripts.profile_retrieval --database data/experiments/golden.db         # 延迟归因
python -m scripts.probe_golden_difficulty --database data/experiments/golden.db   # 饱和成因探针
```

v2（75 题：52 同义改写 + 9 跨文档多跳 + 6 等价多标签 + 8 应拒答）：

```bash
# 重新生成需要 LLM key；校验失败的题会被丢弃并把原因写进 law/golden_eval_v2.rejected.json
python -m scripts.generate_golden_set_v2 --output law/golden_eval_v2.json
python -m scripts.run_golden_experiment --golden law/golden_eval_v2.json \
    --json docs/evaluation/golden-set-v2-corpus18-2026-09-27.json \
    --markdown docs/evaluation/golden-set-v2-corpus18-2026-09-27.md \
    --database data/experiments/golden-v2.db
```

v3（20 题：语料扩容时新增的 5 份文档各 4 题，沿用 v2 的去泄漏方法论）：

```bash
# --documents 接受数字前缀或完整名字段；生成侧已带两道闸门（样板题、跨文档重复引文）
python -m scripts.generate_golden_set_v2 --documents 14,15,16,17,18 --types paraphrase \
    --per-document 4 --output law/golden_eval_v3.json \
    --dataset rural-finance-regulations-golden-v3
python -m scripts.run_golden_experiment --golden law/golden_eval_v3.json \
    --json docs/evaluation/golden-set-v3-corpus18-2026-09-27.json \
    --markdown docs/evaluation/golden-set-v3-corpus18-2026-09-27.md \
    --database data/experiments/golden-v3.db
```

融合实验（10 组配置：等权 RRF 之外加了加权、截断、分数式 convex 三种修法）：

```bash
for s in v1 v2 v3; do
  python -m scripts.run_golden_experiment --golden law/golden_eval_$s.json \
      --json docs/evaluation/golden-set-$s-fusion-2026-09-28.json \
      --markdown docs/evaluation/golden-set-$s-fusion-2026-09-28.md \
      --database data/experiments/golden-$s-fusion.db
done
# 机制归因：每种融合把谁的页级第一名挤掉了、救回了多少（走产品路径复现）
python -m scripts.probe_fusion --database data/experiments/golden-v2-fusion.db \
    --golden law/golden_eval_v2.json
```

检索质量门禁（CI 每次提交都跑；用提交进仓库的小夹具语料，不依赖 `law/` 里的原文）：

```bash
python -m scripts.run_golden_experiment --corpus tests/fixtures/eval_gate/corpus \
    --golden tests/fixtures/eval_gate/golden.json \
    --configs sparse-bm25,dense-hash,hybrid-rrf,hybrid-convex-weighted,hybrid-convex-weighted-rerank \
    --json /tmp/eval-gate.json --database /tmp/eval-gate.db
python -m scripts.check_eval_regression --baseline tests/fixtures/eval_gate/baseline.json \
    --current /tmp/eval-gate.json --tolerance 1e-6
```

配置之间的差值是显著的吗（配对 bootstrap，只用已提交的产物、不需要语料）：

```bash
# 每个配置对上参考配置，逐题配对重采样 2000 次，给出 95% 区间与「一道题值多少 pp」
python -m scripts.bootstrap_ci --artifact docs/evaluation/golden-set-v3-fusion-2026-09-28.json \
    --against sparse-bm25 --json /tmp/bootstrap.json
```

题集有多少题，差值就有多粗：v3 只有 20 题，**一道题 = 5pp**，于是"分数式+加权"相对纯 BM25 的
R@1 +0.050 的 95% 区间是 `[+0.000, +0.150]` ——**跨 0，判为 inconclusive**，只能说"多中一道题"，
不能说"更好"；同题集上 dense-hash 的 -0.500（区间 `[-0.800, -0.200]`）才是真差异。工具还会
把逐题均值与产物里已发布的总量对账，不一致就告警（历史产物按不同分母平均过）。这个区间只覆盖
"题集抽样"这一项不确定性，不含标注错误，也不含"题目与证据出自同一次模型调用"这件事。

本地检索的一次性成本与重复成本（合成语料，任何人可复现）：

```bash
python -m scripts.benchmark_local_retrieval --documents 400 --queries 20
```

答案质量评测与裁判审计（默认关闭；每题会调两次付费模型）：

```bash
# 生成答案 + 裁判打分，并把裁判看过的 ≤3 块证据写进产物（人工复核必须看得到证据）
python -m scripts.run_golden_experiment --corpus tests/fixtures/eval_gate/corpus \
    --golden tests/fixtures/eval_gate/golden.json --configs sparse-bm25,sparse-bm25-rerank \
    --answer-evaluation --json docs/evaluation/answer-quality-eval-gate-2026-10-05.json
# 人工标签 vs 裁判：Cohen's kappa + bootstrap 区间（--dump-sample 先生成待标模板）
python -m scripts.judge_agreement --artifact docs/evaluation/answer-quality-eval-gate-2026-10-05.json \
    --labels docs/evaluation/judge-agreement-labels-2026-10-05.json --json /tmp/agreement.json
# 扰动校准：往正确答案里植入一句语料里根本没有的"规则"，看裁判会不会降 faithfulness
python -m scripts.judge_calibration --artifact docs/evaluation/answer-quality-eval-gate-2026-10-05.json \
    --json /tmp/calibration.json
```

实测（`deepseek-chat`，8 篇夹具语料、16 题、2 个配置 = 32 行，p50 约 3.5s/题）：
correctness 与人工标注的 **kappa = 1.000**、completeness = 1.000，但 **faithfulness = 0.000**
（人工认为"证据里确实没有该条款时拒答"是忠实的，裁判给 0 分）。32 行里裁判的三个维度只有两种
取值（28 行 `(1,1,1)`、4 行 `(0,0,0)`），**从未分离**。扰动校准连续三次都是
**`fabrications_flagged = 0/16`**：裁判的 reason 里明明写着"额外补充了证据中不存在的第三十三条
内容"，却仍给 0.5/1.0，理由是"不影响对核心问题的忠实度，且未与证据冲突"。⇒ 这个裁判可以用来
粗筛"答对了没有"，**不能**用来证明"有依据"；相关限制与取舍见
[`docs/priority-fixes.md`](docs/priority-fixes.md) 事项三十四。

结论与"能写/不能写"的边界见 [`docs/evaluation-report.md`](docs/evaluation-report.md)：
v1 标注 52/52 可回验，但指标已饱和（18 份语料上**纯 BM25** 就在文档级/页级拿满分，重排增益归零），
且本地 32 维 hash 向量使 hybrid 反而低于纯 BM25；v2 去掉"问题里报法规名"的词面泄漏后
指标明显下降（R@1 1.000 → 0.660），两种题集的对照见该报告 §8，
两轮语料（13 份 / 18 份）的对照与逐题归因见 §9，给新增 5 份文档补题后的结果见 §10
（BM25 R@1 0.800、表格类 4/4 全中——扩语料没有把新格式落下）。**§11** 是"混合检索为什么
没提升"的机制归因与修法：页级探针数出等权融合"挤掉 19 道 / 救回 2 道"，改成**分数式 + 加权**
后 v3 上首次超过纯 BM25；同一轮还查出并修掉"平局胜负由导入顺序决定"的不可复现缺陷
（同一配置跑出过 0.550 与 0.700），并给语义重排（TypeSafe，可选后端、默认关闭）接上
用量与花费记账——难集 R@1 0.646 → **0.769**（单跳 37/52 → 45/52，纯 BM25 是 38/52），
v3 0.850 → **0.950**（page_hit 1.000）；多跳题一道没涨，因为重排改不了候选集。
**§12** 修掉了查询管线的结构性重复劳动（`sparse` 每查询 387 次嵌入 → **0**、`hybrid` 774 → 稳态
**1**、p50 1102 → 194 ms，**质量指标逐位不变**），**§13** 把质量门禁装进了 CI
（夹具语料跑真实链路 + 逐项比对基线，注入回归验证过会拦）。

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
- 前端拆分为 `frontend/src/components/` 下的 8 个面板组件 + 3 个可测模块（`sse.ts` 流式分帧、
  `auth.ts` 凭据存储、`metrics.ts` 指标标签与格式），由 `npm test`（vitest）覆盖；
  **API Key 不再落任何浏览器存储**：登录时用它换一个会话令牌，令牌只放在 sessionStorage
  （默认勾选"在本标签页内保持登录"，取消勾选则仅内存、刷新需重新登录；隐私模式下自动降级为仅内存），
  关掉标签页即失效；历史上写进 localStorage/sessionStorage 的旧密钥
  在加载时被删除而不迁移。

### 阶段五：企业级检索

- Dense/Sparse 并行检索与 RRF 融合。
- 可插拔 Query Rewrite，本地默认使用规则扩展。
- 可插拔 Reranker，本地默认使用词项覆盖与融合分数重排。
- 支持 Milvus 和 Elasticsearch 适配边界。
- 文档和 Chunk 版本过滤（`document_version` 缺省表示不过滤、即全部版本；填具体标签则只搜该标签）。
- Memory/Redis 检索缓存。
- Retriever、Reranker 和缓存故障时自动降级。

### 阶段六：公网部署

- PostgreSQL 运行时存储与 Alembic 迁移。
- Redis 缓存、限流和 Celery Broker。
- API Key 认证和租户绑定；浏览器不再长期持有这把密钥——它只用来换一个**服务端会话令牌**
  （`POST /api/v1/auth/session`，`X-API-Key` → `ers_...`），此后所有请求发 `Authorization: Bearer ers_...`。
  令牌有 TTL（`SESSION_TTL_SECONDS`，默认 3600）、可吊销（`DELETE /api/v1/auth/session`）、
  可审计（`GET /api/v1/auth/sessions`，含 `last_used_at` 与 `key_fingerprint`），
  库里只存 `sha256(token)`；服务端到服务端的调用方仍可直接用 `X-API-Key`。
- 固定窗口限流，支持 Memory/Redis 后端。
- Nginx HTTPS、HSTS 和反向代理配置。
- Staging/Production Compose、GitHub Actions CI/CD。
- PostgreSQL 备份与恢复脚本。
- `/health/live`、`/health/ready` 和 Prometheus `/metrics`。
- 可靠性保障：外部索引写入走 `index_outbox`（与摄取同一事务落 intent，重试预算记在行上，可按行回放与 sweep；见 `docs/priority-fixes.md` 事项三十六）；状态字段有库级 `CheckConstraint`、评测参数/结果在 PostgreSQL 上是 JSONB、内容表随语料级联删除而评测历史保留；每个 API/Worker 副本启动时的 `alembic upgrade head` 由 PostgreSQL advisory lock 串行化（`EVALRAG_MIGRATION_LOCK=0` 可关闭）。
- 上传文件走可插拔对象存储：本地目录（默认，`OBJECT_STORE=local`）或 S3/MinIO（`OBJECT_STORE=s3` + `S3_*`），下载接口在 S3 上返回 307 预签名 URL、在本地后端直接返回字节。
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
- MinIO 控制台：`http://localhost:9001`（`MINIO_ROOT_USER` / `MINIO_ROOT_PASSWORD`，默认 `evalrag` / `evalrag-secret`）

Compose 会先执行 `alembic upgrade head`，然后启动 API 和 Worker。

上传文件默认已不落本地卷：compose 里会起 MinIO 与一次性的 `createbuckets` 服务，并把 API/Worker 的
`OBJECT_STORE` 设为 `s3`、`S3_ENDPOINT_URL` 指向 `http://minio:9000`。`S3_PUBLIC_ENDPOINT_URL`
额外设成 `http://localhost:${MINIO_PORT:-9000}`，因为下载接口返回的是**给浏览器**的预签名 URL，
里面的主机名必须是浏览器能解析的地址，而 `minio` 只在 compose 网络里可解析。若要用自己的 S3/OSS，
改 `S3_ENDPOINT_URL`、`S3_BUCKET`、`S3_ACCESS_KEY`、`S3_SECRET_KEY`（`S3_PATH_STYLE` 视服务商而定）
并把 `OBJECT_STORE` 保持为 `s3`；不设 `OBJECT_STORE=s3` 时会退回本地目录（`OBJECT_STORE_LOCAL_DIR`）。
本地后端不支持预签名，下载接口会退化成由 API 进程流式返回字节。

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
OBJECT_STORE=s3
```

`OBJECT_STORE=s3` 时还需设置 `S3_ENDPOINT_URL`、`S3_BUCKET`、`S3_REGION`、`S3_ACCESS_KEY`、
`S3_SECRET_KEY`（如用兼容 S3 的对象存储需按厂商设置 `S3_PATH_STYLE`），以及
`S3_PUBLIC_ENDPOINT_URL`——预签名 URL 里的主机名必须是**客户端**能解析的地址，
所以不能填只在内网可解析的 endpoint；不设置时预签名 URL 会指向 `S3_ENDPOINT_URL`。
`S3_PRESIGN_SECONDS` 控制 URL 有效期（默认 300 秒，签发后无法提前吊销）。

Production Compose **不会**创建 Milvus 或 Elasticsearch。生产部署必须另外提供 API/Worker 均可访问的 Milvus、Elasticsearch 和 OpenAI-compatible embedding 服务，并设置对应 URI、认证、collection/index、模型及 `EMBEDDING_DIMENSIONS`。首次切换后端、embedding 模型或维度后，对每个已 ready 文档执行 `python -m app.cli reindex-document <document_id> --force`；该命令先按文档删除外部旧索引，再写入新索引。建议生产保持 `EXTERNAL_RETRIEVAL_FALLBACK=false`，避免外部后端配置/schema/auth 错误被本地结果掩盖；若显式开启，只有连接/超时类不可用错误会降级。

Staging 与 Production Compose 会自己起一个 MinIO 服务（`minio` + 一次性的 `createbuckets`），
并把 API/Worker 的 `OBJECT_STORE` 固定为 `s3`、`S3_ENDPOINT_URL` 指向 `http://minio:9000`。
这两个文件里的 `MINIO_ROOT_USER`、`MINIO_ROOT_PASSWORD`、`S3_PUBLIC_ENDPOINT_URL` 用了
`${VAR:?set ...}` 写法：**不设置就拒绝启动**，而不是退回一个众所周知的默认口令——
生产栈悄悄用默认密码比直接起不来更糟。`S3_PUBLIC_ENDPOINT_URL` 必须是浏览器可达的
对象存储地址（例如 `https://objects.example.com`），因为下载接口会把预签名 URL 交给客户端。

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
pytest -q                      # 470 passed, 9 skipped（本机无 PostgreSQL，9 个 skip 全是环境限制）
pytest tests/test_storage.py tests/test_upload_streaming.py tests/test_tasks.py
                               # 52 passed（对象存储 + outbox 回放的专项口径）
uv sync --frozen --extra dev   # 完全按 uv.lock 安装（CI 的第一道门禁）
uv lock --check                # 锁文件与 pyproject.toml 是否一致
cd frontend && npm run build   # vue-tsc --noEmit + vite build
cd frontend && npm run lint && npm run format:check
cd frontend && npm test        # vitest：SSE 分帧、令牌存储、指标格式、三个面板 + App mount（44 项）
```

CI 里同样的门禁还包含 `mypy`、`pytest --cov=app --cov-fail-under=80`、镜像构建
（`docker build`，不推送）与依赖漏洞扫描（`uvx pip-audit`），定义在
`.github/workflows/gate-backend.yml`、`gate-frontend.yml`、`gate-eval.yml`，由 `ci.yml` 与
`release.yml` 共同调用；`gate-eval.yml` 会用仓库内的夹具语料跑一遍真实链路，
再与提交的基线逐项比对质量指标（比的是指标、不是延迟）。

上面 `pytest`、`ruff`、`mypy` 与前端四条命令的数字都取自 2026-10-08 的本地运行
（`ruff → All checks passed!`、`mypy → Success: no issues found in 49 source files`、
`npm test → 44 passed / 6 files`、`lint`/`format:check`/`vue-tsc --noEmit` 全部 exit 0），
第 2 梯队中"未在本地执行过"的部分（GitHub Actions 工作流、`docker compose up`、
真实 Milvus/Elasticsearch/MinIO/Redis、`npm run build`）在
[`docs/code-review-2026-10-04.md`](docs/code-review-2026-10-04.md) 的 §8/§9 里逐条列出。

LangSmith 仅发送脱敏 metadata、问题文本、文档 ID、页码和受控摘要，不上传完整原文。
