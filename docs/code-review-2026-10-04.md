# EvalRAG Enterprise 代码审查报告

审查日期：2026-10-04　范围：`app/`、`scripts/`、`tests/`、`alembic/`、`deploy/`、`frontend/`、`docs/`、CI
方法：逐文件阅读 + 运行测试套件 + 独立复算关键指标 + 对照 README/评测报告中的数字与仓库产物

---

## 0. 总体结论

**工程成熟度远高于典型秋招项目**，尤其在「评测基础设施」这条线上：有 golden set、有标注回验脚本、
有指标归因探针、有装进 CI 的检索质量门禁、甚至有一份把「能写 / 不能写」划清楚的评测报告。

它的问题不在于「做得少」，而在于**少数几个会被面试官一句话问穿的点**：

| 会被问的问题 | 当前答案 | 严重度 |
|---|---|---|
| 「引用不幻觉是怎么保证的？」 | `validate_citations` 是空壳，只判非空 | **高** |
| 「nDCG@K 怎么算的？」 | 算错了（DCG/n，多跳永远到不了 1） | **高** |
| 「并发下怎么保证一个文档只处理一次？有真并发测试吗？」 | 逻辑对，但 239 个测试里没有一处真并发；一次 PostgreSQL 都没连过 | **高** |
| 「指标差 3pp 是显著的吗？」 | 无置信区间、无 train/dev 划分，147 题单语料 | **高** |
| 「测试覆盖率多少？」 | 没有覆盖率工具，没有类型检查 | 中 |
| 「Milvus/ES 上了以后还全量扫库吗？」 | 是，每个请求仍 `get_chunks()` 拉全库进 Python | 中 |
| 「上传 50MB 的行不行？」 | 开发栈的 nginx 默认 1MB 就会 413 | 中 |

**测试套件状态（本机实测）**：收集到 239 个测试，`187 passed`；其余 52 个在本会话**无法执行**，
原因是 Windows 沙箱给 pytest 临时目录下发了当前令牌读不到的权限（51 个 `tmp_path` 夹具 setup ERROR，
1 个 `tests/test_ocr.py` 因 `TemporaryDirectory` 清理阶段 `PermissionError` 而失败，其断言路径本身是正确的）。
这不是仓库缺陷 —— 换普通终端或对 `C:\Users\99659\AppData\Local\Temp\dsh-*` 放权后应全绿。
注意 `.pytest_cache/` 也已不可读，所以本地 `pytest` 会带 cache 警告。

---

## 1. 已实现功能清单

### 1.1 API 层（FastAPI，9 组路由）

| 能力 | 位置 |
|---|---|
| 知识库创建 / 列表（租户绑定） | `app/api/routes/knowledge.py` |
| 文档上传（流式落盘 + 扩展名白名单）、删除（先清外部索引再删库，409 处理竞态）、详情、按知识库列表 | `app/api/routes/documents.py` |
| 检索 `/retrieval/search`（同步）+ `/chat/stream`（SSE：`retrieval → citations → token → trace → [DONE]`，失败发 `event: error`） | `app/api/routes/chat.py` |
| 反馈落库（trace_id 关联、版本号快照） | `app/api/routes/feedback.py` |
| 评测数据集 CRUD、评测创建/状态/结果/基线对比（`/evaluations/{id}/compare`） | `app/api/routes/evaluations.py` |
| `/health`、`/health/live`、`/health/ready`（真连 DB）、`/health/llm`、`/health/langsmith`（付费检查，需管理令牌）、`/metrics` | `app/api/routes/health.py` |

中间件三件套：请求上下文（`X-Request-ID` / `X-Trace-ID`）、Prometheus 指标、固定窗口限流（白名单免限流）。
`app/middleware.py`

### 1.2 检索链路

- **真实 Okapi BM25**：IDF + `k1`/`b` + 文档长度归一化，中文按「整串(≤8) + 2-gram + 3-gram」切分
  （`app/core/retrieval.py:51-99`）。不是词频统计冒充。
- **真向量 dense**：`cosine_similarity` + 可插拔 embedding（`HashEmbedding` 32 维确定性实现 / OpenAI-compatible 批量接口）。
- **融合**：等权 RRF（默认）之外，提供加权 RRF、逐通道截断、min-max 归一化的 convex 分数融合，共 5 种 spec（`Fusion` 表）。
- **重排**：`LexicalReranker`（归一化分 × 0.7 + 查询词覆盖 × 0.3）、可选 `CrossEncoderReranker`、
  可选 `TypesafeReranker`（付费 Noul 判断，带 429/5xx 重试 + `Retry-After` + 并发闸门 + usage 记账）。
- **查询改写**：规则扩展，可插拔。
- **缓存**：Memory / Redis / Null，key 覆盖 question、top_k、mode、版本、rerank、rag_version、
  embedding provider/model/dimensions、外部 collection/index、语料指纹（`app/core/retrieval_service.py:88-109`）。
- **检索器构建缓存**：按 `(kb, version, mode, fusion, 语料指纹)` 复用，避免每查询重建（`:225-257`）。
- **进程级向量缓存**：LRU 4096，key = `(provider:model:dimensions, sha256(text))`，批量路径只发 miss 且去重
  （`app/core/embeddings.py:28-53, 193-253`）。
- **外部后端 + 有界降级**：Milvus（同步 SDK 走 `to_thread`）、Elasticsearch；`ResilientRetriever` /
  `ResilientReranker` **只对 `BackendUnavailableError`（连接/超时类）降级**，配置/schema/维度错误直接失败，
  避免被本地结果掩盖。
- **版本过滤**：文档版本 + chunk 版本，Milvus filter 与 ES term filter 都带 `knowledge_base_id` 隔离。

### 1.3 摄取与解析

- 格式：PDF（PyMuPDF 逐页，页级分块、真页码）、DOCX、HTML/HTM（`html.parser` 抽正文，丢 `script/style/nav/header/footer/aside`
  + class/id 关键词 + 样板行过滤）、XLSX（openpyxl 逐表，**页码=工作表序号**，每行序列化成自描述文本并重复列名）、
  TXT/MD（UTF-8 → GB18030 回退，带 BOM 处理）、`.doc`/`.xls`/图片按扩展名 400 拒绝。
  `app/core/ingestion.py`，白名单单一来源 `SUPPORTED_SUFFIXES`/`SUPPORTED_LABEL`。
- **上传安全**：1 MiB 分块边读边写、超限立即 413 并中止读取、写 `.part` 后 `replace()` 原子改名、空文件 400、
  文件名 `re.sub(r"[^\w.\-]", "_", ...)` 消毒。
- **OCR 接缝**：`OcrBackend` 协议 + `TesseractBackend`（pymupdf 渲染 200dpi → `tesseract <png> stdout -l chi_sim+eng`，
  固定参数无 shell）。抽不出文字时区分「没配后端 / 后端不可用 / OCR 也取不到」三种原因，
  统一落 `needs_ocr`（**不是** `failed`），写进 `error_message`，且**不参与 Celery 自动重试**。
- **任务幂等**：`store.claim_document` 用单条条件 UPDATE 抢锁，`rowcount == 1` 才算抢到；
  `pending`/`failed` 可抢，`processing` 超过 900s 视为陈旧可抢，`ready` 仅在 `force=True` 时可抢。
  `app/tasks.py`、`app/core/store.py:224-269`。
- **进度与失败原因**：`progress` 20/60/100，`error_message` 落地，`GET /documents/{id}` 可见。
- 外部索引写入用 `replace_document`（先删后写），DB chunk 用 `replace_chunks`（同一事务删+插+更新计数）。

### 1.4 可观测性

- Span 树：`http.request` / `rag.request` / `query_rewrite` / `retrieval` / `retrieval.rank` / `retrieval.rerank` /
  `generation.answer` / `ingestion.document|extract|chunk|index|ocr` / `evaluation.experiment|example`。
  父子关系用 `ContextVar` 维护，`duration_ms`、inputs/outputs、error 齐全。
- LangSmith 可选，不可用时自动退化为本地 `SpanRecord`（可注入 `recorder`），不阻塞主业务。
- **脱敏**：手机号 / 身份证正则 + `tenant_hash`（sha256 前 16 位），`redact_value` 递归处理 dict/list/tuple，
  在 `set_outputs` / `update_metadata` 入口统一做。

### 1.5 评测平台

- 持久化数据集（题目、期望答案、期望文档、页码、**证据引文**、类别、`should_refuse`、多跳 `expected_evidence`、`evidence_mode`）。
- Runner：一次检索对同一排名算 `Recall@{1,3,5,top_k}`、`precision@k`、`nDCG@k`、`mrr`、
  `all_targets@k`/`any_target@k`、`page_hit`，带引文的额外算 `passage_hit`/`passage_at_1`/`passage_mrr` 与每题 `passage_rank`；
  候选题单独报 `negative_retrieved_rate`，不污染检索指标。
- 每题结果保存 `retrieved[].chunk_id/text/score/document_id/page/version` → 可复核。
- `latency_ms` + `p50/p95/p100/mean`（nearest-rank）。
- 基线差值 `baseline_diff`，且**跨租户基线会被跳过并告警**。
- 可选 LangSmith Dataset 同步与 `evaluate`（远端名按租户加 hash 后缀隔离）。
- CLI：`run-evaluation`、`sync-dataset`、`check-llm`、`check-langsmith`、`reindex-document --force`。

### 1.6 评测工具链（scripts/）

`run_golden_experiment.py`（11 组配置矩阵，输出 JSON+Markdown）、`audit_golden_set.py`（标注回验）、
`generate_golden_set_v2.py`（去泄漏出题 + 两道闸门）、`probe_fusion.py`（融合机制归因）、
`probe_golden_difficulty.py`（饱和成因）、`profile_retrieval.py`（延迟归因）、`check_eval_regression.py`（回归门禁）。

### 1.7 部署与运维

- Docker Compose（dev / staging / production），`migrate` 独立服务 + `service_completed_successfully` 门控，
  API/Worker 都等迁移完成 —— 这是正确做法。
- Nginx HTTPS（TLS1.2/1.3、HSTS、nosniff、SAMEORIGIN、Referrer-Policy、`client_max_body_size 55m`）。
- Alembic 10 个 revision，**每个都有 downgrade**，全部用 inspector 守卫做成幂等，
  `nullable=False` 一律带 `server_default`，`0009` 在有应拒答样本时**拒绝**执行有损 downgrade。
- GitHub Actions：`backend`（ruff + pytest）、`frontend`（`vue-tsc --noEmit && vite build`）、
  `eval-gate`（夹具语料跑真实链路 + 逐项比对基线，失败上传报告）。
- 备份/恢复脚本（sh + ps1）。
- 认：API Key → 租户绑定（`secrets.compare_digest` 用于健康令牌）、未认证时拒绝跨租户。

### 1.8 前端

Vue 3.5 + TS strict，`api.ts` 有完整请求/响应类型、统一 `checked()` 错误包装、手写 SSE 解析；
覆盖知识库、文档上传与轮询（`needs_ocr` 当终态）、问答流式、反馈、Dataset 编辑、实验配置、指标卡片、
逐题结果与基线差值。

---

## 2. 缺陷清单（按可被问穿的程度排序）

### P0-A 「引用校验」名不副实 —— `app/core/citations.py:6-10`

```python
def validate_citations(answer: str, chunks: list[Chunk]) -> bool:
    if not answer.strip():
        return False
    return bool(chunks)
```

函数名、docstring（"Require every cited chunk identifier ... to belong to retrieved evidence"）
和 README 第 27 行「页码和分数引用」都在承诺一件**代码没做的事**。
而且 `answer_with_evidence`（`app/core/rag.py:28-30`）用它当拒答闸门，所以实际语义只是「回答非空且证据非空」；
**流式路径 `stream_with_evidence` 完全不校验**。
→ RAG 项目最核心的 faithfulness 问题，目前是空的。

### P0-B nDCG@K 归一化错误 —— `app/core/evaluation.py:139-156`

实现对每个期望跳取 `1/log2(rank+1)` 后**按跳数取平均**，即 `DCG/n`，而不是 `DCG/IDCG`
（IDCG 应为 `Σ_{i=1..n} 1/log2(i+1)`）。仓库内不存在 IDCG。

我没有只靠读代码下结论，独立复算验证：

```
n=1 跳：实现上限 = 1.0000   （恰好等于正确 nDCG）
n=2 跳：实现上限 = 0.8155   （正确 nDCG 应为 1.0）
n=3 跳：实现上限 = 0.7103   （正确 nDCG 应为 1.0）
```

即：**单跳题上这个指标是对的，多跳题上它永远到不了 1** —— 而多跳正是 v2/v3 的主打卖点。
作者在 `docs/priority-fixes.md:675` 已经意识到「多跳的 nDCG 按跳平均，多跳 nDCG 本身没有公认口径」，
但报告里仍然把它当 nDCG 发布。

同一处还有：`recall_at_k`（`:80-91`）**忽略 `mode`**，对 6 道 `mode="any"`（等价多标签）的题算
「命中的可互换标签比例」，与该类标签的语义（命中任一即可）相反；`precision_at_k`（`:130-136`）
是「chunk 级命中数 / k」对「document 级目标集」，每文档 21.4 chunk，口径不成立，且没出现在任何报告表里。

### P0-C 指标基数爆炸 —— `app/middleware.py:57-61` + `app/core/metrics.py:12-16`

```python
container.metrics.observe(request.url.path, response.status_code, ...)   # 原始路径，含真实 UUID
```

`/api/v1/documents/3f2a...` 每个文档 id 都会新建一条时间序列，进程内 `Counter` **无界增长**，
`/metrics` 响应越来越长。正确做法是用路由模板 `request.scope["route"].path`（`/api/v1/documents/{document_id}`）。
另外 `latency_seconds` 只有 `_sum`，没有 `_count` / `_bucket` → Prometheus 算不出 P95，
与「阶段六：Prometheus /metrics」的承诺不匹配。

### P0-D `document_version="latest"` 语义冲突（作者已记录、尚未修）

- `app/schemas.py:26-30` 默认 `"latest"`；
- `app/core/store.py:306-307` 把它当**等值条件** `ChunkRecord.version == "latest"`；
- `app/tasks.py:136` 写入的是上传时的 `document.version`（例如 `v9`）。

→ 上传 `version="v9"` 并 `ready`，再用默认参数检索，**引用数为 0**。
`docs/priority-fixes.md:601-606` 已完整复现，属于「已知未修」的功能缺陷。
`frontend/src/App.vue` 还会在版本输入为空时回填 `'latest'`，与同页「空=全部」的展示逻辑不一致。

### P0-E v2 题集有一道重复题，且harness 以 question 为 key

我实际读 `law/golden_eval_v2.json` 统计：

```
examples: 75
DUPLICATE questions: {'这份规定从哪一天开始正式生效？': 2}
```

`scripts/run_golden_experiment.py` 用 question 建立 golden 索引，两行会归到同一个文档，
于是**至少一条已提交的逐题结果带着错误标签，而没有任何测试或审计能发现**。
`generate_golden_set_v2.py` 现在已有去重闸门（`asks_for_boilerplate` 等），
但闸门是在 v2 生成**之后**的提交里加的，v2 从未重新生成。

### P0-F 开发栈的 nginx 会把 >1MB 上传挡掉

`frontend/nginx.conf` 全文 17 行，**没有 `client_max_body_size`**（默认 1m），
而 `.env` 里 `MAX_UPLOAD_MB=50`，`app/api/routes/documents.py:84-90` 也按 50MB 校验。
dev compose 的 8080 端口正是走这个 nginx 代理，所以「支持 50MB 上传」在默认栈里是假的。
（生产 `deploy/nginx/https.conf:22` 设了 55m，反而没问题 —— 典型的只修了生产没修开发。）
另外生产 nginx 只代理 `/api/` 和 `/health`，**`/metrics` 在生产不可达**，Prometheus 抓不到。

### P0-G 测试污染真实数据库 + 继承真实 .env

`tests/test_app.py:6` 是模块级 `client = TestClient(app)`，`app` 走 `get_settings()` → `env_file=".env"`。
于是 `:9-34` 的四个测试写的是开发者真实的 `data/evalrag.db`（我实测该文件 mtime 在跑完测试后变为本次会话时间），
并继承 `.env` 里的 `LLM_PROVIDER=openai-compatible` 与 `LANGSMITH_ENABLED=true` + 真 key
→ **跑一次测试会往 LangSmith 发 trace**。其余测试大多正确地注入 `Settings(...)`/`tmp_path`，只有这几处例外。

### P1-H 并发安全只有「顺序验证」，且一次 PostgreSQL 都没连过

`claim_document` 的设计是对的，但 239 个测试里没有任何线程/进程/多 worker 并发；
`test_tasks.py:149` 是先抢占再重跑，`test_tasks.py:167-172` 是顺序断言 `False/True/False`。
`grep postgresql tests/` 只命中 `test_store.py:35-43`（断言 URL 规范化和 engine 的 dialect 名，**不执行查询**）。
而生产跑的是 PostgreSQL。这是「我用数据库原子更新实现了跨进程幂等」这句话最容易被追问穿的地方。

### P1-I 生产路径没有摆脱「整库加载 + Python 全量扫描」

`app/api/routes/chat.py:33-37` 每个请求先 `store.get_chunks(kb_id, version)` 把**整个知识库的 chunk**
拉进内存，再交给 `RetrievalService`。即使 `DENSE_RETRIEVAL_BACKEND=milvus`、
`SPARSE_RETRIEVAL_BACKEND=elasticsearch`，这一步依然存在（本地 sparse fallback 需要它）。
再叠加：

- `_corpus_fingerprint`（`retrieval_service.py:216-223`）在**缓存查询之前**对全量语料做 SHA-256
  → 「缓存命中」省掉了排序，但**省不掉 O(语料字节) 的哈希**；
- `_retrievers` 最多 32 条，**每条强引用整个语料的 Chunk 列表** → 32× 语料常驻内存；
- 本地 BM25 每次查询对全语料重新分词（`:69-99`），无倒排索引、不预计算 IDF 与文档长度统计。

386 chunk 下无感，10 万 chunk 下就是架构问题。作者已在 `docs/priority-fixes.md:623-631` 量化并列为 P1。

### P1-J 评测统计

- **无显著性检验**：147 题、单语料（18 份 / 386 chunk）、配置间差异 1–3 题（≈2–6pp），
  报告却引用 +3.4pp / −34.0pp；无 bootstrap、无置信区间。
- **无 train/dev 划分**：11 组融合配置与 TypeSafe 都是看过 v2/v3 之后选的 → 「convex-weighted 在 v3 上首次超过 BM25」
  是 selected-on-test。
- **无答案质量指标进入产物**：`run_golden_experiment.py:383` 硬编码 `answer_evaluation=False`；
  `judge_answer` 无校准/一致性证据。拒答只用「检索是否为空」近似。
- **引文由出题的同一次 LLM 调用产出** → 「引文在标注页上」是生成器保证的；`audit_golden_set.py` 只是用
  同一套解析器再查一遍（自证），52/52 里还包含 3 条引文在另外两份文档里逐字相同的题。
  没有人工标注、没有 inter-annotator agreement。
- **部分报告数字无产物支撑**：§12（「387→0 嵌入、p50 1102→194ms」）没有任何 artifact 带该提交，
  也没有计时产物；`report:723` 提到的 0.700 在任何提交产物里都不存在；
  README 说「两套评测集」实际 3 套、「10 组配置」实际 11 组。
- **CI 门禁偏弱**：夹具 8 文档 / 8 chunk / 16 题，随机文档命中率 0.625，而 `sparse-bm25 recall_at_1` 是 0.6
  （接近随机）；`check_eval_regression.py` 只拦回归，改进直接放过；身份校验既不比 git commit 也不比 embedding 维度。
- **语料被 gitignore**（`.gitignore:23-27`），所以任何 §3/§9/§10/§11 的数字在干净 clone 里都**无法复现**。

### P1-K 工程基建缺口

- **无覆盖率**（仓库有 `.coverage` 的 gitignore 条目但没有任何东西产出它）、**无类型检查**（无 mypy/pyright）。
- **CI 用 `pip install -e ".[dev]"`，637KB 的 `uv.lock` 是装饰品**（`pyproject.toml` 无 `[tool.uv]`），依赖不可复现。
- 无 `docker build`、无安全扫描（`pip-audit` 只在本地跑过）、无 dependabot/CodeQL。
- `release.yml` 的 `images` job 打 tag 就发，**不依赖 test 结果**。
- `npm ci` 只做 `vue-tsc && vite build`，**前端零测试**（无 vitest）。
- `App.vue` 823 行单体（script 1-434 / template 436-880，87 个顶层声明），无 pinia/路由，~35 个模块级 `ref`；
  可访问性薄（2 个 `aria-label`，无 `role`、无 live region）；API key 存 `localStorage`（XSS 可取），
  「登录」只是填一个租户字符串。

### P1-L 部署与运维

- `Dockerfile` 建了 `appuser` 但**没有 `USER` 指令**，靠 `docker-entrypoint.sh:7` 的 `runuser` 降权
  → 绕过 entrypoint 的 `docker run` 是 root；且 entrypoint 每次启动 `chown -R /app/data/uploads`（O(文件数)）。
- `deploy/docker-compose.production.yml` 里 **Redis 无认证**、无网络隔离、无资源限制。
- `docker-compose.yml:56,81` 把 `./app`/`./alembic` 以 `:ro` 挂进镜像覆盖已安装包 → 镜像不自包含。
- 基础镜像全是可变 tag（`python:3.12-slim`、`postgres:16-alpine`、`redis:7-alpine`、`nginx:1.27-alpine`），无 digest。
- `scripts/restore_postgres.sh:15` 直接 pipe 进 `psql`，无 `--clean/--if-exists` → 对非空库恢复会失败；
  `scripts/backup_postgres.sh:14` 把 `pg_dump` 重定向到目标文件，**失败会留下 0 字节的「备份」**；
  `scripts/backup_postgres.ps1:16` 用 `Set-Content -Encoding utf8`（PS5.1 带 BOM）→ 恢复可能损坏。
- `alembic/env.py` 无 `pg_advisory_lock`，两个 migrate 容器并发可双跑；
  `alembic/versions/0003` 的 downgrade 不是 upgrade 的逆（留下 7 列 3 索引）。

### P2-M 杂项

- `evaluation.py`（1294B）与**无扩展名的 `evaluation`**（1421B）内容重复且都被 git 跟踪；
  `tests/test_langsmith_runner.py:2` import 的是 `evaluation.py`，无扩展名那个是**不可导入的死代码**。
- `app/core/llm.py` 的 prompt 把 `[document_id p.page]` 前缀塞进 context，但没有任何机制保证模型输出这些 id
  —— 与 P0-A 是同一个问题的两面。
- `.env` 里有**真实凭据**（LANGSMITH / LLM / TYPESAFE，长度分别为 51 / 35 / 108 字符）。
  好消息：`.gitignore` 已忽略 `.env`，`git ls-files` 与全历史扫描都没有提交，`.example` 全是占位符。
  但工作目录会被打包、备份、被 agent 读取 —— 建议轮换这三个 key。
- `docs/evaluation-report.md` 68KB + `docs/priority-fixes.md` 70KB。内容质量很高，
  但**面试官不会读 140KB 文档**，需要一份 1 页的 README 摘要（把结论前置）。

---

## 3. 这些做得很好，应该在简历/面试里重点讲

1. **融合负优化的机制归因**（最强素材）：先用 `probe_fusion.py` 数出「等权 RRF 在页级挤掉 19 题、只救回 2 题」，
   再给 5 种融合变体对比，最后用**分数式 + 加权**在 v3 上首次超过纯 BM25。
   这是「先量化机制、再改设计、再验证」的完整闭环，比「我加了 hybrid 检索」高一个量级。
2. **不可复现缺陷的定位与修复**：同一配置跑出过两个不同的 R@1，根因是 RRF 平局由 chunk id 决定，
   而 `chunk_id = f"{document_id}:{index}"` 里的 `document_id` 是每次摄取新生成的 UUID，且 store 按 id 排序
   → 排序随导入顺序变化。改成**内容破平**（page → text）后稳定。
   这是很硬的 debug 故事，也是面试官最喜欢的「你怎么定位偶发问题」。
3. **性能归因与安全网**：每查询 387 次嵌入 → 0；hybrid 774 → 稳态 1；p50 1102 → 194ms，
   并且强调「**质量指标逐位不变**」作为改造安全的证据。有前后对照、有回归保护。
4. **去泄漏方法论**：v1 的题要求写出法规全称（而全称逐字出现在原文首页），等于把最强词面线索送给 BM25；
   去掉后 R@1 从 1.000 掉到 0.660 —— 把「我的指标虚高了多少」量化成 32pp。
   大多数候选人只会报一个漂亮数字，不会自己把它拆掉。
5. **评测装进 CI**：夹具语料跑真实链路 + 与基线逐项比对（`--tolerance 1e-6`），并且**注入过回归验证会拦**。
6. **`claim_document` 的取舍说明**：为什么用条件 UPDATE 而不是 Redis 分布式锁或 `SELECT ... FOR UPDATE`
   —— 锁需要过期与续租逻辑、`task_id` 去重挡不住「不同任务处理同一文档」；条件 UPDATE 与状态机天然一致、
   跨进程跨语言、无额外依赖。取舍讲得清楚。
7. **降级边界**：只对 `BackendUnavailableError`（连接/超时）降级，配置/schema/维度/鉴权错误必须直接失败，
   避免「外部后端坏了但被本地结果掩盖」。这个判断力比会写 try/except 值钱。
8. **上传流式写入**：1 MiB 分块边读边写、超限在读取过程中即 413 并中止、`.part` + `replace()` 原子改名、
   任何失败路径都清理临时文件。
9. **`needs_ocr` 状态设计**：区分「文件坏了」（`failed`）与「这个部署缺 OCR 能力」（`needs_ocr`），
   并给三种原因分别写清可执行的修复提示，且不进自动重试（不是瞬时故障）。
10. **XLSX 行级自描述序列化**：实测把「取值所在 chunk 找不到自己的列名」从 39.1% 降到 0.0%（代价是文本长约 3 倍），
    并明确说出这个代价。
11. **GB18030 回退解码**：避免「UTF-8 宽松解码把每个汉字变成 U+FFFD 后静默入库」。
12. **脱敏与租户隔离**：Trace 层统一做手机号/身份证/token 脱敏 + tenant hash；
    LangSmith 远端 Dataset 名加租户 hash 后缀；跨租户基线跳过并告警；跨租户创建基线返回与不存在一致的 404（不泄露存在性）。
13. **`docs/priority-fixes.md` 的复盘模板**（根因 / 设计选择 / 替代方案与取舍 / 新增测试 / 验证命令及结果 /
    性能质量数据 / 仍存在的限制）—— 这个结构本身就是很好的面试叙事骨架，建议直接内化成口头表达方式。

---

## 4. 提升到秋招面试级别的行动清单（按 ROI 排序）

### 第 0 梯队：1–3 天，补上「会被一句话问穿」的点

| # | 动作 | 为什么面试加分 | 验收 |
|---|---|---|---|
| 1 | **让 `validate_citations` 真的做事**：解析答案里的 `[doc_id p.page]`，校验每个引用都在 evidence 内，否则拒答；流式路径同样接上 | 引用可核验性是 RAG 的核心考点，现在是空壳 | 单测：伪造一个不在证据里的引用 → 必须拒答；合法引用 → 通过 |
| 2 | **修 nDCG 归一化**（除以 IDCG）或**改名为 `dcg@k` 并说明口径**；同时修 `recall_at_k` 对 `mode="any"` 的口径 | 「nDCG 怎么算的」是标准追问，现在多跳上限 0.8155 | 新增测试：完美排名（rank 1..n）必须得 1.0 |
| 3 | **修 metrics 标签基数**：用 `request.scope["route"].path`；补 `_count` 与 `_bucket`（或直接用 `prometheus_client`） | 「高基数标签」是监控面试的经典题 | 请求 3 个不同 document_id 后 `/metrics` 只有 1 条时间序列 |
| 4 | **修 `document_version` 语义**：默认 `None`（不过滤）或把 `latest` 解析为「每文档最大版本」 | 这是真实可见的功能 bug | 上传 `v9` 后默认查询能命中；补多版本测试 |
| 5 | **修 `tests/test_app.py`**：改成 fixture + 独立 SQLite/临时库 + 显式 `Settings`，不再继承 `.env` | 「测试是否有副作用」是工程素养信号 | 跑完 `pytest` 后 `data/evalrag.db` mtime 不变 |
| 6 | **`frontend/nginx.conf` 加 `client_max_body_size 55m`**；生产 nginx 加 `/metrics` 反代（带 token/IP 白名单） | 部署细节的真实性 | 默认栈上传 5MB 成功 |
| 7 | **清理**：删掉无扩展名的 `evaluation` 死文件；修 v2 重复题并重新生成 v2 产物 | 仓库整洁度 / 数据可信度 | `audit_golden_set` 新增重复题检查并通过 |

### 第 1 梯队：3–7 天，把工程深度变成「可讲、可验」

| # | 动作 | 为什么面试加分 | 验收 |
|---|---|---|---|
| 8 | **真并发测试**：用 CI service container 起 PostgreSQL，8 线程/进程同时 `claim_document`，断言恰好 1 次成功、DB 终态正确 | 把「并发幂等」从顺序验证升级为真并发验证；同时补上「从未真连 PG」的空白 | 新测试在 CI 里跑真实 PG，并发断言通过 |
| 9 | **加覆盖率 + 类型检查**：`pytest-cov` + `--cov-fail-under`（先设真实基线），CI 加 mypy（至少 `app/core`、`app/api`） | 「覆盖率多少」「有类型检查吗」是必问项，现在是「没有」 | CI 里 `--cov-fail-under=75` 与 `mypy` 都通过 |
| 10 | **本地 BM25 加倒排索引**：预计算词频/文档长度/IDF，`LocalIndex` 只建一次，查询不再对全语料重新分词 | 直接对应作者 P1「消除整库加载 + 全量扫描」；是「从 demo 到可用」的关键一跳 | 给出改造前后 p50/p95 对比，且逐题指标完全不变 |
| 11 | **加显著性检验**：`scripts/bootstrap_ci.py`，每组配置 1000 次重采样给 95% CI，报告里把 Δ 改成「Δ [CI]」 | 在 n=75 的情况下主动说明不确定性，比报一个 3.4pp 的数字专业得多 | 评测报告所有 Δ 列都带区间；不显著的差异标灰 |
| 12 | **评测 Runner 加有界并发 + 单题超时 + checkpoint** | 对应作者 P1，且是「工程化」的直接证据 | 注入一个超时样例，Runner 跳过并记为失败样例，不中断整轮 |
| 13 | **跑一次 `answer_evaluation=True` 并产出 artifact**；对 `judge_answer` 抽 30 题做人工校验，报 Cohen's kappa | 补上「答案质量」这条完全缺失的评测线；LLM-as-judge 的一致性度量是很强的加分项 | 产物里出现 `answer_correctness/faithfulness/completeness` + kappa 数字 |
| 14 | **前端拆分 + 冒烟测试**：`App.vue` 拆成 `views/` + `components/` + composable；加 vitest；API key 从 `localStorage` 移出 | 823 行单体是明显的减分项；前端零测试同样 | `npm test` 有真实断言；单文件 <300 行 |

### 第 2 梯队：可选，增强「系统设计」叙事

15. **outbox / 可重放**：PostgreSQL 与 Milvus/ES 双写一致性（worker 在「外部索引成功、状态更新前」崩溃会留下悬挂 `processing`）。这是分布式一致性的标准考点，作者已列 P1。
16. `Document.status` 用 Enum + `CheckConstraint`；评测参数/结果在 PG 用 JSONB；补 FK/级联删除。
17. 补 Cache / Retrieval / Celery / DB Pool 指标；readiness 检查 Redis、队列与外部后端。
18. 上传落对象存储（S3/MinIO）+ 预签名 URL，解除 API/Worker 共享本地卷限制。
19. Alembic 加 `pg_advisory_lock`；`0003` 的 downgrade 补齐或明确声明 forward-only。
20. `release.yml` 加 `needs: [backend, frontend, eval-gate]`；CI 加 `docker build`、`pip-audit`、`uv.lock` 真正生效（`uv sync --frozen`）。

---

## 5. 面试话术建议

**别这样开场**：「我做了个基于 FastAPI + Vue 的 RAG 问答系统」——烂大街，面试官会立刻降级为兴趣。
**这样开场**：「我做了个**评测驱动**的 RAG 平台。先用 golden set 量化出 hybrid 比纯 BM25 差 24 个百分点，
再用探针定位到等权 RRF 的机制问题（挤掉 19 题、只救回 2 题），改成分数式加权后在难集上首次反超；
过程中还抓到一个偶发不可复现的缺陷，根因是融合平局由随机 UUID 决定。」

**准备三个深挖故事（STAR）**：

1. **偶发不可复现**：同一配置跑出 0.550 与 0.700 → 定位到 RRF 平局用 chunk id 破平 →
   而 id 含每次摄取新生成的 UUID → 改为内容破平 + 加回归测试。
2. **每查询 774 次嵌入 → 1 次**：定位到 retriever 每查询重建 + dense 通道被算两遍 →
   引入进程级 LRU 向量缓存（key = provider:model:dimensions + 文本哈希）+ 检索器复用（key = 语料指纹）→
   **用「质量指标逐位不变」证明改造安全**。
3. **自己拆掉自己的漂亮数字**：发现 v1 的 52 道题里有 49 道在问题里写了法规全称，
   而去掉后 R@1 从 1.000 掉到 0.660 → 建立去泄漏出题流程与两道生成期闸门。

**主动交代边界**（作者在 `docs/evaluation-report.md` §5「能写 / 不能写」已经做得很好了，
面试时复述即可，主动说限制远好于被问出来）：

- 本地 32 维 hash 向量**不具备真语义能力**，hybrid 的低分有一部分是它造成的，不是融合范式的结论；
- 语料 18 份 / 386 chunk，**规模不构成生产验证**；延迟是单机串行测量；
- 没有人工标注者、没有一致性度量，题集是 LLM 自出题自校验；
- Milvus / Elasticsearch 只做了 SDK 契约测试（fake client），**没有连过真实集群**。

**当前会被问倒的五个问题，正好就是行动清单的优先级依据**：

1. 「你的引用怎么保证不幻觉？」→ `validate_citations` 是空壳（第 0 梯队 #1）
2. 「并发下一个文档怎么只被处理一次？有真并发测试吗？」→ 只有顺序测试、没连过 PG（第 1 梯队 #8）
3. 「上了 Milvus 之后还全量扫库吗？」→ 是（第 1 梯队 #10）
4. 「nDCG@5 怎么算的？」→ 现在算错了（第 0 梯队 #2）
5. 「覆盖率多少？有类型检查吗？」→ 都没有（第 1 梯队 #9）

把这 5 条补齐，这个项目在秋招里会从「不错的课程设计」变成「有真实工程判断力的项目」。

---

## 6. 第 0 梯队执行结果（2026-10-04 完成）

七项全部落地。逐项的代码位置、设计取舍与验证命令记录在
[`priority-fixes.md`](priority-fixes.md) 的事项二十六至三十，这里只给结论与证据。

| # | 项目 | 状态 | 关键证据 |
|---|---|---|---|
| 1 | 引用真校验（含流式） | ✅ | `app/core/citations.py` 重写为 `citation_report`；`rag.py` 非流式拒答、`chat.py` 流式发 `event: error` + `code=ungrounded_citation` 且不发 `trace`；`Settings.citation_required` 控制"必须引用"；新增 11+5+1 项测试 |
| 2 | nDCG 归一化 + any 模式 recall | ✅ | 改为除以 IDCG；新增测试断言**完美排名必须得 1.0**（旧实现两跳上限 0.8155）；any 模式 `recall@k` 与 `any_target@k` 一致；CI 门禁实测 `+0.0000` |
| 3 | 指标标签基数 + 直方图 | ✅ | 标签改用路由模板（`scope["route"].path` → `path_params` → 正则兜底）；11 桶直方图 + `_count`/`_sum`；测试断言 5 个不同 UUID 只产生 1 条时间序列 |
| 4 | `document_version` 语义 | ✅ | 默认 `None`（不过滤），空串归一为 `None`，`"latest"` 退回为字面标签；前端初值/回填改空串、请求发 `null`；新增端到端测试：上传 `v9` 后默认查询命中 |
| 5 | `test_app.py` 隔离 | ✅ | 新增 `tests/conftest.py` 的 `isolated_settings()/client`（私有共享缓存内存库，不落盘、不读 `.env`）；顺带为内存 SQLite 显式指定 `StaticPool`，消掉 SQLAlchemy 弃用告警 |
| 6 | nginx | ✅ | `frontend/nginx.conf` 加 `client_max_body_size 55m`（原为默认 1 MB，与 `MAX_UPLOAD_MB=50` 矛盾）并代理 `/health`；`deploy/nginx/https.conf` 加 `/metrics` 反代（仅私有网段可访问） |
| 7 | 清理与数据缺陷 | ✅ | 删除死文件 `evaluation`；v2 重复题进入审计（`unique questions: 74 (duplicates: 1)`）；harness 改为按 `example_id` 建索引，新增测试证明同题不同标签不再互相串号 |

**顺带发现并修掉的两个缺陷**（都不在原清单里）：

- `scripts/check_eval_regression.py` 的成功行含 `✓`，在 GBK 控制台/管道下 `UnicodeEncodeError`，
  把一次**通过**的质量门禁变成 exit 1 —— 对一个"用来建立信任"的检查来说是最坏的失败模式。已改为降级字符、不降级退出码。
- `app/core/ocr.py` 的 `TemporaryDirectory` 清理失败会**顶掉** `OcrUnavailableError`：
  文档本该被归类为 `needs_ocr`（交给人工或配了 OCR 的部署），却变成一个无法解释的通用失败。
  已改为 `mkdtemp` + `rmtree(ignore_errors=True)`。`ignore_cleanup_errors=True` 并不够用——
  CPython 的清理处理器在 chmod 失败时会从 `__exit__` 里抛出。

**验证结果**：

```
ruff check app tests alembic scripts          → All checks passed!
pytest                                        → 267 passed, 1 skipped
vue-tsc --noEmit (frontend)                   → exit 0
scripts.check_eval_regression (CI 门禁)        → exit 0，全部指标 +0.0000
scripts.audit_golden_set                      → v1 52/52、v2 74 unique + 1 duplicate、v3 20/20
```

测试数从 239 增至 268（新增 `test_citations`/`test_metrics`/`test_document_versions`/`test_golden_experiment_keying`
与既有文件里的新用例）。唯一 skip 是 `tests/test_ocr.py` 中需要可写临时目录的那一项——
本会话的 Windows 沙箱不允许进程写入自己刚创建的目录，属环境限制，普通文件系统上会正常执行。

**故意没做的两件事**（判断依据见事项二十八/三十）：

1. **没有重跑 `docs/evaluation/*.json` 的历史产物**。新的 nDCG 与 any 模式 recall 口径会让多跳题与等价题的那些列**数值变化**，
   而重生成需要未入库的 `law/` 原文与 5–14 分钟。因此那批产物仍是旧口径，报告中该两列的语义与当前代码不一致——
   README/评测报告若要继续引用，需要重跑或标注口径。CI 门禁不受影响（夹具全是单跳题，实测逐项 `+0.0000`）。
2. **没有改写 v2 的重复题**。两道题问的是两部不同法规、答案各异，按 id 建索引后两行都能正确计分；
   改写文案会破坏与既有产物的可比性。审计现在会把重复**报出来**，由人来决定是否改写。

> 环境说明：本会话的 Windows 沙箱会让 pytest 无法列举自己刚创建的临时目录，因此那 51 个使用 `tmp_path` 的既有测试
> 需要一个临时插件（`sandbox_tmp_pool.py` + 预建目录池）才能运行；该插件是本次排查的脚手架，**不属于项目**，
> 已在交付前删除，仓库里只留下 `tests/conftest.py` 这个真实改进。

## 7. 第 1 梯队执行结果（2026-10-05 完成）

逐项的根因、设计取舍、变异验证与仍存在的限制记录在
[`priority-fixes.md`](priority-fixes.md) 的事项三十一至三十五（#10/#11 见该文件更早的条目与 README），
这里只给结论与证据。

| # | 项目 | 状态 | 关键证据 |
|---|---|---|---|
| 8 | 真并发 + 真 PostgreSQL | ✅ | `tests/test_concurrency.py`：8 线程 `Barrier` 同时放行，6 个用例 × sqlite/postgres，断言**恰好一个** worker 认领、抢先与陈旧抢占、读者永远看不到半套索引（写者各用独立 chunk id 前缀）；`tests/test_migrations.py`：逐版本 downgrade 后快照必须与 upgrade 前完全一致（10 个修订版本；探针查出 **4/10 个 downgrade 不是"逆"**——0001 漏 drop `feedback`/`evaluations`、0003 漏 7 列 3 索引、0004 漏两个 `version` 列、0005 漏 `feedback.tenant_id`，全部补齐）；CI 新增 `postgres:16` service job 真跑这两个文件 |
| 9 | 类型检查 + 覆盖率门禁 + `uv.lock` | ✅ | `[tool.mypy]`（`check_untyped_defs` 等）+ `[tool.coverage]`（`fail_under=80`，依据实测 `app/` 行覆盖 **86.7%**）+ dev extras 增补 `mypy`/`pytest-cov`；**28 个真实类型问题**逐个修（pymupdf `Document` 不走 `__iter__`、`RedisError` 被重绑定成类型、`run_type` 收窄为 Literal…）；CI 增加 `mypy`、`pytest --cov --cov-fail-under=80`、`setup-uv` + `uv lock --check`；mypy 本地 1.14.1 与锁定 1.20.2 都是 `Success` |
| 10 | 本地 BM25 倒排索引 | ✅ | `BM25Index`（postings + df + doclen）+ 保序 `query_terms`；两条实现（一次性直算 / 索引）在 `tests/test_bm25_index.py` 里被钉成**逐位相同**；400 文档 / 20 查询实测 **119.62 → 1.29 ms/查询（92.8×）**，一次性路径无回归（111.00 ms）；质量门禁 35 行指标 `+0.0000` |
| 11 | 配对 bootstrap 置信区间 | ✅ | `scripts/bootstrap_ci.py`：同题重采样 + 固定种子 + 逐题均值与已发布总量对账；v3（20 题，1 题 = 5pp）上"分数式+加权" R@1 +0.050 的 95% 区间 `[+0.000, +0.150]` → **inconclusive**，dense-hash −0.500 → worse；全量 24 inconclusive / 6 worse / 5 no data |
| 12 | Runner 有界并发 / 每题超时 / checkpoint | ✅ | `EVALUATION_CONCURRENCY`（默认 4）+ `EVALUATION_EXAMPLE_TIMEOUT_SECONDS`（默认 120）+ 逐题 checkpoint（带实验指纹，失败保留已完成的题，重跑只补缺的题）；`results.progress` 暴露 `completed/total/resumed`；新增 5 项测试 + **5 个变异体全部被抓** |
| 13 | answer evaluation + judge 人工校验 | ✅ | `--answer-evaluation`（默认关）跑通真实 `deepseek-chat`：`docs/evaluation/answer-quality-eval-gate-2026-10-05.json`（2 配置 × 16 题 = 32 行，逐行存裁判三分 + reason + 裁判看过的 ≤3 块证据）；人工复核 correctness **kappa 1.000**、completeness **1.000**、faithfulness **0.000**（人工认为"证据不足时拒答"是忠实的，裁判给 0）——32 行里裁判三个维度只有 `(1,1,1)`×28 与 `(0,0,0)`×4 两种取值，**从未分离**；扰动校准（往正确答案植入语料里不存在的"规则"）连续三次 `fabrications_flagged = 0/16`，裁判 reason 写着"证据中不存在的第三十三条"却仍给 0.5/1.0（理由："不影响忠实度、未与证据冲突"）。顺带查出并修掉 **LLM 适配器零重试**：一次 `httpx.ConnectError` 让整轮付费评测 failed（`app/core/llm.py` 加 3 次重试 + `Retry-After`） |
| 14 | 前端拆分 + vitest + key 移出 localStorage | ✅ | `App.vue` 880 行 → 脚本 434 行的外壳 + 8 个 `components/*.vue`（只搬模板，状态与函数留在 App，行为逐字不变，`defineModel` + props/emits）；三块有逻辑的代码抽成可测模块 `src/sse.ts`（`SseDecoder` 按"更早出现的 `\n\n` / `\r\n\r\n`"切帧，半个帧留在 `pending`）、`src/auth.ts`、`src/metrics.ts`；**API Key 不再进 localStorage**——默认只在内存，勾选"在本标签页内记住密钥"才写 sessionStorage，legacy key 一律删除而不迁移；顺带修掉两个真缺陷：旧解析器**完全忽略 `event: error`**（被拒答时界面一片空白）且**只认 LF 终止符**（CRLF 帧一个都解析不出来）；`vitest.config.ts`（独立配置 + `pool: 'threads'`）与 **32 项前端测试** `npm test` → 5 files / 32 passed，`vue-tsc --noEmit` exit 0，CI `frontend` job 增加 `npm test` |

**当前验证快照**（每个数字都可用仓库内命令复现）：

```
ruff check app tests alembic scripts          → All checks passed!
mypy (1.14.1 本地 / 1.20.2 锁定)               → Success: no issues found in 44 source files
pytest                                        → 332 passed, 8 skipped
scripts.check_eval_regression (CI 门禁)        → exit 0，35 行指标全部 +0.0000
scripts.benchmark_local_retrieval             → 92.8× （一次性路径无回归）
scripts.bootstrap_ci                          → 区间与分母可复现（固定种子）
scripts.judge_agreement                       → correctness/completeness kappa 1.000，faithfulness kappa 0.000
scripts.judge_calibration                     → fabrications_flagged 0/16（三次独立运行）
cd frontend && npm test                       → 5 files / 32 passed（SSE 分帧、密钥存储、指标格式、两个面板）
cd frontend && npx vue-tsc --noEmit           → exit 0（构建产物在 CI 上验证）
```

8 个 skip = 6 个 postgres 参数（本机无 PostgreSQL，CI 真跑）+ 1 个需要可写临时目录的 OCR 用例
+ 1 个需要可写临时目录的迁移用例，全部是环境限制而非代码跳过。

> 本节的 332/8 是**第 1 梯队收尾时（2026-10-05）**的快照；第 2 梯队 6 项完成后已变为
> `pytest → 451 passed, 9 skipped`，最新快照见 §8。

## 8. 第 2 梯队执行结果（2026-10-07 完成）

逐项的根因、设计取舍、新增测试与仍存在的限制记录在
[`priority-fixes.md`](priority-fixes.md) 的事项三十六至四十一，这里只给结论与证据。

这一梯队原本标注为"可选，增强系统设计叙事"，实际六项都落在"出问题时才被发现"的位置上：
outbox 与 advisory lock 只在崩溃/并发下生效，库级约束只在有人绕过 ORM 写脏数据时生效，
可观测性只在排障时生效，对象存储只在多副本部署时生效，CI 门禁只在发布时生效。
所以下面的证据以"这个场景是怎么被写成测试的"为主；没有实测性能数字可报，
**#20 的工作流改动从未执行过**（需要 GitHub Actions；本机连 `docker compose up` 都没跑过），表格里已如实标注。

| # | 项目 | 状态 | 关键证据 |
|---|---|---|---|
| 15 | 外部索引 outbox + 可重放 | ✅ | `index_outbox` 表（`app/db/models.py:192` 的 `IndexJobRecord`）**刻意无外键**（行要活过文档/知识库删除，否则级联会抹掉"外部索引还需清理"这唯一证据）；摄取提交点 `app/core/store.py:358-373` 的 `save_document_index` 在**同一事务**里写 chunks + `_upsert_index_job(..., "upsert")`；重试预算在**行上**而非 broker 上——`INDEX_JOB_MAX_ATTEMPTS = 3`（`app/core/store.py:456-459`）、`fail_index_job`（:551-572）累加 `attempts` 到上限置 `failed`，`app/tasks.py:366-369` 注释 "No autoretry: the row itself carries the retry budget"；`claim_index_job`（:472-535）用 `with_for_update(skip_locked=True)` 认领、`rowcount != 1` 即 rollback，`stale_after_seconds=900` 允许回收死 worker 的行；`_replay_index_outbox`（`app/tasks.py:290-318`）驱动所有到期行并返回 `{"replayed", "failed"}`，一个坏文档不中断整轮 sweep；错误截断到 `last_error = (error or "")[:2000] or None`；删除路径 `delete_document`（:390-406）删行后补 `operation="delete"` intent，`delete_knowledge_base`（:232-267）`rowcount != 1` 则 rollback 且**不留 intent**；迁移 `alembic/versions/0012_index_outbox.py`（`down_revision = "0011_postgres_constraints"`）upgrade/downgrade 都先查 `get_table_names()` 故可重复执行；新增 `tests/test_index_outbox.py` **19 项**（同一事务提交 intent、只有一个 worker 领到、陈旧行可回收、新鲜行不提前 sweep、预算耗尽变成可见失败、delete intent、拒绝的删除不留 intent、回放发布 PG 的 chunk、回放清索引、坏文档不阻塞 sweep），`tests/test_tasks.py` 里 `test_external_index_failure_keeps_document_failed_and_db_unchanged` 被改写为 **`test_external_index_failure_leaves_a_replayable_row_behind`** 并新增 `test_a_parked_index_row_marks_the_document_failed` |
| 16 | Enum/CheckConstraint + PG JSONB + FK 级联 | ✅ | 模型是唯一事实来源：`app/db/models.py:51-53` 的 `_status_check(column, allowed)` 生成 `f"{column} IN ('a', 'b')"`，`documents`/`evaluations` 各带 `ck_documents_status` / `ck_evaluations_status`；`JSON_DOCUMENT = JSON().with_variant(JSONB(), "postgresql")`（:45-48）让 PG 落 JSONB、其它方言落泛型 JSON，**一套代码路径**；级联按归属分两类——内容随语料死（`documents.knowledge_base_id`、`chunks.document_id`/`chunks.knowledge_base_id`、`evaluation_datasets.knowledge_base_id`、`evaluation_examples.dataset_id` 全 `CASCADE`），评测历史活过被它测过的语料（`evaluations.knowledge_base_id`/`dataset_id` 用 `SET NULL` + `nullable=True`，:157-161）；SQLite 侧两个开关缺一不可——`json_serializer=lambda value: json.dumps(value, ensure_ascii=False)`（`app/core/store.py:74-83`，否则中文被转义存储、库不可读）+ 每连接 `PRAGMA foreign_keys=ON`（:89-104，SQLite **默认关闭**外键，不开则级联与 CHECK 静默失效）；迁移不重述逻辑而是从模型读出来：新增 `app/db/postgres_schema.py` 的 `desired_check_constraints()` / `jsonb_columns()` / `desired_foreign_keys()` 与 `upgrade_statements(inspector)` / `downgrade_statements(inspector)` 纯函数，`alembic/versions/0011_postgres_constraints.py` 只在 PostgreSQL 方言上执行它们（`bind.dialect.name != postgres_schema.PG_DIALECT` 返回 `[]`）；两个必须显式处理的坑——`index_outbox` 由 `0012` 才建出，所以 `upgrade/downgrade_statements` 用 `_existing_tables(inspector)` 过滤不存在的表（否则全新库的 `alembic upgrade head` 会在 `0011` 就给不存在的表加约束），JSONB 转换用 `USING NULLIF(col, '')::jsonb` 让空串历史行不中断迁移；新增 `tests/test_schema_constraints.py` **13 项**（`get_args(DocumentStatus) == DOCUMENT_STATUSES` 的 API/库集合守护、库里拒收未声明状态、每个声明状态都被接受、删知识库带走 documents/chunks/datasets 而 evaluations 的 `knowledge_base_id`/`dataset_id` 变 `None` 且 dataset_name 保留、跨租户删除被拒、`assert "全对" in str(raw)` 证明 JSON 未被转义）+ `tests/test_postgres_schema.py` **14 项**（四个 CHECK 名与条件逐字、三个 JSONB 列、CASCADE/SET NULL 两个集合、约束名与 legacy 名都稳定、upgrade 幂等、只动自己拥有的表、downgrade 还原 0010 形状且不含 `ON DELETE`、不容 `index_outbox`） |
| 17 | Prometheus 指标 + readiness 深检查 | ✅ | 手写 Prometheus 文本格式而不引入 `prometheus_client`：`app/core/metrics.py:74` 的 `_Histogram` + :127 的 `Metrics`，渲染出 `evalrag_http_requests_total{path,status}` / `evalrag_http_errors_total{path}` / `evalrag_http_request_duration_seconds_{bucket,sum,count}` / `evalrag_cache_{hits,misses,errors}_total{backend}`（`cache_backend_name` 从实例读 backend 标签，:97）/ `evalrag_retrieval_stage_duration_seconds_{bucket,sum,count}{stage}` / `evalrag_celery_tasks_{started,succeeded,failed}_total{task}` / `evalrag_db_pool_{size,checked_out,idle,overflow,max_overflow}{pool}` / `evalrag_external_backend_up{backend}`；采集点在操作边界——通道各记自己的时间、hybrid 只记合并段，DB pool 读不到 accessor 就**不发布样本**而不抛异常（保证 scrape 永远渲染得出来）；readiness（`app/api/routes/health.py`）按**启用配置**分支：`_check_redis`（:28）`asyncio.wait_for(client.ping(), timeout=_timeout(settings))`、`_check_database`（:53）、`_inspect_queue`（:62）同步 broker 探针经 `asyncio.to_thread`（无人应答 → `"no Celery worker answered the ping"`）、`_check_backends`（:99）把探测失败本身当健康信号，未启用的后端返回 `"skipped"` 而不是假装 OK；`GET /metrics`（:254）是 `PlainTextResponse` 且 `include_in_schema=False`；新增 `tests/test_observability_metrics.py` **25 项**（过期算 miss 不算 hit、redis 故障记 error 而不是 miss、无 metrics 的 cache 零开销、local/hybrid/retrieval_service 各阶段计数、`evalrag_db_pool_size{pool="default"} 5` 与 `max_overflow 3`、无 accessor 不抛异常、`/metrics` 同时出 pool 与 http 序列、gauge 0/1；readiness 侧 `ready` 时四个未启用依赖全是 `"skipped"`、无 worker 时 `degraded` 且错误含 "no Celery worker"、redis ping 失败含 "connection refused"、启用但探测失败 → 503 且 gauge 变 0、数据库不可达 → 503） |
| 18 | 上传落对象存储 + 预签名 URL | ✅ | 新增 `app/core/storage.py`：`ObjectStore` 协议（put/get/delete/presign_get）+ `LocalObjectStore`（默认）与 `S3ObjectStore`，`create_object_store(settings)` 只在 `OBJECT_STORE` 归一化等于 `"s3"` 时切 S3（写错一个字母退回本地而不是崩）；key 由 id+文件名推导、不改 schema——`document_key("doc-1","policy.txt") == "documents/doc-1/policy.txt"`，`_safe_name` 把非 `alnum/._-` 换成 `_`，于是 `../../etc/passwd` 落成 `documents/doc-1/.._.._etc_passwd`；本地写 `.part` 再 `temporary.replace(target)`（半截文件不以目标名出现）、越界 key 显式报 `object key escapes the store root`、`presign_get` 抛 `PresignUnsupported`；`download_document`（`app/api/routes/documents.py:195-232`）在 S3 上 307 重定向到预签名 URL、在本地读回字节返回，`Content-Disposition` 用 `f"attachment; filename*=UTF-8''{quote(filename)}"`（中文名直接拼会破坏响应头）；S3 实现支持第二个可选 client 给"签名 Host 与内部 endpoint 不同"的公开端点（否则预签名 URL 里是浏览器解析不了的 `http://minio:9000`），`get` 在 `finally` 里 `body.close()`；删除路径刻意 fail-soft（`delete_document`，:168-192）——行已删则对象残留只 warning 不失败，注释理由是"行已经没了，这正是请求承诺的"；上传路径相反是 fail-fast（存储抛错 → 503 "document could not be stored"，排队失败 → 置 failed + `_remove_upload` + 503）；`docker-compose.yml` 加 `minio` + 一次性 `createbuckets`（`mc mb --ignore-existing`）服务，api/worker 注入 `OBJECT_STORE: s3` / `S3_ENDPOINT_URL: http://minio:9000` / `S3_PUBLIC_ENDPOINT_URL: http://localhost:${MINIO_PORT:-9000}` 并删掉两处 `uploads_data` 挂载、卷换成 `minio_data`；`deploy/docker-compose.staging.yml` 与 `deploy/docker-compose.production.yml` 同样自带 `minio` + `createbuckets`，且把 `MINIO_ROOT_USER` / `MINIO_ROOT_PASSWORD` / `S3_PUBLIC_ENDPOINT_URL` 写成 `${VAR:?set ...}`——**不设置就拒绝启动**，理由是"悄悄用默认口令的生产栈比起不来更糟"（文件内注释原文）；新增 `tests/test_storage.py` **19 项**（key 推导含中文名、文件名逃不出前缀、空文件名仍可用、本地 round-trip 且不留 `.part`、覆盖写、缺失对象、delete 幂等、拒绝越界 key、本地不能发 URL、默认后端且拼错不改、只用 S3 时才建 S3、公开端点独立签名 client、put/get/delete/presign 的调用参数、缺 key 映射成 `ObjectMissing` 而真故障原样抛、文件名编码 `%E5%80%9F`）+ `tests/test_upload_streaming.py` 新增 6 项（按推导 key 存储并排队、文件名走不出 store root、存储拒绝写时 503、content 路由返回字节、content 路由 307 重定向、缺失对象） |
| 19 | Alembic advisory lock | ✅ | 新增 `app/db/migration_lock.py`：PG 用**独立连接**上的 session 级 advisory lock（注释理由：commit/rollback 不能提前释放，也不会漏进应用连接池），`MIGRATION_LOCK_KEY = 5_710_214_013_041_212`、`LOCK_ENV_VAR = "EVALRAG_MIGRATION_LOCK"`、`_DISABLED_VALUES = frozenset({"0","false","no","off"})`（**未设置=开启**，安全默认）、`lock_enabled(env=None)` 与 `@contextmanager migration_lock(engine)`；接进 Alembic 只改一行结构：`alembic/env.py` 的 `with connectable.connect() as connection:` → `with migration_lock(connectable), connectable.connect() as connection:`；非 PG 或显式关闭时 yield `False` 且**完全不建立连接**，SQLite 上 `alembic upgrade head` 行为逐字不变；新增 `tests/test_migration_lock.py` **9 项**，其中 `test_env_module_wires_the_lock_around_the_migration_run` **读 `alembic/env.py` 源码**断言 `from app.db.migration_lock import migration_lock` 与 `migration_lock(connectable)` 都在（防"helper 写好了但没接上"），`test_non_postgres_dialects_do_not_connect_at_all` 断言临时库文件根本没被创建、`test_disabled_lock_does_not_connect_to_postgres` 断言 `engine.connects == 0`，真并发一条 `test_a_second_migrator_waits_for_the_advisory_lock` 需 `EVALRAG_TEST_DATABASE_URL`（无则 skip，本机即为此 skip，也是全量 skip 从 8 变 **9** 的来源）；`0011`/`0012` 的 downgrade 都已实现且幂等，逐版本 downgrade 还原由 `tests/test_migrations.py::test_every_downgrade_restores_the_previous_schema` 守着（`0003` 的 downgrade 补齐是第 1 梯队 #8 的成果，本轮未改该文件） |
| 20 | CI 与 release 门禁 | ⚠️ 已实现，未在本地验证 | 三个门禁的定义抽成**可复用工作流** `gate-backend.yml` / `gate-frontend.yml` / `gate-eval.yml`（都 `on: workflow_call`），`ci.yml` 与 `release.yml` 共用同一份步骤（`gate-backend.yml:1-4` 注释：`needs` **不能跨工作流文件**引用 job，所以"发布要等门禁通过"只能靠复用同一份定义，而不是把步骤复制过去）；`release.yml` 因此把 `backend`/`frontend`/`eval-gate` 三个 job 用 `uses: ./.github/workflows/gate-*.yml` **真实建出来**，`images` job 才写得出 `needs: [backend, frontend, eval-gate]`——打 tag 不再等于自动发布；backend 门禁：`uv sync --frozen --extra dev` → `uv lock --check` → `uv run --frozen ruff check app tests alembic scripts` → `uv run --frozen mypy` → `uv run --frozen pytest -q --cov=app --cov-fail-under=80`（下限 80% 取自本地实测 86.7% 留的余量）；frontend 门禁：`npm ci` → `npm run lint` → `npm run format:check` → `npm run build` → `npm test`；eval 门禁：8 份合成文档 / 16 题的夹具语料跑真实链路后用 `scripts.check_eval_regression --tolerance 1e-6` 逐项比对基线（**不比延迟**，失败时上传 `/tmp/eval-gate.json`）；`ci.yml` 另外把 `postgres` job 从 `pip install -e ".[dev]"` 改为 `uv sync --frozen`（`uv.lock` 从装饰品变成事实来源，锁文件过期直接失败），新增 `docker-build` job（两镜像、`type=gha,scope=${{ matrix.image }},mode=max` 缓存、`push: false`）与 `audit` job（`uv export --frozen ... --no-emit-project` + `uvx pip-audit --no-deps`，用 `uvx` 临时拉起故不进生产依赖树）；**注意**：本机没有执行过任何工作流，GitHub 解析与 CI 绿灯都未验证，`docker-build` 只构建不推送、`audit --no-deps` 不覆盖传递依赖与镜像 OS 包 |

**本轮核实快照**（下面两条数字已在仓库内实际运行得出；本次文档任务未重跑它们，
也未重跑其他命令——其余未跑过的集中在末尾"未核实项"里）：

```
pytest                                        → 451 passed, 9 skipped
pytest tests/test_storage.py tests/test_upload_streaming.py tests/test_tasks.py
                                              → 52 passed（对象存储 + outbox 回放的专项口径）
```

`tests/test_migration_lock.py` 共 9 个用例、其中 1 个需要 `EVALRAG_TEST_DATABASE_URL`，
因此 9 个 skip 的构成是：**6 个 postgres 参数**（本机无 PostgreSQL，CI 真跑）
+ 1 个需要可写临时目录的 OCR 用例 + 1 个需要可写临时目录的迁移用例
+ 1 个需要 `EVALRAG_TEST_DATABASE_URL` 的 advisory lock 真并发用例（本轮新增）；
这四类的 `pytest.skip` / `pytest.mark.skipif` 位置已核对
（`tests/test_concurrency.py:97`、`tests/test_migrations.py:109,112`、
`tests/test_migration_lock.py:138`、`tests/test_ocr.py:28`），全部是环境限制而非代码跳过
（其中"6 个 postgres 参数"这个拆分沿用第 1 梯队 §7 的既有口径，未逐个重数）。

**未核实项**（如实列出，避免读者把"写了"当成"验过"）：`ruff` / `mypy` / `--cov-fail-under=80`
本次未重跑（第 1 梯队快照里的 `ruff → All checks passed!` 与 `mypy → Success` 仍是最近一次
本地记录）；`scripts.check_eval_regression` 与 `scripts.benchmark_local_retrieval` 未重跑；
`docker compose up` 未在本机执行，因此 `minio` / `createbuckets` 的启动时序与预签名 URL 的
浏览器可达性只在代码层面审阅；真实 Milvus / Elasticsearch / MinIO / Redis 集群均未连接
（相关测试用 fake client 与 monkeypatch）。

## 9. 后续项执行结果（2026-10-08 完成）

三项后续项（A/B/C）不是新的能力，而是把"能演示"补成"敢在别人机器上跑"：A 让浏览器不再持有
不可撤销的长期密钥，B 让最外层的 `App.vue` 第一次有 mount 级集成测试，C 让前端也有 lint/format
门禁。逐项的根因与取舍记在 [`priority-fixes.md`](priority-fixes.md) 的事项四十二（A），
这里只给结论与证据。

| 项 | 项目 | 状态 | 关键证据 |
|---|---|---|---|
| A | API Key 换服务端会话 + 短期令牌 | ✅ | 浏览器不再持久化任何密钥。新增 `app/core/sessions.py`：`SESSION_TOKEN_PREFIX = "ers_"`、`new_session_token()` = 前缀 + `secrets.token_urlsafe(32)`、`hash_token()` = sha256、`key_fingerprint()` = 哈希前 16 位、`bearer_token()` 解析 `Authorization`；新增 `api_sessions` 表（`app/db/models.py` 的 `ApiSessionRecord`，`token_hash` 唯一索引、**无 tenant 外键**因为租户在配置里）+ 迁移 `alembic/versions/0013_api_sessions.py`（幂等守卫，down_revision `0012_index_outbox`）；`app/api/routes/auth.py` 五个端点——`POST /api/v1/auth/session`（`X-API-Key` 换 `ers_...`，租户不符 403，`AUTH_ENABLED=false` 时也可签发但标 `authenticated=false`）、`GET`（自述）、`DELETE`（吊销自己）、`GET /api/v1/auth/sessions`（审计列表，带 `last_used_at`/`key_fingerprint`）、`DELETE /api/v1/auth/sessions/{id}`（跨租户 404）；`app/api/deps.py` 的 `authenticate()` 改为**先 Bearer 会话再 X-API-Key**，`current_session` 依赖负责 401 的三种原因（未知/已吊销/已过期）；`app/middleware.py` 的限流身份在无密钥时用 `session:<sha256 前 16 位>`（签发本身就要密钥，所以不能借此绕过限流）；`SESSION_TTL_SECONDS`（默认 3600）与 `SESSION_TOUCH_SECONDS`（默认 60，避免每个请求都写一次 `last_used_at`）。前端 `frontend/src/auth.ts` 重写：令牌只进 sessionStorage（`evalrag_session_token`），legacy `evalrag_api_key`/`evalrag_api_key_session` 删除而不迁移，`localStorage`/`sessionStorage` 取值本身可能抛异常故统一走 `storeOf(kind)`；`frontend/src/api.ts` 的 `requestHeaders()` 有令牌就发 `Authorization: Bearer`，并新增 `createSession`/`getSession`/`revokeSession`；`frontend/src/App.vue` 的 `login()` 先兑换令牌再存（失败连 tenant 都不写）、`logout()` 先尽力 `DELETE` 再清本地、`onMounted` 用 `GET /auth/session` 校验且失败即回落登录页。新增 `tests/test_auth_sessions.py` **19 项** + 前端 `auth.test.ts` 9 项、`App.test.ts` 扩到 12 项 |
| B | `App.vue` 级 mount 集成测试 | ✅ | `frontend/src/App.test.ts` 用 `mount(App)` + `vi.mock('./api')`（mock 整个 api 模块，`vi.hoisted` 规避提升顺序）覆盖 12 个场景：未登录只渲染 `.login-page` 且不发任何请求；挂载后按租户拉知识库/文档/数据集/评测；用 API Key 兑换令牌并写入 sessionStorage；兑换被拒只显示错误、不进入工作区；带令牌挂载时用 `GET /auth/session` 校验并在失效时回落登录页；`streamChat` 的 `onCitations`/`onText` 渲染 `.answer-text` 与 `.citation-list`；`event: error` 渲染 `.alert.error`；`api.search` 500 渲染错误。它证明的是"组件树 + 模板 + 状态机连起来是通的"，但仍在 jsdom 里跑，真实 SSE 流与 vite/esbuild 行为不在覆盖范围内 |
| C | 前端 ESLint + Prettier 接入 | ✅ | 新增 flat config `frontend/eslint.config.js`（`@eslint/js` recommended + `typescript-eslint` recommended + `eslint-plugin-vue` 的 `flat/recommended`，`**/*.vue` 用 `vue-eslint-parser` 且内层 `parserOptions.parser` 指向 ts 解析器，末尾 `eslint-config-prettier` 关掉与格式化冲突的规则；唯一显式关闭的是 `vue/multi-word-component-names`，因为 `App.vue` 是 `main.ts` 挂载的单一外壳，注释写明理由）；`frontend/.prettierrc.json`（`semi: false`、`singleQuote: true`、`printWidth: 100`、`trailingComma: "all"`）与 `.prettierignore`（`dist/`、`node_modules/`、`_npmcache/`、`package-lock.json`）；`package.json` 增加 `lint`（`eslint . --max-warnings 0`）与 `format:check`（`prettier --check .`）；`.github/workflows/gate-frontend.yml` 在 `npm ci` 之后插入这两步（顺序：`npm ci` → `lint` → `format:check` → `build` → `test`）。接入时按 Prettier 机械重排了 9 个已有文件，无逻辑改动 |

**本轮核实快照**（2026-10-08 在当前工作树实跑；前端数字来自真实 `vitest run`，不是等价执行器）：

```
pytest                                 → 470 passed, 9 skipped
ruff check app tests alembic scripts   → All checks passed!
mypy                                   → Success: no issues found in 49 source files
cd frontend && npm test                → 44 passed（6 files）
cd frontend && npm run lint            → exit 0（24 files, --max-warnings 0）
cd frontend && npm run format:check    → exit 0（All matched files use Prettier code style!）
cd frontend && npx vue-tsc --noEmit    → exit 0
```

一条值得记的过程证据：A 的前端部分先由 teammate 用"vite 不可用时的等价执行器"（`@vue/compiler-sfc`
+ `tsc` transpile，无 esbuild）跑出 44/44，但 Lead 用真实 `vitest run` 复跑时多出一个失败——
`src/auth.test.ts > survives storage that throws (private mode)` 在 `dropLegacyApiKeys()` 里抛
`SecurityError`：**`sessionStorage` 属性取值本身**就会抛（Safari 隐私模式、沙箱 iframe），
而这次取值发生在 `try` 之外，所以模块注释里"存储不可用就退回内存"的承诺并没有兑现。
修复方式是把存储访问收进 `storeOf(kind)`（内部 `try`）再由 `readQuietly`/`removeQuietly`/`writeQuietly`
调用，修后真实 `vitest` 44/44。这条也说明：**"等价执行器全绿"不能替代真实运行**，
尤其是断言本身就是关于运行时行为的那些用例。

仍未核实的部分：`npm run build`（`vite build` 走 esbuild，本机沙箱下无法执行）与 CI 里的
`gate-frontend` 真实运行都没有本地证据；`docker compose up`、真实 Milvus/Elasticsearch/MinIO/Redis、
GitHub Actions 解析同样未验证。
