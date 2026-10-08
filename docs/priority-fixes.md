# EvalRAG Enterprise 优先修复清单

> 建立日期：2026-09-26  
> 用途：代码复盘、版本规划、简历真实性检查。完成事项应补充验证命令和结果，不只勾选状态。

## P0：公开仓库或简历演示前

- [x] **LangSmith Dataset 增加租户命名空间**（2026-09-27 完成）
  - 风险：不同租户使用相同 Dataset 名称时，远端样本可能混存。
  - 位置：`app/api/routes/evaluations.py`、`app/core/langsmith_eval.py`
  - 验收：两个租户创建同名 Dataset 后映射到不同远端名称；测试覆盖同步与实验查询。
  - 结果：新增 `remote_dataset_name(tenant_id, name)`（`<name>--<tenant_hash[:12]>`），`create_dataset`、`ensure_dataset`、`run_experiment` 全部按租户解析远端名称；`tenant_hash` 后缀避免把原始租户标识写进远端系统。

- [x] **修复 Redis 故障降级异常类型**（2026-09-27 完成）
  - 风险：Redis 断连时 Cache/Rate Limit 抛出未处理异常，API 返回 500。
  - 位置：`app/core/cache.py`、`app/core/rate_limit.py`
  - 验收：捕获 `redis.exceptions.RedisError`；Redis 不可用时按既定策略降级；补充集成或故障注入测试。
  - 结果：两个后端改为捕获 `redis.exceptions.RedisError`（redis-py 的连接/超时异常是它的子类，此前捕获的内建 `ConnectionError`/`TimeoutError` 永远匹配不到）。缓存读失败按 miss 处理并打 warning；限流失败 fail-open 放行并打 warning；新增 `REDIS_FAILURES` 常量集中声明。

- [x] **保护真实 LLM/LangSmith 健康检查**（2026-09-27 完成）
  - 风险：公开 `/health/llm` 可被匿名重复调用并产生模型费用，且绕过 `/api/` 限流。
  - 位置：`app/api/routes/health.py`、`app/middleware.py`、`deploy/nginx/https.conf`
  - 验收：live/ready 保持廉价；外部连通检查需要认证、内网访问或独立管理端点，并有测试。
  - 结果：`require_health_admin` 依赖——本地环境（`APP_ENV=development/local/test`）或显式 `HEALTH_CHECKS_PUBLIC=true` 时放行；否则要求 `X-Health-Token` 与 `HEALTH_ADMIN_TOKEN` 常量时间比较，未配置令牌时返回 503 而不是放行。限流中间件由「只限 `/api/`」改为「豁免廉价路径清单」，因此这两个端点现在也计入限流。

- [x] **校验 Baseline Evaluation 租户归属**（2026-09-27 完成）
  - 风险：异步 Runner 可按其他租户的 Evaluation ID 计算差值，泄露聚合指标并污染结果。
  - 位置：`app/api/routes/evaluations.py`、`app/core/evaluation_runner.py`
  - 验收：创建任务及 Runner 执行时均验证当前评测与基线属于同一租户。
  - 结果：创建接口对跨租户基线返回与其他不存在情况相同的 404；Runner 的 `_baseline_diff` 在租户不匹配时跳过并打 warning。

- [x] **Staging 默认启用认证或仅绑定本机地址**（2026-09-27 完成）
  - 风险：认证关闭时服务端直接信任请求中的 `tenant_id`。
  - 位置：`.env.staging.example`、`deploy/docker-compose.staging.yml`
  - 验收：示例配置默认不暴露无认证多租户 API。
  - 结果：staging 示例改为 `AUTH_ENABLED=true` 且提供 `API_KEYS` 占位，并新增 `HEALTH_CHECKS_PUBLIC=false` / `HEALTH_ADMIN_TOKEN` 占位；`tests/test_config_examples.py` 锁死这两点。

- [x] **升级存在安全公告的 pytest 开发依赖**（2026-09-27 完成）
  - 当前：`pytest>=8.3,<9`，环境中为 8.4.2。
  - 扫描：`pip-audit` 报告 `PYSEC-2026-1845`，修复版本 9.0.3。
  - 验收：升级后完整测试通过。
  - 结果：`pyproject.toml` 改为 `pytest>=9.0.3,<10`，环境升级到 9.1.1；`pip-audit` 输出 `No known vulnerabilities found`；全量测试 116 passed。

## P0：RAG 能力真实性

- [x] **实现真实 BM25 与向量 Dense 检索**（2026-09-26 完成）
  - 当前问题：Sparse 只是词频统计；Dense 只是 Token 集合重合。
  - 目标：本地 BM25 使用 IDF、长度归一化、`k1/b`；Dense 使用 Embedding 与余弦相似度。
  - 验收：测试能证明语义向量排序与 BM25 长度/IDF 行为，不再以 Token overlap 冒充 Dense。

- [x] **将 Milvus/Elasticsearch 接入主检索和索引链路**（2026-09-26 完成）
  - 当前问题：适配器存在，但 `RetrievalService` 固定创建 `LocalRetriever`；默认索引为临时内存对象。
  - 目标：通过配置选择 local/external 后端；Worker 实际写入 Milvus/ES；查询实际从外部后端召回并 RRF。
  - 验收：Factory、故障降级、租户/知识库/版本过滤与集成测试完整。

- [x] **实现模型原生 Token Streaming**（2026-09-26 完成）
  - 当前问题：等待完整答案后按固定字符切片发送，并非模型端流式输出。
  - 目标：LLM 协议提供 `stream()`；retrieval/citations 在生成前发送；模型 token 到达即转发。
  - 验收：测试证明首个 token 在完整生成结束前到达；Mock 与 OpenAI-compatible 实现均覆盖。

## 已完成事项复盘（2026-09-26）

### 事项一：真实 BM25 与向量 Dense

- 根因：原 `retrieve()` 的 sparse 分支只统计查询词在文档中的出现次数，没有 IDF、平均文档长度和长度归一化；dense 分支统计查询与文档的 Token 集合重合数，与向量检索无关。
- 设计选择：本地 sparse 改为 Okapi BM25（语料级 IDF、`k1`、`b`、文档长度归一化）；本地 dense 改为 Embedding 余弦相似度。
- 替代方案与取舍：没有直接删除本地实现，因为需要默认无外部依赖可运行。保留确定性 `HashEmbedding` 作为默认/测试 fallback，同时支持 OpenAI-compatible Embedding（可配置 `dimensions` 并校验返回向量长度）。代价是本地 hash 向量不具备真实语义，只能用于离线回归。
- 新增测试：`tests/test_retrieval.py`（BM25 IDF/长度行为、dense 使用向量而非 Token 重合）、`tests/test_embeddings.py`。
- 验证命令及结果：`.venv/Scripts/python.exe -m pytest -q` → `95 passed`；`ruff check app tests alembic` → `All checks passed!`
- 仍存在的限制：本地 hash embedding 无语义能力；真实语义质量取决于部署时接入的 embedding 服务。

### 事项二：Milvus/Elasticsearch 进入主检索与索引链路

- 根因：`RetrievalService._build_retriever()` 无条件构造 `LocalRetriever`；`MilvusDenseRetriever`/`ElasticsearchBM25Retriever` 只是未被调用的适配器；Celery Worker 使用临时内存索引。
- 设计选择：新增 `create_retriever()` / `create_ingestion_pipeline()` 配置化工厂，按 `DENSE_RETRIEVAL_BACKEND`、`SPARSE_RETRIEVAL_BACKEND` 选择后端；外部检索与索引统一携带 `knowledge_base_id` 和 `version` 过滤；`.env.production.example` 显式选择 `milvus` / `elasticsearch` / `openai-compatible`。
- 替代方案与取舍：故障回退由“捕获所有异常”改为只降级明确的 `BackendUnavailableError`（连接/超时类），配置、鉴权、schema、维度错误必须直接失败，避免外部后端失效时被本地结果掩盖。生产示例默认 `EXTERNAL_RETRIEVAL_FALLBACK=false`。
- 加固项：ES 索引显式 mapping（`keyword`/`text`/`integer`）并对 bulk 逐项失败抛错；Milvus 校验已存在 collection 的字段、类型、维度与 COSINE metric；写入和查询前校验向量维度；版本字段限制为 `^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$`，避免 Milvus filter 拼接注入；检索缓存 key 纳入 embedding provider/model/dimensions 与外部 collection/index；新增文档级删除与 `reindex-document --force` 重建入口（CLI）；Milvus 同步 SDK 调用移入线程，不再阻塞事件循环。
- 新增测试：`tests/test_backends.py`、`tests/test_pipeline.py`、`tests/test_retrieval_service.py`、`tests/test_tasks.py`、`tests/test_indexing_contracts.py`、`tests/test_cli_reindex.py`。
- 验证命令及结果：pytest `95 passed`；ruff 通过；`docker compose -p evalrag-auth2 up --build -d` 全栈启动，上传→Celery 解析→Chunk→hybrid 检索→引用→SSE→Nginx 代理全链路通过。
- 仍存在的限制：未连接真实 Milvus/Elasticsearch 实例验证，SDK 调用契约由 fake client 测试覆盖；PostgreSQL 与外部索引之间仍是双写，缺少 outbox/重放（见 P1）。

### 事项三：模型原生 Token Streaming

- 根因：原实现先 `await llm.answer()` 得到完整答案，再按 24 字符切片发送，不是模型端流式。
- 设计选择：`LLM` 协议新增异步 `stream()`；OpenAI-compatible 实现以 `stream=true` 请求并增量解析 SSE delta；路由改为检索事件 → 引用事件 → 逐 token 转发 → trace → `[DONE]`；`rag.request` span 覆盖整个流生命周期，`generation.answer` 为其真实子 span。
- 替代方案与取舍：保留 `answer()` 非流式路径以兼容既有调用方与评测 Runner。新增受控 `event: error` 协议（`generation_failed`），失败时不输出 provider 原始消息、密钥或堆栈，也不输出 `trace` 事件；客户端取消时标记 span 为 `stream cancelled` 并逐层关闭上游 generator/HTTP client。
- 新增测试：`tests/test_llm.py`（分裂帧、单 chunk 多事件、CRLF、keepalive、provider error object、malformed JSON、HTTP 错误、资源关闭）、`tests/test_chat_stream.py`（首 token 时序、Trace 父子关系、取消与失败时序）。
- 验证命令及结果：pytest `95 passed`；ruff 通过；Docker 全链路冒烟中 SSE 事件顺序为 `retrieval → citations → token → trace → done`，首 token 在 1.0 秒左右到达。
- 仍存在的限制：首 token 时序由单元测试与本地 Docker 冒烟共同证明，未做真实公网 + Nginx 生产链路的 TTFT 压测；未验证反向代理缓冲配置在高并发下的表现。

## 已完成事项复盘（2026-09-27：工程化加固）

### 事项四：上传流式写入

- 根因：`app/api/routes/documents.py` 先 `await file.read()` 把整个上传读进内存，再做大小判断；超限请求在拒绝之前已经占用完整内存，`MAX_UPLOAD_MB` 不能真正防御内存耗尽。
- 设计选择：新增 `_stream_upload_to_disk()`，以固定 1 MiB 分块边读边写，累计超过上限立即抛 413 并停止读取；写入目标为同目录 `.part` 临时文件，成功后 `replace()` 原子改名；空文件返回 400；任何失败路径（含 413）都会删除临时文件。文件句柄写入通过 `run_blocking()` 交给线程，避免在事件循环上做同步磁盘 I/O。若 multipart 已给出 `file.size`，先用它做一次廉价预判。
- 替代方案与取舍：没有引入 `aiofiles` 或先落盘再校验的新依赖路径——分块读取 + 临时文件已能满足「内存上限与请求体大小解耦」，代价是多一次 rename（同文件系统内为原子操作）。也没有在反代层设置小 `client_max_body_size` 代替应用层校验，因为该值需要与 `MAX_UPLOAD_MB` 双处维护且无法覆盖直连 API 的场景。
- 新增测试：`tests/test_upload_streaming.py`（分块上限、超限提前中止读取、只剩 `.part` 被清理、空文件 400、HTTP 201 落盘路径与队列投递）。
- 验证命令及结果：`pytest` 全绿；Docker 全栈冒烟中上传 → `pending` → Worker → `ready`（`chunks=1`）通过；`data/uploads` 下无残留 `.part`。
- 仍存在的限制：未校验 magic/MIME；解析阶段仍无超时与内存上限；上传目录仍是共享本地卷。

### 事项五：Celery 并发幂等

- 根因：`process_document` 直接进行「读取当前状态 → 解析 → 写索引 → 置 ready」，没有任何抢占步骤。Celery 是 at-least-once 投递（worker 崩溃、broker 重投、`--force` 重建与定时重试都会造成同一文档被并发处理），两个 worker 会同时对同一文档重复解析、重复写外部索引、重复覆盖状态。
- 设计选择：数据库原子状态迁移。新增 `Store.claim_document()`，执行 `UPDATE documents SET status='processing' WHERE id=:id AND status IN ('pending','failed',...)`，通过 `cast(CursorResult, result).rowcount` 判断是否抢到；`process_document` 第一步 claim，抢不到就直接返回 `{'stage': 'not_claimed'}` 并结束，不抛异常（避免 Celery 重试放大竞争）。`--force` 重建走显式的 `force=True` 分支，否则已是 `ready` 的文档不会被重复处理。
- 替代方案与取舍：没有引入 Redis 分布式锁或 Celery `task_id` 去重——锁需要额外的过期与续租逻辑，去重只能防止同一任务 ID 重复投递，都防不住“不同任务处理同一文档”。数据库行级原子更新的条件与状态机本身保持一致，天然跨进程、跨语言、无额外依赖，代价是把幂等责任放在数据库上（由此单条 UPDATE 的行锁成为竞争点，但每文档一次，热路径不在它上面）。也没有使用 `SELECT ... FOR UPDATE`：需要显式事务与重试语义，而条件 UPDATE 更短、更贴近状态机。
- 新增测试：`tests/test_tasks.py`（重写：首次 claim 成功、已 ready 且无 force 时 `not_claimed`、force 重建、失败后置 `failed` 与重试）、`tests/test_task_idempotency.py`、`tests/test_schema_parity.py`。
- 验证命令及结果：pytest 全绿；Docker 全栈 Worker 日志为 `Task ... received` → `succeeded ... 'stage': 'indexed'`；在真实 PostgreSQL 上并发调用 8 次 `claim_document()` 结果为 `claims_granted: 1 of 8`、最终 `status=processing`；CLI `reindex-document` 无 `--force` 返回 `already_indexed`，带 `--force` 返回 `indexed`。
- 仍存在的限制：外部索引写入与 PostgreSQL 状态仍非事务性双写，worker 在“外部索引成功、状态更新前”崩溃会留下 `processing` 悬挂文档（需要 outbox/对账或超时回收，见 P1）；没有为 `processing` 设置超时回收任务。

### 事项六：消除异步接口中的同步阻塞

- 根因：三类同步调用出现在 `async def` 处理函数里——Redis 客户端、SQLAlchemy 同步 Session、同步文件写入——它们直接阻塞事件循环，单个慢查询会让同一 worker 上的所有请求排队。
- 设计选择：缓存与限流接口整体异步化（`Cache.get/set`、`RateLimiter.check` 改为 `async def`，Redis 后端改用 `redis.asyncio`），中间件改为 `await limiter.check(...)`，`RetrievalService` 的缓存读写改为 await；存储调用统一经 `app/core/concurrency.py::run_blocking()`（`asyncio.to_thread`）在路由层下移，路由本身仍保持异步签名，`app/api/routes/evaluations.py` 等同步路由继续由 FastAPI 线程池承载。
- 替代方案与取舍：没有把持久层重写为 `AsyncSession`/`asyncpg`——那会牵动 store、tasks、CLI、测试与 Alembic 迁移的调用面，风险与收益不匹配；`to_thread` 让阻塞点显式且有界，代价是每请求多一次线程调度，并且线程池默认上限（约 40）在高并发下仍是新的排队点。没有使用全局 `run_in_executor` 包装整个中间件：那会掩盖真正的阻塞来源。
- 新增测试：`tests/test_async_non_blocking.py`（用 `httpx.ASGITransport` 在测试自身的循环里驱动应用，比较存储调用与事件循环的线程 ID，覆盖检索、上传、删除三个路由）、`tests/test_cache_rate_limit.py`（异步缓存/限流）、`tests/test_retrieval_service.py`（缓存替身改为异步）。
- 验证命令及结果：pytest 全绿（含 3 个线程 ID 断言）；把 `run_blocking()` 临时改回直接调用后这 3 个测试立即失败（证明断言有效）；Docker 全链路冒烟通过。
- 仍存在的限制：未做事件循环阻塞时长的量化压测；SQLAlchemy 同步引擎与线程池上限仍限制单进程并发。

### 事项七：统一 Schema 演进

- 根因：`app/core/store.py` 里有一份手写的 SQLite `ALTER TABLE`/建索引逻辑，启动时与 Alembic 迁移并行执行。两条路径都改同一张表，谁生效取决于运行顺序与方言，生产（PostgreSQL）与本地（SQLite）实际经历了不同的 schema 演进，模型文件成了第三个真相来源。
- 设计选择：删除手写 102 行升级逻辑，schema 只由 Alembic 负责；新增 `alembic/versions/0007_document_updated_at.py` 让模型中的 `DocumentRecord.updated_at` 有对应迁移；新增 `tests/test_schema_parity.py`，在全新 SQLite 上分别用迁移与 `Base.metadata.create_all()` 建表，比对表集合与关键列，锁死“迁移 == 模型”。
- 替代方案与取舍：没有保留手写路径并只做“幂等化”——双轨本身才是问题；也没有改用 `create_all()` 代替迁移，因为那放弃版本化与生产升级能力。代价是本地首次启动必须跑 `alembic upgrade head`（入口脚本已包含），任何模型变更都必须同时补迁移，否则 parity 测试失败。
- 新增测试：`tests/test_schema_parity.py`。
- 验证命令及结果：全新 SQLite 上 `alembic upgrade head` → `0007_document_updated_at (head)`，`documents` 表含 `updated_at`；parity 测试通过；pytest 全绿；Docker migrate 容器执行迁移后 API/Worker 正常。
- 仍存在的限制：迁移仍是 forward-only，未补全 `downgrade`；`0001`–`0007` 只覆盖当前表结构，历史迁移未在 PostgreSQL 上逐一回归。

## 已完成事项复盘（2026-09-27 第二轮：公开仓库安全加固）

### 事项八：租户隔离（LangSmith 命名空间与 baseline 归属）

- 根因：LangSmith Dataset 在账号内是全局命名空间，本地 `EvaluationDataset.name` 直接作为远端名称，两个租户取同名就会写进同一个远端 Dataset；`EvaluationRunner._baseline_diff` 用 `store.get_evaluation(baseline_id)` 取基线，而该方法不做租户过滤，跨租户基线会把别人的聚合指标算进本租户的 `baseline_diff`，再通过 `/evaluations/{id}/results` 返回。
- 设计选择：远端名称统一走 `remote_dataset_name(tenant_id, name)` = `<name>--<tenant_hash(tenant_id)[:12]>`，`create_dataset`、`ensure_dataset`、`run_experiment` 三个入口都按租户解析，避免「查不到就建」的路径各自为政；基线在创建接口做租户校验（跨租户返回与不存在一致的 404，不回显对方 ID 是否存在），Runner 侧再做一次防御性跳过并打 warning。
- 替代方案与取舍：没有给 `store.get_evaluation` 加租户参数——它同时被 Runner、CLI 和 health 使用，改签名会波及无关调用方，且路由层已有「取回后再比对租户」的既有模式；代价是校验点分散在两处，需要同时维护。远端名称用 hash 后缀而不是完整租户名，牺牲了运维可读性换取租户标识不外泄（可由本地库反查对应关系）。
- 新增测试：`tests/test_langsmith_tenant_namespace.py`（命名空间稳定性、同名 Dataset 映射到不同远端名并走命名空间查询、实验按命名空间运行）、`tests/test_evaluation_tenant_scope.py`（跨租户基线 404 且不落库、Runner 跳过跨租户基线但同租户仍算差值）。
- 验证命令及结果：pytest 全绿（新增 5 项）。
- 仍存在的限制：命名空间无法阻止同租户内数据集重名（本地已有 409 校验）；远端孤儿 Dataset 仍不会随租户删除而清理。

### 事项九：付费健康检查的保护与限流范围

- 根因：`/health/llm` 会真实调用模型供应商，`/health/langsmith` 会调用 LangSmith 接口，两者都挂在根路由上且无认证；限流中间件的条件是 `path.startswith("/api/")`，因此这两个路径既不受认证约束也不受限流约束，构成「匿名可触发的付费调用」。
- 设计选择：新增 `require_health_admin` 依赖与两个配置项。本地环境（`APP_ENV` ∈ development/local/test）或显式 `HEALTH_CHECKS_PUBLIC=true` 时放行，保持开发体验；其他环境要求 `X-Health-Token` 与 `HEALTH_ADMIN_TOKEN` 做 `secrets.compare_digest` 比较，令牌未配置时返回 503；限流中间件由「白名单前缀」改为「豁免清单」（`/health`、`/health/live`、`/health/ready`、`/metrics`、docs 与根路径），其余路径包括这两个端点全部计入限流。
- 替代方案与取舍：没有采用「内网地址自动放行」——生产链路里 API 的调用方是 Nginx，来源永远是私网地址，这条规则等于没有保护；也没有把连通检查拆成独立管理端口，那需要额外的监听、反代与部署变更，收益不如令牌直接。限流豁免清单是精确匹配而不是前缀匹配，避免 `/health` 前缀把 `/health/llm` 一起豁免掉——这个细节在写测试时被显式验证过。
- 新增测试：`tests/test_health_admin.py`（非开发环境缺令牌 401、错误令牌 401、正确令牌 200、未配置令牌 503、live/ready 仍 200、第二次调用 `/health/llm` 返回 429 且廉价路径不被计入）。
- 验证命令及结果：pytest 全绿；临时把 503 分支改成放行后该测试立即失败（证明断言有效）。
- 仍存在的限制：令牌是静态共享密钥，没有轮换与审计；`/metrics` 仍未鉴权，属于下一步；Nginx 层没有 IP 白名单作为第二道防线。

### 事项十：部署示例与依赖扫描

- 根因：`.env.staging.example` 默认 `AUTH_ENABLED=false` 且 `API_KEYS={}`，服务端会直接信任请求里的 `tenant_id`，示例即等于「可被任意人读写多租户数据」；`pyproject.toml` 的开发依赖固定在 `pytest>=8.3,<9`，命中 `PYSEC-2026-1845`（修复版本 9.0.3）。
- 设计选择：staging 示例默认 `AUTH_ENABLED=true` 并提供 `API_KEYS` 占位；三个示例文件都补上 `HEALTH_CHECKS_PUBLIC=false` 与 `HEALTH_ADMIN_TOKEN`，本地示例留空（`APP_ENV=development` 自动豁免）；`pyproject.toml` 改为 `pytest>=9.0.3,<10` 并升级环境。新增 `tests/test_config_examples.py` 解析示例文件并断言认证开启、健康检查令牌存在，避免示例再次退化成无认证默认。
- 替代方案与取舍：没有把 staging 改成仅绑定 `127.0.0.1`——那样前端与联调都不可用，认证是更符合真实部署的选择。示例里的令牌与 API Key 都是占位符而非真实凭据，因此仓库本身不含秘密，但也意味着「部署时必须替换」要靠文档和这两条测试提醒，而不是技术强制。
- 新增测试：`tests/test_config_examples.py`。
- 验证命令及结果：`pip-audit --progress-spinner off` → `No known vulnerabilities found`；pytest 9.1.1 下全量 116 passed。
- 仍存在的限制：示例文件无法强制替换占位符；CI 尚未把 `pip-audit` 纳入门禁。

## 已完成事项复盘（2026-09-27 第三轮：golden set 审计与真实评测数字）

原始数据：`docs/evaluation/golden-set-2026-09-27.json`、完整分析 `docs/evaluation-report.md`。

### 事项十一：评测集质量审计

- 做法：`scripts/audit_golden_set.py` 用与摄取完全相同的解析器重新抽取语料，
  把每题的证据引文回原文里逐字查找，核对"是否在标注页"。
- 结果：52/52 引文命中且页码一致；字段完整、问题不重复、13 份文件各 4 题、无越界页码；
  答案中的数字均能在引文中找到。
- 新发现的三个局限（记录在评测报告，未改动题集）：
  1. 5 份 docx 解析页码恒为 1，20 道题的 `page_hit` 退化成文档级召回；
  2. 《民法典》163/316 chunk（52% 语料）却只占 4/52 题；
  3. 三份《贷款管理办法》的"贷款人"定义引文逐字相同，文档级标签对这三题不可区分。

### 事项十二：评测指标从单一 k 扩展到多 k + 分位延迟

- 动机：`retrieval_metrics(example, k)` 只在 `top_k` 一个截断点上算指标，
  重新跑一次 top_k=1 会改变候选池，跨 k 的数字不可比。
- 设计选择：一次检索出排名，对同一条排名计算 Recall@{1,3,5}/nDCG@{3,5}/{precision}，
  截断点上限受 `top_k` 约束（`top_k=3` 时不出 `recall_at_5`，避免造数）；
  聚合层增加 `latency_ms_p50/p95`（nearest-rank，空集不返回伪值）。
- 新增测试：`tests/test_evaluation.py`（多截断点、分位函数边界）、
  `tests/test_evaluation_runner.py`（Runner 落库的指标键、`retrieved` 里带 `chunk_id`/`text`）。
- 附带的契约变化：评测结果每条 retrieved 现在带 `chunk_id` 与 `text`，
  使结果可复核（也是 `quote_hit` 这类脚本侧指标能做出来的前提）。

### 事项十三：第一次真实评测给出的三个结论

- 指标饱和：BM25+词面重排在文档级/页级指标上全部 1.000，题集没有留下改进空间；
  随机 5-chunk 基线为文档 0.246 / 页 0.160。
- 负向优化被证实：`EMBEDDING_PROVIDER=hash` 的 32 维向量使 dense 通道 R@1 只有 0.692，
  等权 RRF 融合后 hybrid R@1 0.904 < BM25 0.981；逐题对照 6:0（BM25+重排严格更优，无反向案例）。
- 延迟归因：单次检索 p50 685 ms（BM25）/ 1087 ms（hybrid），缓存关闭、316 chunk；
  由配置差值可分解为"全语料嵌入 ~400 ms + BM25 全量打分 ~280 ms"。

### 事项十四：顺手修正的两处工程问题

- `app/core/ingestion.py` 由 `import fitz` 改为 `import pymupdf`：
  每次摄取都会打印弃用警告，而 `pyproject.toml` 已约束 `pymupdf>=1.25`。
- `scripts/` 纳入 lint 门禁（`.github/workflows/ci.yml` 与 README 的验证命令），
  并修掉 `scripts/generate_golden_set.py` 的类型检查告警。

### 事项十五：passage 级指标进入评测产品

- 根因：文档级 recall 在 13 份文档的语料上饱和（BM25+重排全为 1.000），
  而"答段是否排第一"在配置间的差距是 0.769 : 0.288——差异被聚合口径吃掉了。
- 设计选择：给样例加可空的 `evidence_quote`（migration `0008`），
  Runner 只对**带引文的样例**计算 `passage_hit`/`passage_at_1`/`passage_mrr`，
  逐题结果里记 `passage_rank`；不带引文的老数据集指标含义不变。
- 替代方案与取舍：另建一列 passage 标签表（结构更干净，但把"标注完整度"割裂到两张表）；
  或把引文塞进 `expected_answer`（无需迁移，但污染答案字段且无法与答案长度解耦）。
- 验证命令及结果：`pytest` 全量 126 passed；两处 RED 探针（弃掉存储写入、弃掉 Runner 计算）让断言分别失败；
  重跑 `scripts.run_golden_experiment` 后平台 `passage@1` 与脚本侧独立实现逐位一致。
- 仍存在的限制：引文仍是"命中该 chunk"的严格子串判定，未做片段级打分归一；
  LangSmith 同步路径**刻意不上传文档内容**（`redact()` 边界），因此 passage 指标只在本地评测可用。

### 事项十六：让评测平台承载难样本（多跳 / 应拒答）

- 根因：题集要加难样本时，"期望文档"必须是单值且必填的——多跳题的答案分布在两份文件、
  应拒答题根本没有期望文档，现有标注表达不出来。
- 设计选择：跳用 JSON 列 `expected_evidence_json`（`[{document_id, page, quote}]`）+ `should_refuse`，
  `expected_document_id` 变可空；不变量放在 Pydantic `model_validator`（应拒答不许带证据、
  其余样例至少一个标签、主标签必须与第一跳一致）。
- 替代方案与取舍：子表 `evaluation_example_targets` 结构更规范，但每次读取多一趟查询、
  且要迁移全部历史样例；跳是整体读取的载荷，从不按跳过滤，所以 JSON 更合适，
  代价是 SQL 无法约束形状（由 Pydantic 兜住）。
- 指标口径变化与兼容性：`recall@k` 变集合命中比例、`page_hit` 要求每个期望 (文档,页) 都在、
  `passage_hit` 要求每跳引文都命中；单跳样例的数值与改造前逐位相同，靠既有测试守住。
- 验证：`tests/test_evaluation_multi_hop.py` 7 项（含"部分命中不算成功"与两条校验拒绝路径）；
  全量 133 passed；`alembic upgrade head` → `0009`，`downgrade -1` 在存在应拒答样本时拒绝执行。
- 仍存在的限制：应拒答只用"检索是否为空"近似，答案层的拒答正确率未纳入；
  评测 Runner 尚未支持并发、超时与失败重试（见 P1 条目）。

### 事项十七：生成 v2 难样本题集

- 根因：v1 的提示词**要求**问题写出制度名称，而制度全称逐字出现在原文首页，
  等于把最强的词面线索直接送给 BM25；上一轮量化过它值约 11.5 个百分点 R@1。
  另外题集只有"单文档、必有答案"一种形态，多跳与应拒答题无法标注。
- 设计选择：新增 `scripts/generate_golden_set_v2.py`，四类题分别生成、分别校验：
  同义改写（禁止出现《》与任何文件名称/简称、必须日常说法）、
  跨文档多跳（两跳必须一侧一项、每跳引文逐字可查）、
  等价多标签（**从语料里扫描逐字重复的句子**再让模型就它提问，不靠模型记忆）、
  应拒答（相邻但确实不在语料里的问题）。
  校验失败就丢弃并记录原因（`law/golden_eval_v2.rejected.json`），不写入题集。
- 两个真实坑：①《商业银行法》那一份连续 3 轮 0 产出的原因是 **docx 在管线里只有一页**，
  模型却编造了页码 → 提示词改为"该文件只有一页，page 必须写 1"后该份产出 4 题；
  ②句法切分被 PDF 硬换行切碎 → 先把续行接回再切句，并按"定义型/样板文字"排序过滤
  （"自某日起施行"这类共享样板会让一道题有多个正确答案，属于坏题而不是难题）。
- 验证与结果：75 题（52 改写 + 9 多跳 + 6 等价 + 8 应拒答）；
  `scripts/audit_golden_set.py` 新增 v2 段：**87/87 跳的引文都在所标页码上、0 处越界、
  0 题提到任何文件名称**，并新增"问题与答案句的词面重合度"指标（v1 中位数 0.429 → v2 0.333；
  按题型：改写 0.333、多跳 0.259、等价 0.689——等价题必须复述共享原句）。
  评测端实测（`docs/evaluation/golden-set-v2-2026-09-27.json`）：
  **纯 BM25 的 R@1 从 v1 的 0.981 掉到 0.660、page_hit 1.000→0.851、passage@1 0.769→0.612**
  ——题集的词面重合值约 32 pp R@1；重排在难集上增益为 0（0.660→0.660）；
  hybrid 仍低于纯 BM25（0.532）；8 道负样本全部检索到内容，且其 top 分数与 12% 的正常问题重叠，
  阈值拒答这条路被数据否掉。三次重跑质量指标逐位相同。
- 替代方案与取舍：不改 v1 而是新建 v2（保留可比基线）；
  负样本的"该拒答"目前只由"检索是否为空"近似，答案层拒答正确率仍未纳入。
- 仍存在的限制：多跳的"必须两份文件才能回答"由提示词约束 + 一次重试保证，
  没有独立的机器判定（`rejected.json` 里记录了 3 次"两跳来自同一侧"）；
  等价题的候选池受语料限制，全库只有 23 句共享句（其中 3 句是定义型）。

### 事项十八：无文字层文件不再"成功地上传了一份检索不到的东西"

- 根因：上传校验只看**扩展名**，而 `extract_text` 对扫描版 PDF 返回的是
  `[(1, ""), (2, ""), ...]`——页数正常、字符全空。下游 `chunk_pages` 得到 0 个 chunk，
  `process_document` 照样把文档置为 `ready`、进度 100%、`stage=indexed`、不报错。
  文档此后永远检索不到，也没有任何地方能看出为什么。
- 复现（真实代码路径，非构造断言）：用一份真实的 11 页县级财政扫描件（无文字层，
  见 `语料候选-2026-09-27/`）走过完整上传与抽取链路，得到
  `result={'status': 'ready', 'stage': 'indexed', 'progress': '100'}`、
  `document.status=ready chunks=0 error=None`、库里 0 个 chunk。
- 设计选择：在抽取后立即检查**可提取字符数**（`sum(len(text.strip()))`），为 0 时抛
  `EmptyExtractionError`，走既有的失败路径：状态 `failed`、写入原因
  `N page(s), 0 extractable characters - likely a scanned document without a text layer (OCR required)`，
  并且**在任何索引写入之前**中止（测试断言 dense/sparse 两个索引器一次都没被调用）。
  失败原因复用已有的 `Document.error_message`，`GET /api/v1/documents/{id}` 直接可见。
- 替代方案与取舍：① 新增 `needs_ocr` 状态语义更准，但要动 DB 枚举、迁移、前端渲染和
  状态机，本轮用 `failed` + 可执行的原因文字，成本低且不引入新状态；
  ② 在 Worker 里做 OCR（`pymupdf` 只能取文字层，需要 Tesseract/PaddleOCR 及镜像体积），
  这是"支持扫描件"而不是"不假装成功"，属于后续独立事项；
  ③ 把空抽取也纳入自动重试——被否掉，这是文件的稳定属性，重试只是把同一个失败重复三遍，
  因此任务注册里加了 `dont_autoretry_for=(EmptyExtractionError,)`。
- 新增测试：`tests/test_tasks.py::test_scanned_file_without_a_text_layer_fails_instead_of_indexing_nothing`
  （RED 已验证）、`::test_empty_extraction_is_not_retried_by_the_queue`（RED 已验证）、
  `::test_a_partly_machine_readable_document_is_still_indexed`（边界回归：只作废页数全空的文件，
  有文字的文档不受影响）。
- 验证命令及结果：`pytest tests/test_tasks.py` → 8 passed；全量 `pytest -q` → 157 passed；`ruff` 干净。
- 仍存在的限制：白名单仍然只看扩展名，所以真正的判定发生在 Worker 而不是上传响应里；
  扩展名正确但内容损坏（如伪装成 .pdf 的文本）走通用异常路径，原因文字是解析器原文而不是人话；
  语料里目前没有扫描件，这个缺陷是在候选语料上发现的，仓库内没有真实扫描件做端到端回归。

### 事项十九：`.txt`/`.md` 的 GB18030 解码

- 根因：纯文本分支是 `payload.decode("utf-8", errors="replace")`。中文公文式文本文件大量是
  GBK/GB18030，用 UTF-8 宽松解码不会失败——它把每个汉字变成 U+FFFD，**静默产出乱码并照样入库**。
  实测一句 25 字的中文按 GB18030 编码后，替换字符 35 个。
- 设计选择：按 `("utf-8-sig", "gb18030")` 顺序严格解码，全部失败才回退 `errors="replace"`。
  `utf-8-sig` 顺带解决 BOM：原来 BOM 会作为 `\ufeff` 留在正文首字符，进入第一个 chunk 的正文。
  GB18030 是 GBK 的超集，覆盖范围更宽；顺序上 UTF-8 优先，因为它是新文件的实际编码。
- 替代方案与取舍：不引入 `charset-normalizer`/`chardet` 这类探测库——多一个依赖只为两三种编码，
  而"先 UTF-8 再 GB18030"能覆盖实际会遇到的情况；代价是"恰好同时是合法 UTF-8 的 GBK 字节"
  会被判成 UTF-8（GBK 汉字对极少，且那种文件本来就无解）。
- 新增测试：`tests/test_ingestion.py` 4 例（GB18030 解码、UTF-8 仍按 UTF-8、BOM 被去掉、
  两者都不匹配时不抛异常），前两例 RED 已验证。
- 仍存在的限制：这是"猜编码"，没有根据文件声明或统计特征判定；
  `.docx`/`.pdf` 的编码由各自的库负责，不走这条路径。

### 事项二十：文档格式支持的范围与理由

- 起因：语料体检（13 份 = PDF 8 + DOCX 5）显示格式与发文机关都太单一，而模板类规则、
  网页公文、表格、扫描件完全没覆盖。用仓库外的候选语料包（10 个真实文件，逐一跑真实抽取链路）
  把"能不能解析、解析出来是什么样"都测了一遍，再决定支持边界。
- 决策表（按投入产出排序，不是按技术难度）：

| 格式 | 决策 | 理由 |
|---|---|---|
| `.html` / `.htm` | **支持**（2026-09-27 完成） | 地方性法规、司法解释常常只以网页发布；标准库 `html.parser` 就够，不新增依赖 |
| `.xlsx` | **支持**（2026-09-27 完成） | 真正的问题是分块策略：按字符切会把表格压平，数字找不到列名 |
| `.xls` | **不做** | `xlrd` 只读且已停止维护，同 `.doc`：老格式让上传方另存更划算 |
| `.doc` | **不做** | 需要 `antiword`/LibreOffice 这类外部二进制，或维护成本更高的纯 Python 解析；收益仅是"多认一个后缀"，却把外部二进制带进镜像，影响可复现性与部署体积。正确做法是让上传方另存为 `.docx` |
| 图片 / 扫描件 | **不装 OCR 引擎，只做接口与状态**（2026-09-27 完成） | Tesseract/PaddleOCR 要系统二进制和 GB 级镜像；做成可插拔后端 + `needs_ocr` 状态能拿到绝大部分设计收益，而不把 demo 稳定性押上去 |

- 验证与实测（HTML，用自己的 3 份真实政府网页跑抽取链路）：

| 文件 | 原始 HTML | 抽取正文 | 行数 | chunk | 备注 |
|---|---|---|---|---|---|
| 北京市地方金融监督管理条例 | 52,530 B | 8,634 字符 | 155 | 13 | 样板词清零（改前残留 `首页`/`版权所有`/`京ICP`） |
| 最高法民间借贷司法解释 | 41,651 B | 9,766 字符 | 213 | 15 | 同上，改前残留 `首页`/`版权所有`/`京ICP`/十六进制 token |
| 中央财政农业保险保费补贴管理办法（网页版） | 26,982 B | **718 字符** | 19 | 1 | **是"附件壳"页面**：正文只有标题和下载链接，内容在附件 PDF 里 → 不进语料 |

- 设计选择：两段式抽正文。① 结构启发式：丢 `script/style/nav/header/footer/aside` 等元素，
  按 class/id 关键词丢站点框架，但**带内容类关键词（content/article/main/TRS…）的属性优先保留**
  （`article-header` 里通常是标题，不能当框架丢掉）；② 行级样板过滤：`版权所有`/`ICP备`/十六进制
  token/`×` 这类实测残留按行丢，且**只丢 ≤40 字符的短行**——法律条文里的"主办单位"出现在长句中，
  不会被误杀。HTML 抽取结果只有一个逻辑页（页码恒为 1），与 DOCX 一致。
- 替代方案与取舍：不做正文密度算法（readability 那类）或按站点模板抽取——两者都能更干净，
  但前者要引依赖、后者要针对每家政府网站维护模板，超出这个项目该有的规模；
  代价是启发式仍会漏掉零星碎片（实测残留：`注册`、`评价建议`、`|`）。
  也没有按 `<meta charset>` 解码：政府网页声明的编码经常与实际不符，复用纯文本那条
  "UTF-8 → GB18030"的梯子更稳。
- 新增测试：`tests/test_ingestion_html.py` 9 例（RED 验证 8 例：7 例是"格式不被识别"的
  `ValueError` RED，1 例是行级过滤缺失的断言 RED；`test_a_legal_sentence_mentioning_a_chrome_word_survives`
  是写时即绿的边界护栏）；`tests/test_upload_streaming.py` 增加 `.html` 被接受的 2 例、
  并把 `.html` 从拒收名单移出（白名单消息同步改为 `pdf, docx, html, txt, md`）；前端 `accept` 同步。
- 仍存在的限制：网页公文的正文抽取是启发式，站点框架命名千变万化，一定会有漏网；
  "附件壳"页面（正文只有标题 + 一个 PDF 链接）能通过上传但与空文档等价，目前只能靠选语料时人工判断；
  HTML 一律视为单页，网页里的分页/章节结构在引用里看不出来。

- 表格（`.xlsx`）的设计选择与实测：新增依赖 `openpyxl`（`pypi` 上维护活跃、
  且能在测试里直接生成真实 xlsx 作为 fixture，不必往仓库塞二进制样例）。
  抽取时**每个数据行重复自己的列名**，序列化成 `工作表名 第N行 省辖市: 郑州市 | 市县区: 巩义市`
  这样的自描述文本，然后交给原有的固定窗口分块器——**分块器不需要认识表格**，
  只要一个 chunk 装得下一行，行里的"列名: 值"就是完整的。页码取工作表序号。
  - 实测（真实 54×6 补贴测算表，`_table_chunk_probe.py`）：把表格按行拼成文本再分块（最自然的做法）
    得到 1,175 字符 / 2 chunk，**取值出现 69 次、其中 27 次（39.1%）所在 chunk 里没有它的列名**；
    行级自描述得到 3,529 字符 / 6 chunk，取值出现 98 次、**0 次丢失列名（0.0%）**。
    代价是文本变长约 3 倍（列名重复），这个代价必须明说。
  - 真实文件推翻了一个想当然的假设：这张表的第 1 行是 `附件1`、第 2 行是 `102个县（市）名单`、
    第 3 行空、**第 4 行才是表头**，而且表头是两套重复的 `序号/省辖市/市县区`（表里并排放了两组名单）。
    因此表头判定改成"第一个非空单元格 ≥2 的行"，其上方的标题行作为正文保留，
    否则 `附件1` 会被当成第一列列名。已写成测试 `test_title_rows_above_the_header_are_kept_as_content`。
- 新增测试：`tests/test_ingestion_tables.py` 9 例，全部 RED 验证（首轮 9 例全红：
  `.xlsx` 不被识别的 `ValueError`；其中 4 例在实现后暴露了测试 fixture 与实现的两处真实分歧，
  修完转绿——百分比要显式设置 `number_format`，表头不在第 1 行）。
  另有 `tests/test_upload_streaming.py` 的 `.xlsx` 被接受 1 例、`.xls` 仍在拒收名单。
- 仍存在的限制：合并单元格/两行表头会让部分列名退化成"第N列"；重复列名不加区分
  （`序号` 出现两次时两条 `序号: 值` 无法区分属于哪一组）；
  公式按缓存值读取（用程序生成、从未被 Excel 打开过的文件可能取到空值）；
  workbook 整体读入内存（大表需要上限）；`.xls` 与 `.doc` 一律拒收。

- OCR 的设计选择（`needs_ocr` 路由）：新增 `app/core/ocr.py`，把 OCR 定义成 `OcrBackend` 协议 +
  `create_ocr_backend(settings)`，配置为默认值 `OCR_BACKEND=none` 时返回 `None`（不引入任何系统依赖）。
  `TesseractBackend` 用 pymupdf 把每页渲染成 PNG，再调 `tesseract <png> stdout -l chi_sim+eng` 取文字，
  **不需要 pytesseract 之类的 Python 包**，只需二进制 + 中文语言包。Worker 在"抽出 0 字符"时区分三种情况：
  没配后端 / 后端不可用（`OcrUnavailableError`）/ OCR 跑完也没文字，三者都落到 `needs_ocr` 状态并把原因
  写进 `error_message`，而不是笼统地 `failed`——文件是好的，缺的是这个部署的 OCR 能力。
  异常类型上 `NeedsOcrError` 继承 `EmptyExtractionError`（语义仍是"没抽到文字"），两者都不进自动重试。
- 一个容易漏掉的连带改动：前端 `pollDocument` 原来只把 `ready`/`failed` 当终态，新增 `needs_ocr`
  之后必须同步，否则扫描件会让前端一直轮询；样式里也补了 `.status.needs_ocr`。
- 新增测试：`tests/test_tasks.py` 5 例（无后端 → `needs_ocr`、配了后端 → 文字入库且状态 `ready`、
  引擎不可用 → `needs_ocr`、OCR 无产出 → `needs_ocr`、`NeedsOcrError` 不进自动重试）；
  `tests/test_ocr.py` 5 例（默认不建后端、tesseract 参数来自配置、未知后端名报错、
  二进制缺失报 `OcrUnavailableError` 而不是崩掉在 FileNotFoundError、非 PDF 输入被拒）。
  诚实标注：worker 的 5 例是 `AttributeError` 型 RED（新异常/新函数尚不存在）；
  `test_ocr.py` 的 5 例与实现同时诞生，属于契约测试，不是 RED 验证。
- 仍存在的限制：**没有安装 tesseract，也没有用真实扫描件跑过端到端 OCR**——这条链路只在假后端上
  验证过契约；OCR 质量（中文识别率、超过 `OCR_MAX_PAGES` 后的行为、倾斜/低分辨率页的预处理）
  完全没测；`needs_ocr` 目前没有"配置好引擎后重投"的入口，只能重新上传或 `reindex-document --force`；
  `TesseractBackend` 只渲染 PDF（图片上传仍在上传层被 400 拒收）。

### 事项二十一：语料从 13 份扩到 18 份（并整轮重跑）

- 起因：原语料只有 PDF/DOCX 两种格式、发文机关 3 家，且《民法典》一份占了 54% 的字符；
  模板类规则、网页公文、表格、扫描件都没覆盖。格式支持到位后，把候选包里能解析的文件一次性并入，
  再整轮重跑两套题集（用户明确接受重跑代价，所以不做增量拼接）。
- 本次并入 5 份（放 `law/`，原文按既有策略不进仓库）：

| 文件 | 格式 | 补了什么 |
|---|---|---|
| `14_行政法规_国务院_融资担保公司监督管理条例_2017.pdf` | PDF | 新发文机关**国务院**、新类别**行政法规** |
| `15_重要补充_财政部_中央财政农业保险保费补贴管理办法_2021.pdf` | PDF | 农业保险补贴这一支的部委规范性文件 |
| `16_地方性法规_北京市人大常委会_北京市地方金融监督管理条例_2021.html` | HTML | 新类别**地方性法规**、网页来源，首次真实使用 HTML 正文抽取 |
| `17_司法解释_最高人民法院_关于审理民间借贷案件适用法律若干问题的规定_2020.html` | HTML | 新类别**司法解释**、新机关**最高人民法院**、民间借贷这个高频业务场景 |
| `18_测算表_河南省财政厅_农业保险保费补贴资金测算表_2022.xlsx` | XLSX | 唯一的**表格**语料（54×6），用来验证行级序列化在真实检索里的表现 |

- 明确**没有**并进去的候选文件与理由（同样是决策记录，避免"凑数"）：
  - `民航局中小机场补贴预算方案(.xls)`：域外内容（中小机场补贴不属于农村金融），
    且 `.xls` 本身不在白名单；留作格式验证样本，不往语料里凑数。
  - `中央财政农业保险保费补贴管理办法（网页版）`：实测只抽出 718 字符，是"附件壳"页面
    （正文在附件 PDF 里），入库与空文档等价；同一份文件的 PDF 版本已经并入。
  - `县财政扫描件(.pdf)`：无文字层且未装 OCR 引擎；它在缺陷复现（事项十八）里已经发挥作用。
  - `金融支持新型工业化长图(.jpg)`：图片上传在上传层被 400 拒收。
- 语料体检（并入前后，均由生产链路实测）：

| 指标 | 13 份（旧） | 18 份（新） |
|---|---|---|
| 文档数 / chunk 数 | 13 / 316 | 18 / 386（+70） |
| 格式分布 | PDF 8 + DOCX 5 | **PDF 10 + DOCX 5 + HTML 2 + XLSX 1** |
| 抽取字符总数 | 206,446 | **245,740** |
| 发文主体 | 3 家 | **9 个**（+ 国务院、北京市人大常委会、最高人民法院、河南省财政厅） |
| 类别 | 基础法律/现行核心/重要补充/历史参考 | 再 + 行政法规、地方性法规、司法解释、测算表 |
| 《民法典》占比 | 111,320 字符 / **54%** | 111,318 字符 / **45.3%**（分母变大，集中度下降） |
| 前 4 大文档占比 | — | 59.0% |
- 一条顺带修掉的隐患：白名单原来散在三处（上传路由的正则、harness 的 `{.pdf,.docx}` 过滤、
  抽取分支），其中 harness 那份会**静默跳过**新格式——即"文件放进 `law/` 却没进索引，
  而且评测照跑不误"。现在收成 `app/core/ingestion.py` 里的
  `SUPPORTED_SUFFIXES`/`SUPPORTED_LABEL` 单一来源，三处引用同一常量。
- manifest 更新方式：没有手写 id。新增的 5 条由一次性探针 `_corpus_import_probe.py`
  走生产 worker 真实导入后产出（真实 `document_id`、chunk 数、sha256、字节数），
  原有 13 条一字未动——因为审计用 manifest 里的 id 核对已绑定样例（`document ids resolvable: 52/52` 保持不变）。
- 仍存在的限制：新并入的 5 份**没有对应的评测题**（两套题集都是为原 13 份标注的），
  所以它们在本轮评测里的身份是"干扰文档"而不是"被考察对象"，审计输出里
  `documents_without_questions` 列出的正是这 5 份；这也意味着本轮指标变化
  混着两个因素（语料变大 + 干扰项变多），不能单独归因给某一个。

### 事项二十二：给新增的 5 份文档补题（v3），生成侧加两道闸门

- 起因：语料扩到 18 份后（事项二十一），新增的 5 份只能当干扰项，"新并入的语料本身能不能被检索到"
  无法回答；而这批文件带 2 种全新格式（HTML/XLSX），格式支持有没有真正打通，
  需要"被考察对象"而不是"干扰项"来证明。
- 做法：给这 5 份各出 4 题（共 20 题，`law/golden_eval_v3.json`），沿用 v2 的去泄漏方法论
  （不出现《》、不写文件名/简称、用日常说法），并在生成器上补三处能力：

| 改动 | 为什么 |
|---|---|
| `--documents` 只对指定文档出题（数字前缀或完整名字段） | 原来只能对 `law/` 全量出题，18 份会产出 72 题 |
| 单页规则按**实际页数**判定（原来只有 `.docx` 才说"只有一页"） | HTML/表格导出同样是单页，模型会按"第X条"编页码 → 引文回验必然失败 |
| **两道闸门**：① 问题在问生效/施行/发布日期 → 拒；② 引文在别的文档里也出现 → 拒 | 去泄漏之后这两类题看得见答案却**指不唯一** |

- 闸门是**被真实缺陷推出来的**，不是预防性设计：第一轮（不带闸门）的 20 题里，
  逐题查引文跨文档唯一性时发现两类各 1 道——"这份规定从哪一天开始正式生效？"
  （引文本身唯一，但 18 份法规都有施行日期，去泄漏后指向不唯一）、
  "监管部门去一家典当行现场检查时，检查人员至少要有几个人？"
  （引文 `检查人员不得少于2人` 在《融资担保公司监督管理条例》里逐字出现）。
  闸门用这两个**已知坏样本**验证过会拦（`boilerplate=True` / `shared=True`），合法题不误杀；
  带闸门重跑 20/20 一次通过、0 丢弃，被拦的题会记进 `law/golden_eval_v3.rejected.json`（不静默丢弃）。
- 标注回验（`scripts/audit_golden_set.py` 新增 v3 段）：**20/20 跳的引文都在所标页码上、
  0 处页码越界、0 题提到任何文件名**；"问题与答案句的词面重合度"中位数 0.214
  （v2 0.333、v1 0.429）。
- 实测（同一份 18 文档语料、六组配置）：

| 配置 | v3 R@1 | page_hit | passage@1 |
|---|---|---|---|
| sparse-bm25 | 0.800 | 0.900 | 0.700 |
| sparse-bm25-rerank | 0.800 | 0.900 | 0.700 |
| dense-hash | 0.300 | 0.300 | 0.050 |
| hybrid-rrf | 0.550 | 0.750 | 0.400 |
| hybrid-rrf-rerank（及默认） | 0.650 | 0.750 | 0.550 |

  逐文档：doc 15（财政部 PDF）与 **doc 18（河南测算表 XLSX）都是 4/4 全中**，
  doc 16（北京条例 HTML）最弱（R@1 0.50，其中 1 题连 top-5 都没进）。
  **表格 4/4 是"行级自描述序列化"这条设计决策的直接回报**：按字符切把表格压平的话，
  "第 102 位是哪个县"这种题不可能稳定命中。
  与原有文档同语料、同风格对照：新文档 R@1 0.800 vs 原文档（v2 同义改写 52 题）0.731——
  只差 1 道题，在 20 题粒度（±1 题 = 5 pp）里属噪声，所以只能说"**没有证据显示新格式更难**"，
  不能反过来说"新格式更好检索"。
- 仍存在的限制：
  - 测算表 4 题里 3 题是同一模板（"第 N 位是哪个县"）；该附件只有县名单、没有金额，
    可问的维度本来就少——这 4 题的作用是验证"表格行级序列化可被检索"，不代表表格问答的难度；
  - doc 16 是唯一有真漏检的文档（"监管谈话"题在任何配置下都不进 top-5），
    推测与网页样板（目录/导航/页脚）挤占 chunk 有关，但没有单独做消融实验；
  - 20 题的规模下，任何 5 pp 以内的差异都不能当结论。

### 事项二十三：混合检索负优化的定位、四种修法，与一个不可复现缺陷

- 起因：事项二十一/二十二只把现象说清（等权 hybrid 低于纯 BM25，v2 上 0.660 → 0.527），
  三个怀疑——32 维哈希不是语义通道、候选不截断、重排只重排融合后候选——都还是**推测**。
  "发现并修负向优化"这句话要站得住，得给出机制数字，并且真的有一条路能修回去。
- 做法分四块：

| 改动 | 为什么这么改 |
|---|---|
| 融合方式抽成 `Fusion` spec，经 evaluation 的 `parameters` 通路下发（`app/core/retrieval.py`） | 不动 API/DB：`retrieval_mode` 是校验+持久化字段，为一个实验改契约不值；`create_retriever` 默认 `None` = 原等权实现，产品默认行为不变 |
| 新增四个变体：`rrf-bm25-heavy`（0.3/0.7）、`rrf-truncate-5`、`convex`（逐通道 min-max 归一化分数）、`convex-bm25-heavy` | 每个变体只隔离一个变量：权重、候选长度、分数幅度 |
| `scripts/probe_fusion.py`：走产品路径（每通道各取 20 个候选再融合，与 `HybridRetriever` 逐字等价） | 直接调 `retrieve()` 会融合全量排序，把长尾影响算大，能"证明"一个产品里并不存在的机制 |
| harness 加 `--configs` 过滤、付费预估与确认、用量/花费记账与降级检测 | 11 组配置全跑 = 付费后端 10 倍花费；且降级是静默的，不检测就会把词面重排的结果当语义重排 |

- 机制（页级探针，v2 的 52 道单跳题 / v3 的 20 道）：

| 融合方式 | BM25 页级第一 → 融合后仍第一 | 被挤掉 | 掉出 top-5 | 救回 |
|---|---|---|---|---|
| v2 等权 RRF | 35 → 16 | 19 | 6 | 2 |
| v2 加权 RRF | 35 → 20 | 15 | 6 | 1 |
| v2 截断 RRF | 35 → 22 | 13 | 0 | 2 |
| v2 convex 加权 | 35 → **34** | **1** | 0 | 0 |
| v3 等权 RRF | 16 → 8 | 8 | 3 | 2 |
| v3 convex 加权 | 16 → **15** | **1** | 0 | 1 |

  对照：dense 通道单独在同一批题上的页级第一只有 7/52 与 2/20（BM25 是 35/52、16/20）——
  **等权把正确率 13% 的通道与 67% 的通道当作同等可信的两票**，这就是"下降"的直接来源。
- 修法效果（同一份语料、同一套题，文档级 R@1）：

| 配置 | v1（52） | v2（75，单跳 52） | v3（20） |
|---|---|---|---|
| 纯 BM25 | 1.000 | 0.660（单跳 38/52） | 0.800 |
| 等权 RRF | 0.904 | 0.527 | 0.600 |
| 只加权 | 0.942 | 0.581 | 0.650 |
| 只截断 | 0.923 | 0.537 | 0.500 |
| 只换分数式 | 0.923 | 0.557 | 0.700 |
| **分数 + 加权** | 1.000 | 0.646（单跳 37/52） | **0.850（单跳 17/20）** |
| **+ 语义重排（TypeSafe）** | — | **0.769（单跳 45/52）** | **0.950（单跳 19/20）** |

  v3 上"分数 + 加权"已经**超过纯 BM25**（0.850 vs 0.800，page 0.950 vs 0.900）；
  v2 上仍差 1 道单跳题，但换成真语义通道后就反超了（0.769 vs 0.660）。
  **只加权不够、只截断没用、分数幅度是主要缺失项**——这三条都有上表支撑。
- 语义重排（`RERANK_BACKEND=typesafe`，默认关闭）：按官方 rerank cookbook 一候选一请求
  （`noul` 判断题），8 路并发，429 按 `retry-after` 退避；产物里记调用数/用量/花费，
  并用"实际调用数 == 题数 × 候选数"检出静默降级（两轮都是 1500/1500 与 400/400，`degraded=false`）。
  代价：每查询 p50 1.27s → 3.09s，v2 全量 1500 次调用 $0.0697；多跳题 0.500 → 0.500 一道没涨
  （重排改不了候选集），并有 2 道变差。
- **顺带查出一个不可复现缺陷**：`Chunk.id = f"{document_id}:{index}"`，`document_id` 每次导入
  重新生成的 UUID，而 `store.get_chunks` 是 `order_by(ChunkRecord.id)`——所以融合的平局胜负
  原本由**导入顺序**决定。同一份语料、同一套 v3 题集、只差平局规则/导入顺序，`hybrid-rrf` 的
  R@1 跑出过 **0.550 与 0.700**。修法：破平只用内容（`-分数, 最好名次, page, 正文`），
  并加两条测试——"调换传参顺序结果不变"与"调换 chunk id 结果不变"（后者 RED 验证：改回 id 破平立刻红）。
  产物侧补了可复现性检查：v3 两次独立导入、11 组配置质量指标逐位相同（延迟 p50 波动 8%–9%）。
- 仍存在的限制：
  - 语义重排只有两套小规模题集的证据（386 chunk、20/75 题），且它有外部依赖与出网成本，
    默认关闭——不能写成"系统具备语义重排能力"；
  - 多跳题一条没涨，说明瓶颈已经从"排序"转到"召回"（候选窗口 20 个里没有正确文档），
    下一步应该做的是扩候选/加通道，而不是继续加强排序器；
  - `convex` 的归一化是按查询内 min-max，个别查询的分数分布极端时归一化会失真，
    没有单独做鲁棒性消融；
  - 融合把 p50 从 0.86s 抬到 1.21s（v2，缓存关闭），最好的结果才是打平纯 BM25——
    纯 BM25 在这个语料上仍是性价比最高的通道。

### 事项二十四：查询管线的结构性重复劳动（每查询 774 次嵌入 → 稳态 1 次）

- 起因：§6 的已知局限里挂着一条"瓶颈是每查询重建检索器并把整个语料重新嵌入"，当时的证据是
  `sparse` 与 `dense` 的 `embed()` 调用次数**完全相同**（每查询 387 次）——"只做 BM25"也在为
  全语料嵌入付费。定位到了，但一直没修。
- 做法是三处结构性改动，而不是调参：

| 改动 | 为什么 |
|---|---|
| `retrieve()` 的通道改为**按需计算** | 原来两路都先算完再按 mode 分支返回：BM25 只需要词频统计，却为此嵌入了整个语料 |
| 两条通道**共用同一批向量** | hybrid 原来在 dense 通道嵌一遍，sparse 侧调用 `retrieve()` 时又用**它自己新建的**默认 embedding 嵌了一遍 |
| 进程级向量缓存 + 检索器复用 | 缓存键是（模型标识 + 文本哈希），所以换模型不会串味、换实例仍能命中；检索器按（知识库、版本、mode、融合、语料指纹）复用，语料一变指纹就变，不会serve 陈旧索引 |
| HTTP 嵌入后端**批量 + 复用客户端** | 顺带修掉"每个 chunk 一个 `httpx.AsyncClient`"（含 TLS 握手），并按响应里的 `index` 还原顺序而不是信任返回顺序 |

- 实测（386 chunk 语料、缓存关闭、同一脚本）：

| 配置 | 嵌入调用/查询（改前） | 改后冷启动 | 改后稳态 | p50（改前 → 改后） |
|---|---|---|---|---|
| sparse | 387 | **0** | **0** | 638.9 → **191.8 ms** |
| dense | 387 | 387 | 1 | 448.4 → **4.6 ms** |
| hybrid | 774 | 387 | **1** | 1102.0 → **193.6 ms** |
| hybrid + 重排 + 改写 | 1548 | 774 | 2 | 2229.8 → **437.4 ms** |

  "冷启动"是缓存为空时的首次查询（本地后端绕不开的一次性成本），"稳态"指语料已缓存且
  **问题各不相同**——连问题都重复的话 query 向量也会命中，测出来的"0 次嵌入"是假象。
- **验收条件：质量指标逐位不变。** 用同一套 v3 题集重跑 11 组配置，再与提交产物逐项比对，
  结论是**一致（0 处回归）**；只有延迟变了。性能改动如果不能证明"没动质量"，就不该合。
- 新增测试 `tests/test_retrieval_pipeline.py`（10 条）锁三条契约：sparse 不碰嵌入、同一语料只嵌
  一次、批量且命中缓存不发请求；改前分别是 387/774/每文本一请求，属**行为级 RED**（不是
  ImportError 那种假红）。
- 仍存在的限制：向量缓存是**进程级**的，多进程部署下每个 worker 各一份冷启动；386 chunk 时
  32 维只占 1.5 MB，换 1024 维真实模型约 33 MB（上限 4096 条）；**没做过并发压测**，
  报告里仍然没有 QPS/吞吐数字。

### 事项二十五：把检索质量门禁装进 CI

- 起因：单测证明"代码按预期跑"，证明不了"召回没被改坏"。本轮工作里出现 3 次"改一行、指标悄悄变"，
  其中一次（破平改内容键后 v3 的 `hybrid-rrf` 从 0.550 变 0.600）是靠人工逐份比对产物才发现的。
- 做法：`scripts/check_eval_regression.py` 比对两份产物，CI 里用一份**提交进仓库的夹具语料**
  （`tests/fixtures/eval_gate/`：8 份合成文件 / 8 个 chunk / 16 题，引文 15/15 回验）跑真实链路。
  不能直接用 `law/`——法规原文都在 `.gitignore` 里，CI 没有语料。
- 判据：每个质量标量逐项比对；**身份校验**（题集来源/题数/文档数/chunk 数/嵌入 provider/重排后端）
  不一致时直接失败，而不是给一个 Δ；延迟**不设门禁**（同机 ±10%，CI 更吵），只打印；
  默认容差 1e-6 只吸收跨平台浮点差异。
- 验证过它会拦：未改代码 → exit 0；注入"`convex` 融合忽略权重"的回归 → **exit 1** 并点名 18 项
  （如 `hybrid-convex-weighted.mrr 0.7056 → 0.6722`）；恢复 → exit 0；拿不同语料产物比对 →
  身份校验拦下（`chunks 不一致：基线 8 vs 本次 386`）。
- 夹具自洽性也有测试（`tests/test_eval_gate_fixture.py`）：引文必须回验、每份文件都要被考到、
  **基线必须与当前语料匹配**——防止"改了语料忘重生成基线"让门禁开始误报（那比没有门禁更坏）。
- 仍存在的限制：夹具只有 8 个候选、16 题，挡得住**结构性破坏**，挡不住 0.01 量级的质量漂移；
  大规模矩阵（v1/v2/v3 × 11 组配置，每轮 5–14 分钟）仍是人工触发，不进 CI。

## 已完成事项复盘（2026-10-04：指标口径与引用可核验性）

### 事项二十六：`document_version` 的默认值语义

- 根因：见 P1 清单里该条的复现记录。`"latest"` 被当成等值过滤器，于是"没写版本"等于"只搜 label 恰好是 latest 的 chunk"，
  用显式版本上传的文档在默认查询下永远检索不到，而且没有任何地方说明原因。
- 设计选择：默认值改为 `None`，语义是"不过滤版本"；空字符串（前端输入框留空）在 Pydantic `mode="before"` 校验器里
  归一为 `None`，避免"版本名叫空串"这种不存在的第三态。`"latest"` 保留为**字面标签**，仍可精确指定。
  `app/core/retrieval_service.py` 与 `EvaluationRunner` 的默认值同步改成 `None`，前端 `queryVersion` 初值与回填改为空串、
  请求体发 `null`，与同页"空 = 全部版本"的展示逻辑一致。
- 替代方案与取舍：另一种修法是让 `latest` 解析为"每个文档的最大版本"，但那要求给版本定义全序——
  版本是任意字符串（`v9`、`2026-09`），没有可靠的大小关系，只能靠文档的创建时间间接推断，
  等于把"标签"偷偷变成"查询语义"。选默认不过滤，代价是同一文档的多版本会同时进候选池（返回重复内容），
  收益是默认行为不再静默丢数据。
- 新增测试：`tests/test_document_versions.py`（默认命中 `v9` 文档、显式版本仍过滤、空白版本视为不过滤、schema 默认值）。
- 验证命令及结果：`pytest tests/test_document_versions.py` → 4 passed；全量 267 passed, 1 skipped。
- 仍存在的限制：多版本语料的"只看最新"需要调用方显式传版本；`latest` 这个字面标签没有任何特殊含义，
  容易让使用者误以为它代表"最新"——README 与前端提示需要明确这一点。

### 事项二十七：引用校验从空壳变成真校验

- 根因：`app/core/citations.py::validate_citations` 只判断"答案非空且证据非空"，函数名与 docstring 承诺的
  "每个引用都要属于检索证据"从未实现；流式路径（`app/core/rag.py::stream_with_evidence`）根本不校验。
  于是模型编造一个不存在的文档/页码时，用户看到的仍是一段**看起来有出处**的回答。
- 设计选择：`citation_report(answer, evidence)` 解析 `[...]` 片段，判定规则刻意保守——
  只有"文档名命中本次检索"或"带显式页码标记"才算一次引用，`[1]`、`[注]`、`[附件二]` 一律当普通文字，
  避免误杀正确答案。命中不了的算 `unknown`，文档对但页码未检索到的算 `ungrounded`，两者任一非空即拒答。
  "完全没引用"不算完整性问题（mock/本地模型本来就不引用），要强制引用由 `Settings.citation_required` 打开。
- 替代方案与取舍：可以让"必须带引用"成为默认，但那样 mock 与多数本地模型的回答会被全量拒答，
  把配置问题伪装成模型问题；也可以只在提示词里要求引用格式，但提示词约束不了幻觉。
  流式路径无法撤回已发出的 token，所以改为在结尾发 `event: error` + `code=ungrounded_citation`，
  不发 `trace` 事件，与生成失败的终态保持一致。
- 新增测试：`tests/test_citations.py`（11 项：6 种页码写法、4 种普通括号不被误判、文档/页码幻觉、空答案、无证据）、
  `tests/test_rag.py`（拒答与非流式返回）、`tests/test_chat_stream.py::test_chat_stream_reports_an_answer_that_cites_unretrieved_evidence`。
- 验证命令及结果：`pytest tests/test_citations.py tests/test_rag.py tests/test_chat_stream.py` → 全绿。
- 仍存在的限制：只校验引用的**存在性**，不校验引用与被引段落的语义一致性（引用真实但断章取义仍会通过）；
  `citation_required` 默认关闭，所以默认配置下"零引用的回答"仍会返回。

### 事项二十八：nDCG 归一化与 any 模式的 recall 口径

- 根因一：`ndcg_at_k` 对每个期望跳取 `1/log2(rank+1)` 后**除以跳数**，是 `DCG/n` 而不是 `DCG/IDCG`。
  两跳不可能同时排第一，所以两跳题的满分是 `(1 + 1/log2 3)/2 = 0.8155`，三跳题是 0.7103——
  一个**完美排名也拿不到 1** 的指标被当作 nDCG 发布。
- 根因二：`recall_at_k` 忽略 `mode`。等价多标签题（`mode="any"`）的标签是可互换的，
  按"命中标签比例"算会把一个**完全正确**的回答记成 1/3。
- 设计选择：nDCG 改为除以 `IDCG = Σ_{i=1..min(n,k)} 1/log2(i+1)`（`any` 模式下理想排名为 1，IDCG = 1）；
  `recall_at_k` 在 `any` 模式下与 `any_target_at_k` 一致（全有或全无），`all` 模式保持集合召回。
  同时给 `precision_at_k` 补上口径说明：它是"passage 命中数 / k"对文档级标签，属于代理指标，没有任何已发布表格使用它。
- 替代方案与取舍：也可以把这个指标改名成 `dcg@k` 并保留原算法，但那样"0.83 的 nDCG"仍需每次解释，
  且跨数据集不可比；归一化是更小的认知负担，代价是历史产物里该列数值会变（见下）。
- 新增测试：`tests/test_evaluation.py`（完美排名必须得 1.0、某跳滑落后落在 0.9–0.95 且小于 1）、
  `tests/test_evaluation_evidence_mode.py`（any 模式 recall@2 = 1.0，recall@1 = 0.0）。
- 验证命令及结果：CI 门禁实测**未受影响**——`check_eval_regression --tolerance 1e-6` 全部 `+0.0000`、exit 0，
  因为夹具的 16 题全是单跳、无 any 模式，两种口径下数值相同。
- 仍存在的限制：`docs/evaluation/*.json` 里已提交的 v1/v2/v3 产物仍是**旧口径**算出来的，
  多跳题的 `ndcg_at_*` 与 any 模式题的 `recall_*` 与当前代码不一致；重生成需要未入库的 `law/` 原文与 5–14 分钟，
  本轮**没有**重跑。要用新口径复核历史结论请重跑对应命令。

### 事项二十九：指标标签基数与延迟直方图

- 根因一：`MetricsMiddleware` 把 `request.url.path` 直接当标签，`/api/v1/documents/<uuid>` 每个文档 id
  都新建一条时间序列，进程内 `Counter` 无界增长，`/metrics` 随时间越拉越长。
- 根因二：延迟只输出 `_sum`，没有 `_count` 与 `_bucket`，Prometheus 算不出 P95，"阶段六：Prometheus /metrics"的承诺是空的。
- 设计选择：标签改用路由模板（`scope["route"].path`，回退到用 `path_params` 重建，再回退到正则归一化），
  并补 `status` 维度；延迟改为固定桶的直方图（11 个桶 + `+Inf` + `_count`/`_sum`），
  渲染时把非累积桶转成累积桶。404 这类没有模板的路径由 `normalize_path` 收敛。
- 替代方案与取舍：没有引入 `prometheus_client`（会新增依赖并把标签基数问题留给使用者），
  仍然手写渲染；代价是自己维护桶定义与转义，收益是零依赖且行为完全可测。
- 新增测试：`tests/test_metrics.py`（`normalize_path` 只收敛标识符、5 个不同 UUID 只产生 1 条时间序列、
  桶累积值与 `_count`/`_sum`、经真实中间件的 `{document_id}` 标签、未匹配路由被收敛）。
- 验证命令及结果：`pytest tests/test_metrics.py` → 6 passed。
- 仍存在的限制：仍是单进程计数（多 worker 需要 `prometheus_client` 的 multiprocess collector）；
  `/metrics` 自身没有鉴权，生产改为 Nginx 层按私有网段 allow/deny。

### 事项三十：测试隔离、门禁退出码与两处数据/代码缺陷

- 根因一（测试不隔离）：`tests/test_app.py` 用模块级 `TestClient(app)`，而 `app` 会从开发者 `.env` 读配置，
  于是测试写进真实的 `data/evalrag.db`，还会继承真实的 LLM/LangSmith key（跑一次测试会发出付费 trace）。
- 根因二（门禁会撒谎）：`scripts/check_eval_regression.py` 的成功行含 `✓`，在 GBK 控制台/管道下
  `UnicodeEncodeError`，把一次**通过**的质量门禁变成 exit 1。
- 根因三（数据与消费端）：`law/golden_eval_v2.json` 里有一道题重复出现（同一句"这份规定从哪一天开始正式生效？"
  问的是两部不同法规），而 harness 用 `{question: example}` 建索引，两行里有一行被**按另一行的标签打分**；
  重复检测只存在于 v1 的审计路径，生成集这条路径没有。
- 设计选择：测试改用 `tests/conftest.py` 的 `isolated_settings()/client` 夹具，数据库是
  私有共享缓存内存库（不落盘、跨连接同构）；门禁脚本在导入时把 `stdout/stderr` 的编码错误策略改为 `replace`，
  **降级字符而不降级退出码**；审计脚本给生成集补上 `duplicate_questions`/`unique_questions`；
  harness 改为按 `example_id` 建索引（runner 本来就逐行回传该字段），并删掉仓库里不可导入的死文件 `evaluation`
  （与 `evaluation.py` 内容重复，`tests/test_langsmith_runner.py` import 的是后者）。
- 替代方案与取舍：也可以给重复题改文案再重生成 v2，但那会让所有已发布数字失去可比基线；
  按 id 建索引既修正了误归因，又保留了两道题各自有效（同一问题问两部法规，答案不同）的覆盖。
  代价是审计现在会**报告**重复（v2 仍会打印 `duplicates: 1`），需要人工判断是否要改文案。
- 新增测试：`tests/test_golden_experiment_keying.py`（同题不同标签各自计分、命中对方法规时判定为未命中、
  统计里两道题都计入）。
- 验证命令及结果：`python -m scripts.audit_golden_set` 现在对 v2 报 `unique questions: 74 (duplicates: 1)`，
  v1/v3 为 0；`check_eval_regression` → exit 0（修复前同一次通过会 exit 1）；
  全量 `pytest` → 267 passed, 1 skipped；`ruff check app tests alembic scripts` → All checks passed。
- 仍存在的限制：v2 的重复题**没有**改写也没有重生成产物，只在审计里可见；
  `scripts/check_eval_regression.py` 仍只拦回归、放过改善（改善会打印提示）。

## 已完成事项复盘（2026-10-05：真实并发、可逆迁移、类型与覆盖率门禁）

### 事项三十一：跨进程幂等只在顺序下验证过，以及 4/10 个迁移的 downgrade 不是"逆"

- 根因一（并发）：`store.claim_document` 用单条条件 UPDATE 实现"同一份文档只会被一个 worker 认领"，
  但此前只用顺序调用验证过；没有真线程、真多连接、也没有在同一时刻同时进入的场景。
- 根因二（迁移）：10 个 Alembic 修订版本里 4 个的 `downgrade()` 只做了一半——
  `0001_initial` 漏删自己创建的 `feedback`（含索引）与 `evaluations`；
  `0003_evaluation_datasets` 漏删它加到 `evaluations` 的 7 个列与 3 个索引；
  `0004_document_versions` 漏删 `documents.version`/`chunks.version`；
  `0005_feedback_tenant` 漏删 `feedback.tenant_id`。
  后果不是"降级不干净"这么轻：库里会留下任何修订版本都没描述过的形状，再升级时 inspector
  把残留当作"本来就存在"，于是**升级路径本身**也被掩盖。
- 设计选择：`tests/test_concurrency.py` 用 8 个线程 + `threading.Barrier` 同时放行（顺序到达会把并发
  测试悄悄退化成顺序测试），断言"恰好一个认领成功"、stale 抢占、`replace_chunks` 的读者**永远只看到
  旧集合或新集合**、并行写者不丢行；同一批用例参数化为 `[sqlite]/[postgres]`，
  `EVALRAG_TEST_DATABASE_URL` 未设置时 PG 分支 `skip`。`store.py` 的 sqlite 连接钩子补
  `PRAGMA busy_timeout=5000`（否则多线程写立刻 `database is locked`）与文件库 `journal_mode=WAL`。
  `tests/test_migrations.py` 对每个修订版本做一次真实往返：snapshot → upgrade → downgrade → 比
  columns/indexes/unique constraints/foreign keys，并把差异打印成人话。
- 替代方案与取舍：本地没有 PostgreSQL 也没有 docker daemon，所以 PG 只能进 CI（`postgres:16`
  服务容器）；没有把并发测试做成"随机 sleep 碰运气"，因为那既不稳定也不证明竞争存在。
  迁移测试每个修订版本都做一次真实 DDL 往返（约 20 次 upgrade/downgrade），比读文件做静态检查慢，
  但只有真跑 `downgrade()` 才能发现上面四类缺陷。
- 变异性验证（证明测试真的有牙）：在 `store.replace_chunks` 的 DELETE 之后临时插一句 `session.commit()`，
  并发测试立刻失败并打印 `partial index observed: [0, 5, 7]`；把 `0005` 的 `drop_column` 去掉，
  迁移测试立刻失败并指出 `feedback.columns left=['tenant_id'] lost=[] changed=[]`（两处改动均已还原）。
- 新增文件/改动：`tests/test_concurrency.py`、`tests/test_migrations.py`、`app/core/store.py`（PRAGMA）、
  `alembic/versions/0001_initial.py`、`0003_evaluation_datasets.py`、`0004_document_versions.py`、
  `0005_feedback_tenant.py`、`.github/workflows/ci.yml`（新增 `postgres` job）、`.gitignore`（`-wal`/`-shm`）。
- 验证命令及结果：本地 SQLite 下 6 passed / 6 skipped（PG 参数），迁移测试 1 passed / 1 skipped；
  全套 **302 passed, 8 skipped**；`ruff check app tests alembic scripts` → All checks passed。
- 仍存在的限制：PG 分支与 CI 的 `postgres` job 只能由 CI 真正执行（本地无 PG）；
  `busy_timeout`/WAL 只对文件型 SQLite 生效；并发测试验证的是"恰好一个成功"，
  不覆盖 worker 崩溃后重新入队的最坏时序（stale 抢占用回拨 `updated_at` 模拟）。

### 事项三十二：类型检查、覆盖率门禁，以及一个没人读的 `uv.lock`

- 根因：仓库既没有类型检查也没有覆盖率（`uv.lock` 637KB，但 CI 走 `pip install -e`，没有任何东西读它，
  于是它可以静默漂移）。
- 设计选择：`pyproject.toml` 里落 `[tool.mypy]`（`files=["app"]`、`check_untyped_defs`、
  `warn_unused_ignores`、`warn_redundant_casts`、`no_implicit_optional`、`strict_equality`），
  对没有 `py.typed` 的 `celery`/`sentence_transformers`/`pymilvus`/`openpyxl` 做 per-module override；
  `[tool.coverage]` 只统计 `app`、`fail_under=80`；dev extras 增补 `mypy`、`pytest-cov`；
  CI 的 backend job 增加 `mypy`、`pytest --cov=app --cov-fail-under=80`，并加
  `astral-sh/setup-uv` + `uv lock --check` 防止 lock 与 `pyproject.toml` 漂移。
- 顺手修掉的 28 个真实类型问题（都不是为过检查而改）：`app/core/ingestion.py` 里同一个名字
  `document` 先后承载 `pymupdf.Document` 与 `docx.Document`，第二个分支因此在类型上"继承"了第一个
  （`document.paragraphs` 不存在）——改名并拆开；pymupdf 的 `Document` 不走 `__iter__`（迭代靠
  legacy `__getitem__`），`for page in document` 无法类型化——改为显式 `page_count` + 索引访问；
  `app/core/cache.py`/`app/core/rate_limit.py` 把 `RedisError` 重新绑定成 `OSError`（给一个类型名赋类型）
  ——改为 `try/except/else` 组装 `REDIS_FAILURES`（同时去掉重复的 `OSError`）；
  `app/core/observability.py` 的 `run_type` 收窄为 LangSmith 的 Literal（打错一个 run type 现在会报错）；
  `app/core/store.py` 的 `evidence_mode` 在 DB 边界用 `cast` 标注（列是 str，schema 是 Literal）；
  `app/core/evaluation_runner.py` 的 `document_version` 形参原本声明 `str`，而它按语义就是 `str | None`
  （缺省不过滤）；`answer_evaluation` 分支用 `assert` 收窄 `self.llm`；
  `app/core/langsmith_eval.py` 的 `item.inputs` 可能是 `None`；`app/core/rag.py` 的
  `stream_with_evidence` 改成 `AsyncGenerator[str, None]`——调用方确实在 `await ...aclose()`。
- 替代方案与取舍：**没有**打开 `disallow_untyped_defs`（还剩 29 个未注解定义），因为"为了满足 linter
  补 29 个注解"不是这次的目标，写进限制里比假装完成更诚实。覆盖率下限取 80 而不是贴着实测值：
  本地用 stdlib `trace --count --missing` 量到 `app/` 行覆盖 **86.7%**（4378/5048，44 个模块全部被 import），
  而 `coverage`/`pytest-cov` 在本地三个解释器里都装不上（离线）——所以门禁留了约 7pp 余量。
- 验证命令及结果：本地 `mypy`（1.14.1）与 uv 临时环境里的**锁定版本 1.20.2** 都是
  `Success: no issues found in 44 source files`；`uv lock --check` → exit 0（重新生成的 lock 增加了
  `mypy`/`pytest-cov`/`openpyxl`/`mypy-extensions`/`pathspec`，并把 `pytest` 8.4.2 → 9.1.1）；
  全套 **302 passed, 8 skipped**；`ruff` → All checks passed。
- 仍存在的限制：CI 上的覆盖率百分比本地无法复现（没有 pytest-cov），只能保证下限有余量；
  `mypy` 只覆盖 `app/`，`scripts/`、`tests/`、`alembic/` 仍未检查；`uv.lock` 现在只会被
  **校验**而不会用于安装（安装仍走 pip 的版本区间），真正的 `uv sync --frozen` 属于下一梯队。

### 事项三十三：一次评测既跑得慢、又怕卡死、失败还会把已完成的题全丢掉

- 根因：`EvaluationRunner._run_examples` 是 `for example in dataset.examples` 顺序循环——75 题、
  每题一次检索 + 两次 LLM 调用，串行就是分钟到小时级；没有超时，一个卡死的后端调用会拖住整轮；
  失败路径 `update_evaluation(id, "failed", {"metrics": {}, "examples": []}, str(exc))` 把已经算完、
  已经付过钱的逐题行整体覆盖掉，而 `app/tasks.py` 的 Celery 装饰器正是
  `autoretry_for=(Exception,), max_retries=2` —— 重试用同一个 evaluation_id，于是每一次重试都从零开始重新付费。
- 设计选择：① 有界并发 `EVALUATION_CONCURRENCY`（默认 4，`asyncio.Semaphore`，per-example；
  付费重排另有 `TYPESAFE_CONCURRENCY`）；② 每题超时 `EVALUATION_EXAMPLE_TIMEOUT_SECONDS`
  （默认 120s，`asyncio.wait_for`），超时的题写成 `{"metrics": {}, "retrieved": [], "timed_out": True,
  "error": "timed out after ..."}`，因此自动不进任何均值，同时新增
  `completed_example_count`/`timed_out_example_count` 说明几个数参与了平均；③ 逐题 checkpoint：
  每完成一题（写锁内）就把 `ordered()` 写进 `results.examples` + `results.progress`，失败路径改成
  **保留**上一次的 results，只更新 status 与 error_message；`run()` 入口按 fingerprint 取 checkpoint，
  跳过已完成的题，`progress` 报 `completed/total/resumed`。
- 三个不显眼但必要的细节：结果**按数据集顺序**折叠（不是完成顺序），否则 `scripts/bootstrap_ci.py`
  的逐题配对会在两次运行之间错位；fingerprint 覆盖题集、检索设置与 rag/prompt 版本，
  换了设置宁可整轮重跑也不混行；`asyncio.gather` 失败时逐个 `task.cancel()` 再等它们结束，
  否则一个迟到的 checkpoint 写入会把刚写下的 `failed` 覆盖回 `running`。
- 替代方案与取舍：没有引入 `anyio`/`asyncio.TaskGroup`（项目已有 FastAPI + asyncio，`Semaphore` +
  `gather` 足够，也避免为了取消语义再加依赖）；没有把 checkpoint 逐题写进数据库的独立表
  （复用 `evaluations.results_json`，代价是每次写入都是整份 JSON，但 75 题的量级无所谓，
  换来的是 `GET /evaluations/{id}` 天然能看到进度）；超时的题**不计 0 分**而是完全排除——把"没测到"
  当成"测得很差"会静默压低所有指标。
- 验证命令及结果：新增 `tests/test_evaluation_runner_concurrency.py` 5 项（并发上限=3 且行按数据集
  顺序、超时题被排除且计数正确、失败后重跑只补跑缺的题并 `resumed=3`、换 `rag_version` 后
  checkpoint 被丢弃 `resumed=0`、checkpoint 写入失败不影响整轮）→ 5 passed；**5 个变异体全部被抓**：
  去掉 Semaphore（并发数变成 6）、去掉 timeout（慢题 5s 后正常完成、计数为 0）、失败路径改回
  空 results（重跑重新问了全部 4 题）、checkpoint 忽略 fingerprint（`resumed=2`）、
  把 checkpoint 的 `except Exception` 换成 `except ZeroDivisionError`（整轮 failed）——每个变异只让
  对应的那一个用例失败，其余不动。全套 **307 passed, 8 skipped**；`ruff` → All checks passed；
  `mypy` → Success（44 files）。
- 仍存在的限制：并发是进程内的，多 Celery worker 同时跑同一个 evaluation_id 仍会各跑一遍
  （claim 是 per-document 的，评测行没有租约）；超时按"每题"计，不含排队等待时间（有界并发下排队
  可能比 120s 长，但那时也意味着 4 个好题在跑，不是卡死）；checkpoint 写的是整份 JSON，
  题量上到几千题时应改成增量表。

### 事项三十四：答案质量评测从来没开过，而裁判是同一个模型——那就自己审自己

- 根因：`scripts/run_golden_experiment.py:387` 把 `answer_evaluation` 硬编码为 `False`，
  于是"检索到了没有"有 7 个指标、`docs/evaluation/*.json` 有 11 个产物，而"答得对不对、有没有
  依据"一行数据都没有。`app/core/answer_evaluation.py` 的裁判与答题用的是同一个 provider、
  同一个模型（`create_llm(settings)`），三个维度（correctness/faithfulness/completeness）
  写进平均数就再没人看过——管线看不出"裁判只是在附和"。
- 顺带修掉一个真缺陷：LLM 适配器对瞬时网络错误**零重试**。两次真跑 `--answer-evaluation` 时
  `app/core/rag.py:42 answer_with_evidence` → `app/core/llm.py:64` 抛
  `httpx.ConnectError: All connection attempts failed`，整轮 evaluation 直接 failed。
  隔离探针（单次/并发 4 路/连续 30 次各自新建 client）都成功，说明是不可复现的瞬时抖动，
  但"一次瞬断废掉一轮付费评测"是确凿观察。修法：`LLM_MAX_ATTEMPTS = 3`、
  `LLM_BACKOFF_SECONDS = 0.5`、`RETRYABLE_STATUS_CODES = {408,409,425,429,500,502,503,504}`、
  `LLMRequestError`，`answer()` 循环重试并抽出不带重试逻辑的 `_post_chat`，
  退避优先用 `Retry-After`（解析失败则指数退避，上限 30s）。
- 设计选择：① 给评测再加一个开关 `--answer-evaluation`（默认关，保住"没配 key 也能跑检索实验"）；
  ② 产物沿用 `configs[].per_example[]` 形状，逐行写 `judge{correctness,faithfulness,completeness,
  reason}` 与**裁判看过的 ≤3 块证据**（每块 ≤400 字）——忠实度不看到证据就没法人工复核；
  ③ `scripts/judge_agreement.py` 把人工标签与 judge 逐行配对（键是 `(config, question)`，
  同一键出现两行直接报错，因为 golden 集历史上真出现过重复题），算 Cohen's kappa
  + 2000 次 percentile bootstrap（固定 seed `20261005`），并把 judge 值缺失/人工值缺失分别计数，
  绝不当 0 分；双方把所有行都判成同一类时明确写 "undefined"，而不是编一个数出来；
  ④ `scripts/judge_calibration.py` 做扰动校准——把答案正确的行追加一句语料里根本不存在的
  "规则"，再把同一批证据交给同一个裁判：只会算平均分的管线看不出裁判是不是在附和，
  植入一个已知的谎言就能看出来。
- 数据与结果（真实 `deepseek-chat`，语料用**已入库**的 `tests/fixtures/eval_gate/corpus` 8 篇 +
  `golden.json` 16 题（含 1 道应拒答题），2 个检索配置 × 16 题 = 32 行 ≥ 30）：
  - `docs/evaluation/answer-quality-eval-gate-2026-10-05.json`：两配置检索指标相同
    （R@1=0.600、R@3=0.867、MRR=0.700、nDCG@3=0.742、page_hit=0.867、quote_hit=0.867），
    p50 约 3.4–3.7s/题（含生成 + 裁判两次调用）。
  - 人工复核（`judge-agreement-2026-10-05.json`）：correctness kappa **1.000**
    [1.000, 1.000]、completeness **1.000**、faithfulness **0.000**（agreement 0.875）。
    32 行里裁判的三个维度只有两种取值——28 行 (1,1,1)、4 行 (0,0,0)，**从未分离**。
  - 那 4 个 0 分是"证据里确实没有该条款，模型于是拒答"：500 元首次出资额那题召回的 3 块证据
    来自 05/01/02 三份文件，而规则在 `07_成员资格与出资规定.md:5`（检索没召回）；
    "借款期限最长"那题检索返回 **0 块证据**。人工把这两个拒答记为 **faithful = 1.0**
    （证据不足时拒答是唯一忠实的回答），于是 faithfulness 上 kappa 掉到 0：
    **裁判把"没答对"当成了"不忠实"。**
  - 扰动校准（`judge-calibration-2026-10-05.json`，三次独立运行）：`fabrications_flagged`
    = **0/16、0/16、0/16**；faithfulness 仍给满分的 11/16，给 0.5 的 3/16，剩下 2 行 0 分
    是拒答题被判错（与植入无关）。裁判的 reason 里明明写着"额外补充了证据中不存在的第三十三条
    内容"，却仍然给 0.5/1.0，理由是"虽未在证据中出现，但**不影响对核心问题的忠实度，且未与证据
    冲突**"——它把"无依据"降级成了"只要不矛盾就不算不忠实"。
- 替代方案与取舍：没有改裁判的 prompt（改了就无法与本次产物对照，先量出现状再动手）；没有引入
  RAGAS/DeepEval 之类的评测框架（要的是一次可复核的裁判审计，不是又一个依赖）；没有让第二个模型
  当第二裁判（多裁判投票/更强模型留给下一梯队）；标签者不是领域专家、且标注时看到了裁判的分数，
  所以 correctness/completeness 的 1.000 有一部分是循环论证——报告里全部写明，宁可报一个
  "上界"也不假装是无偏的标注者间一致性。
- 验证命令及结果：
  `python -m scripts.run_golden_experiment --corpus tests/fixtures/eval_gate/corpus --golden
  tests/fixtures/eval_gate/golden.json --configs sparse-bm25,sparse-bm25-rerank
  --answer-evaluation --json docs/evaluation/answer-quality-eval-gate-2026-10-05.json` → exit 0；
  `python -m scripts.judge_agreement --artifact ... --labels ... --json ...` → 三行指标 + 混淆矩阵；
  `python -m scripts.judge_calibration --artifact ... --json ...` → `fabrications_flagged: 0`。
  新增 `tests/test_judge_calibration.py`（10 项：植入句被追加且原答案不变、应拒答/无证据/无标准答案
  的行被排除、同一 seed 抽样与轮转可复现、送给裁判的 payload 里 `system_answer` 含植入句且证据
  page 是 int、汇总计数区分"部分扣分"与"真被判不忠实"、产物能被 `judge_agreement.load_rows` 读回、
  同一问题在两个源配置下仍是两行、空样本 main 返回 1）与 `tests/test_judge_agreement.py`（11 项，
  含手算表、双方同类 → kappa undefined、重复键报错、阈值改变结论）；`tests/test_llm.py` 新增 4 项
  （前两次 `httpx.ConnectError` 后成功、三次都失败 → `LLMRequestError` 且 `__cause__` 保留、
  429 的 `Retry-After: 2` 被采纳、400 不重试）。
- 仍存在的限制：16 题、1 个语料（8 篇小文件）、1 个模型、校准只做了一次采样（16 行，虽然跑了 3 遍）；
  更难的未入库语料 `law/`（18 份文档 + 75 题 v2）本地可跑但没有产物提交，因为它对别人不可复现；
  kappa 的 bootstrap 区间在最优点退化（`[1.000, 1.000]`）；judge 的 `faithfulness` 在阈值上
  很脆（0.5 既不算高也不算低），所以校准报告把 `fabrications_flagged`（correctness 保留
  且 faithfulness < 0.5）单独列出来，而不用"多少行低于 0.5"这种会被无关扣分污染的指标。

## 已完成事项复盘（2026-10-05：前端拆分、SSE 分帧与密钥存储）

### 事项三十五：880 行的单文件前端、不认 CRLF 的流解析器，和一个长期留在 localStorage 的 API Key

- 根因：`frontend/src/App.vue` 拆前 880 行（`<script setup>` 434 行 + 模板 444 行），一个文件里装了
  登录页、两个视图和 8 个面板；真正有逻辑的两处——`app/api.ts` 里的 SSE 分帧解析和指标
  标签/格式化——内联在请求循环与模板表达式里，因此浏览器端**一项测试都没有**：Python 侧 332 项
  测试不可能发现前端回归。另外三处是读代码就能确证的真缺陷：
  ① `api.ts:requestHeaders()` 用 `localStorage.getItem('evalrag_api_key')`，`App.vue:login()`
  也把 key 写进 localStorage——API Key 长期留在磁盘上，任何 XSS 或共用这台机器的人都能读回；
  ② `streamChat()` 的旧解析器完全忽略 `event: error` 帧（后端在引用不可核验、生成失败时用它收尾），
  于是"被拒答"在界面上表现为一片空白，用户看不到任何原因；
  ③ 旧解析器用 `buffer.split('\n\n')` 只认 LF 终止符，而 SSE 规范允许 CRLF——CRLF 帧一个都解析不出来，
  多行 `data:` 也没有按规范用换行拼接。
- 设计选择：① 拆分**只搬模板**，状态与函数全部留在 App.vue（零数据流重构，行为逐字不变），
  子组件用 `defineModel` + props/emits 接模板，共 8 个组件；② 把三块有真实逻辑的代码抽成可测模块：
  `src/sse.ts`（`parseSseBlock` / `SseDecoder`，帧边界按"更早出现的 `\n\n` 或 `\r\n\r\n`"取，
  半个帧留在 `pending`）、`src/auth.ts`（`loadApiKey/getApiKey/setApiKey/clearApiKey` + tenant 读写）、
  `src/metrics.ts`（指标标签 + 整数/毫秒/三位小数格式）；③ 密钥默认**只在内存**（本次会话有效），
  勾选「在本标签页内记住密钥」才写 sessionStorage，关掉标签页即失效；**legacy localStorage key
  一律删除而不是迁移**——迁移等于继续保留长期凭据，等于这次修改白做；④ `event: error` 接到
  `handlers.onError`，由 App 既有的 `setError` 显示出来；⑤ vitest 独立 `vitest.config.ts`
  （vite 自己的 `defineConfig` 不接受 `test` 字段，硬塞会让 `npm run build` 类型报错），
  `pool: 'threads'` 而不是默认的 fork 池。
- 替代方案与取舍：没有引入 Pinia——组件只是模板切分，没有跨组件共享状态，为拆文件引入状态库是
  本末倒置；没有把 API Key 换成 httpOnly cookie / 服务端会话（要动后端鉴权链路，属于下一梯队，
  现状写在下面的"仍存在的限制"与 README 里）；没有顺手把轮询循环抽成 composable
  （重构面扩大、收益不明确）；"记住密钥"选 sessionStorage 而不是持久化，是因为这个前端没有真正的
  会话概念，持久化等于把原来的问题换个键名继续存在。
- 新增测试（`frontend/src/`，32 项）：`auth.test.ts`（9 项，含"legacy key 被删除且**不被采用**"、
  "remember=false 时 localStorage/sessionStorage 都不写"、"storage 抛错（隐私模式）时不崩"）、
  `sse.test.ts`（10 项，含跨 chunk 的半个帧、CRLF 终止符跨 chunk、`: ping` 心跳不产生事件、
  多行 data 拼接、`[DONE]` 原样透传）、`metrics.test.ts`（4 项，含非数值指标被过滤、三种格式）、
  `components/ChatPanel.test.ts`（6 项，含无知识库时按钮禁用、三类反馈只发一次、引用页码/版本/分数渲染）、
  `components/DocumentPanel.test.ts`（3 项，含选中文件后 input 被清空以便重选同一文件）。
- 验证命令及结果：`cd frontend && npm test` → **5 files / 32 passed**（vitest 5.0.3，1.08s）；
  `npm run build` 的类型检查部分 `vue-tsc --noEmit` → exit 0。CI 的 `frontend` job 增加 `npm test`
  一步（原来只有 `npm ci` + `npm run build`），并在 job 注释里写明为什么。
  沙箱注记：本机 vitest/vite 需要 esbuild 的服务子进程，默认文件沙箱禁止管道子进程
  （`spawn EPERM`，与之前 `vite build` 无法完成的限制同源），所以本地这次验证是在放宽权限后跑通的；
  CI 的 ubuntu-latest 没有这个限制。
- 仍存在的限制：API Key 仍由前端持有、后端只按 `X-API-Key` 比对，真正的修法是服务端会话或短期令牌；
  `ask()` 的 `onError` 路径没有集成测试（只有类型检查 + 模板 props 传递，App 级 mount 测试留给下一梯队）；
  `vite build` 在本沙箱仍无法完成，产物构建只在 CI 上验证；前端目前只有 `vue-tsc` 一道静态检查，
  没有 ESLint/Prettier；组件测试只覆盖 ChatPanel 与 DocumentPanel 两个面板。

## 已完成事项复盘（2026-10-07：outbox 一致性、库级约束、可观测性、对象存储与门禁）

本节覆盖代码评审第 2 梯队 #15–#20 六项。第 2 梯队原本标注为"可选，增强系统设计叙事"，实际落地的六项
全部属于"出问题时才会被发现"的那一类：outbox 与迁移锁只在崩溃/并发下生效，库级约束只在有人写脏数据时生效，
可观测性只在排障时生效，对象存储只在多副本部署时生效，门禁只在发布时生效。因此这一节的证据以
"测试怎么写得出这个场景"为主，而不是以性能数字为主。
本节的数字口径：全量 `pytest` → **451 passed, 9 skipped**；其余分文件数字见各条"验证命令及结果"。

### 事项三十六：外部索引 outbox + 可重放（#15）

- 根因：摄取链路是"写 PostgreSQL（chunks + `documents.status`）"与"写外部检索后端（Milvus/Elasticsearch）"
  两次独立写，中间没有事务能把它们绑在一起。worker 在外部写成功之后、`documents.status` 更新为 `ready`
  之前崩溃，或者外部写在重试若干次后彻底失败，都会留下一个 `processing` 且永远不会再被调度的文档；
  更早的一版用 Celery 的 `autoretry` 扛这件事，但重试状态活在 broker 的内存/结果后端里，
  进程重启或 broker 迁移之后就没有任何人知道"这个文档还欠一次外部写"。
- 设计选择：把"还欠一次外部写"变成数据库里的一行 `index_outbox`
  （`app/db/models.py:192` 的 `IndexJobRecord`，`__tablename__ = "index_outbox"`），
  摄取事务的提交点同时落 chunk 与 intent——`app/core/store.py:358-373` 的
  `save_document_index(self, document_id, knowledge_base_id, chunks)` 在**同一事务**里调
  `_write_chunks` 与 `_upsert_index_job(..., "upsert")`；外部写搬到
  `app/tasks.py:227-283` 的 `_sync_document_index`，`app/tasks.py:290-318` 的 `_replay_index_outbox`
  负责驱动所有到期行。重试预算写在**行上**而不是 broker 上：
  `INDEX_JOB_MAX_ATTEMPTS = 3`（`app/core/store.py:456-459`，注释 "Small on purpose"），
  `fail_index_job(job_id, error)`（`app/core/store.py:551-572`）每次累加 `attempts`，
  到达上限把 `status` 置为 `failed`；Celery 任务注册处（`app/tasks.py:366-369`）特意写明
  "No autoretry: the row itself carries the retry budget"。领取用
  `claim_index_job(*, document_id=None, stale_after_seconds=900, due_after_seconds=0, now=None)`
  （`app/core/store.py:472-535`）：pending 行可领，`processing` 但超过 `stale_after_seconds` 的行
  表示"上一个 worker 死了"也可领；PostgreSQL 上走 `statement.with_for_update(skip_locked=True)`，
  `UPDATE` 的 `rowcount != 1` 就 rollback 并返回 `None`（没领到就什么都不做）。
  `index_outbox` **刻意不加外键**（`app/db/models.py:192` 的 docstring），因为行必须活过它描述的
  文档/知识库：`delete_document`（`app/core/store.py:390-406`）删行之后补一行 `operation="delete"` 的
  intent（:402-404 的注释就是这条理由），否则级联删除会把"外部索引还需要清理"这唯一证据一起抹掉；
  回放时从 PostgreSQL 读源文本（`get_document_chunks`，`app/core/store.py:431-452`），
  不在 outbox 行里冗余存 payload。错误信息截断到列能容纳的长度：
  `record.last_error = (error or "")[:2000] or None`。
- 替代方案与取舍：另一条路是把 PostgreSQL 与外部后端放进一个两阶段提交/XA 事务，
  或者干脆改成"每次检索时从 PostgreSQL 现算，不维护外部索引副本"。前者被放弃是因为 Milvus/ES
  都不是 XA 参与者，2PC 只能做成"尽力而为"再补补偿逻辑，复杂度不降反升；后者等于放弃外部后端，
  与 #10/#11 之后的检索架构冲突。也考虑过把 intent 存成 Celery 的 task id 或一张内存表——
  放弃的理由就是本节根因里那条：dead letter 记录必须在进程之外、且必须在数据库事务里。
  代价是 `status` 与 `last_error` 成为新的持久状态：必须有 sweep 定时跑，否则行会静静躺在那里
  （这就是 `evalrag.replay_index_outbox` 那个周期性任务存在的理由），
  而且 done 行不删除、只留作"上次回放是什么时候"的记录，表会持续增长。
- 新增测试：`tests/test_index_outbox.py` 19 项，逐条钉一个场景：
  `test_chunks_and_the_index_intent_are_committed_together`、`test_uploading_a_document_does_not_claim_it_is_indexed`、
  `test_reindexing_supersedes_the_previous_intent_instead_of_queueing`、`test_only_one_worker_claims_a_row`、
  `test_a_dead_worker_does_not_hold_the_row_forever`、`test_a_fresh_row_is_not_due_for_another_sweep_yet`、
  `test_the_retry_budget_turns_a_hopeless_row_into_a_visible_failure`、
  `test_a_long_error_is_truncated_to_what_the_column_holds`、`test_deleting_a_document_leaves_a_delete_intent`、
  `test_deleting_a_knowledge_base_leaves_one_delete_intent_per_document`、`test_a_refused_delete_leaves_no_intent_behind`、
  `test_a_replay_publishes_the_chunks_postgres_has`、`test_a_replay_of_a_delete_intent_clears_the_index`、
  `test_a_replay_with_nothing_to_do_says_so`、`test_a_failed_replay_is_booked_and_the_document_stays_unfinished`、
  `test_the_sweeper_replays_what_the_first_attempt_could_not`、`test_the_sweeper_stops_when_nothing_is_due`、
  `test_the_default_sweep_leaves_a_just_committed_row_for_later`、
  `test_the_sweeper_moves_on_instead_of_hammering_one_broken_document`（最后一条对应
  `_replay_index_outbox` 里"一个坏文档不能中断整轮 sweep"的分支，返回计数为
  `{"replayed": ..., "failed": ...}`）。`tests/test_tasks.py` 里原本的
  `test_external_index_failure_keeps_document_failed_and_db_unchanged` 被改写为
  `test_external_index_failure_leaves_a_replayable_row_behind`（断言失败之后**留下可回放的行**，
  而不是旧的"数据库不变"），并新增 `test_a_parked_index_row_marks_the_document_failed`
  （预算耗尽后文档不再是"迟到"而是可见的失败）。迁移侧新增 `alembic/versions/0012_index_outbox.py`
  （`revision = "0012_index_outbox"`，`down_revision = "0011_postgres_constraints"`），
  upgrade/downgrade 都先查 `sa.inspect(op.get_bind()).get_table_names()` 再决定是否动表，
  于是"已经建过"和"回滚过再升级"两种重复执行都是 no-op。
- 验证命令及结果：`pytest tests/test_index_outbox.py tests/test_tasks.py -q` 通过（包含在上述专项
  `tests/test_storage.py tests/test_upload_streaming.py tests/test_tasks.py` → **52 passed** 的口径内）；
  全量 `pytest` → **451 passed, 9 skipped**。事后可复核"重复 upgrade 也成立"的是
  `tests/test_migrations.py::test_every_downgrade_restores_the_previous_schema`，
  它在每个修订版本上跑 upgrade→downgrade→比对快照（注释里写明 downgrade 后再 upgrade
  也顺带证明 upgrade 路径可重复执行）。**未在本地验证**：PostgreSQL 上的
  `with_for_update(skip_locked=True)` 行锁行为（本机没有 PostgreSQL，该分支在 CI 的
  `postgres:16` service job 里跑）、以及真实 Milvus/Elasticsearch 的写失败语义
  （既有契约测试用的是 fake client）。
- 仍存在的限制：outbox 解决的是"最终一致"，不是"立刻一致"——上传接口返回 201 之后到
  外部索引可搜之间存在一个窗口，窗口长度由 sweep 周期决定；`done` 行不清理，
  长期运行需要另加归档策略（P1 清单里"原始文件清理/归档策略"那条仍未勾）；
  回放是"从 PostgreSQL 全量重建该文档的索引"，没有做增量 diff，文档很大的时候回放成本等于重灌一次；
  外部后端的删除失败与更新失败共用同一套 `attempts` 预算，没有区分"可重试"与"不该重试"的错误。

### 事项三十七：状态 CheckConstraint + PostgreSQL JSONB + 外键级联（#16）

- 根因：`documents.status` / `evaluations.status` 在数据库里是任意字符串，只有 Python 侧的 Literal
  在约束取值——任何绕过 ORM 的写入（手写 SQL、脚本、以后新增的写入路径）都能塞进一个
  谁都不认识的状态，而这种行在查询侧的表现是"永远不被任何分支命中"，排查时看起来像丢数据；
  `parameters_json` / `results_json` / `expected_evidence_json` 在 PG 上是 `TEXT`，
  存的是 JSON 但没有校验、不能建 GIN 索引；外键只声明了引用关系、没有 `ON DELETE` 规则，
  于是删知识库要么被外键挡住、要么需要应用层手工按依赖顺序删表。
- 设计选择：把约束下沉到数据库，并让**模型成为唯一事实来源**。`app/db/models.py:51-53` 加
  `_status_check(column, allowed) -> str` 生成 `f"{column} IN ('a', 'b')"`，
  `DocumentRecord`（:66-90）带 `CheckConstraint(_status_check("status", DOCUMENT_STATUSES), name="ck_documents_status")`，
  `EvaluationRecord`（:145 起）同样。JSON 列用
  `JSON_DOCUMENT = JSON().with_variant(JSONB(), "postgresql")`（:45-48）：PG 分支上是 JSONB，
  其它方言保持泛型 `JSON`，应用层照样传 Python 对象，**一套代码路径服务两种方言**。
  级联策略按"数据的归属"分两类：内容随语料死——`documents.knowledge_base_id`、
  `chunks.document_id` / `chunks.knowledge_base_id`、`evaluation_datasets.knowledge_base_id`、
  `evaluation_examples.dataset_id` 全部 `ondelete="CASCADE"`；而评测历史必须活过被它测过的语料——
  `evaluations.knowledge_base_id` / `evaluations.dataset_id` 用 `ondelete="SET NULL"` 且 `nullable=True`
  （:157-161）。SQLite 侧两个开关缺一不可：`create_engine` 加
  `json_serializer=lambda value: json.dumps(value, ensure_ascii=False)`
  （`app/core/store.py:74-83`，否则中文在库里被转义成 `\uXXXX`、库不可读），
  以及每个连接执行 `PRAGMA foreign_keys=ON`（`app/core/store.py:89-104`，
  注释已写明 SQLite **默认关闭**外键，不打开的话级联与 CHECK 会静默失效）。
  迁移不复述这套逻辑，而是从模型读出来生成 DDL：新增 `app/db/postgres_schema.py` 提供
  `desired_check_constraints()` / `jsonb_columns()` / `desired_foreign_keys()` 与
  `upgrade_statements(inspector)` / `downgrade_statements(inspector)` 两个纯函数，
  `alembic/versions/0011_postgres_constraints.py` 只做两件事——在非 PostgreSQL 方言上返回空语句表，
  否则执行这两个函数产出的语句表。
- 替代方案与取舍：考虑过用 PostgreSQL 原生 `ENUM` 类型而不是 `CHECK`，放弃是因为加一个取值需要
  `ALTER TYPE ... ADD VALUE`（在旧版本 PG 上还不能在事务块里跑），而 CHECK 的变更只是 drop + add
  一条普通 DDL；也考虑过在应用层做一个"状态机校验"而不动数据库，放弃是因为它挡不住绕过 ORM 的写入，
  等于没有解决根因。`postgres_schema.py` 抽成独立模块而不是写在 revision 里，取舍点是
  "多一个文件"换"可测性"：schema inspector 是纯输入，于是"这个库缺什么"可以在没有 PG 服务的机器上
  逐条钉住（本机就是这种情况），revision 本身缩到两个循环。**SQLite 刻意不动**（`0011` 在非 PG 上是
  no-op），因为 SQLite 不能给已有表加 CHECK、也不能改外键；本地新建库由
  `Base.metadata.create_all()` 直接带上这三件事——代价是"本地 SQLite 老库"得不到这些约束，
  只能重建库。另一个必须显式处理的坑是**顺序**：模型描述的是最终 schema，而 `0011` 跑在
  `0012` 建出 `index_outbox` 之前，所以 `upgrade_statements` / `downgrade_statements` 都用
  `_existing_tables(inspector)` 过滤掉"数据库里还没有的表"，否则全新库的 `alembic upgrade head`
  会在 `0011` 就给一张不存在的表加约束（这正是那个必须成功的运行路径）；
  JSONB 转换用 `USING NULLIF({column}, '')::jsonb`，好让文本为空串的历史行不至于中断迁移。
- 新增测试：`tests/test_schema_constraints.py` 13 项，其中
  `test_api_literals_match_the_status_sets_the_check_constraints_allow` 把
  `get_args(DocumentStatus) == DOCUMENT_STATUSES`（`EvaluationStatus` 同理）钉住——
  这条是"模型是唯一事实来源"这个选择的守护测试，改了一边不改另一边就会红；
  `test_the_database_rejects_a_document_status_it_does_not_declare`、
  `test_the_database_rejects_an_evaluation_status_it_does_not_declare`、
  `test_a_status_typo_is_rejected_before_it_reaches_the_database`、
  `test_every_declared_document_status_is_accepted`（parametrize，防止约束写得太紧）；
  级联侧 `test_deleting_a_knowledge_base_takes_its_documents_chunks_and_datasets`
  （删前 documents/chunks 计数 1/2，删后全 0，评测数据集与样例也归零）、
  `test_evaluation_history_survives_the_corpus_it_measured`
  （`knowledge_base_id` / `dataset_id` 变 `None`，而 dataset_name 仍保留 "golden"）、
  `test_deleting_a_document_still_leaves_the_dataset_alone`、
  `test_deleting_another_tenants_knowledge_base_is_refused`、
  `test_deleting_a_missing_knowledge_base_reports_no_deletion`；JSON 侧
  `test_parameters_and_results_come_back_as_the_objects_that_went_in`、
  `test_json_is_stored_as_readable_text_not_escaped_ascii`（`assert "全对" in str(raw)`，
  直接对着上面那条 `json_serializer` 的取舍）、
  `test_expected_evidence_hops_survive_the_round_trip`。
  迁移侧 `tests/test_postgres_schema.py` 14 项，用 `FakeInspector` 分别模拟"0010 留下的形状"
  与"0011 跑完的形状"：`test_status_columns_are_constrained_to_the_documented_values`
  （四个约束名与条件逐字比对，含 `status IN ('pending', 'processing', 'ready', 'failed', 'needs_ocr')`
  与 `status IN ('queued', 'running', 'completed', 'failed')`）、
  `test_json_columns_are_the_three_documents_grow_into`、
  `test_deleting_a_knowledge_base_cascades_to_its_content_but_not_its_evaluations`
  （CASCADE 集合与 SET NULL 集合分别钉死）、
  `test_constraint_names_are_stable_so_a_downgrade_can_find_them`
  （`fk_{table}_{columns}` 与 legacy 名 `{table}_{columns}_fkey` 两个命名都钉住，
  因为 downgrade 要靠 legacy 名把 0001–0010 的原始约束名放回去）、
  `test_upgrade_adds_the_check_constraints`、`test_upgrade_converts_the_json_columns_to_jsonb`、
  `test_upgrade_replaces_the_foreign_keys_with_cascading_ones`、
  `test_upgrade_drops_every_old_constraint_exactly_once`、
  `test_upgrade_changes_nothing_when_the_database_already_matches`（幂等）、
  `test_upgrade_touches_only_the_tables_it_owns`、`test_downgrade_puts_the_previous_shape_back`
  （并断言 downgrade 出来的语句里 `not any("ON DELETE" in statement ...)`）、
  `test_downgrade_is_idempotent_before_the_upgrade_ran`、
  `test_statements_never_target_a_table_a_later_revision_creates`（就是上面那条顺序坑）、
  `test_a_table_that_exists_is_still_migrated`（证明存在性守卫不是"新表永久跳过"）。
- 验证命令及结果：`pytest tests/test_schema_constraints.py tests/test_postgres_schema.py -q` 通过；
  全量 `pytest` → **451 passed, 9 skipped**。**未在本地验证**：`0011` 的真实 DDL 只在 CI 的
  `postgres:16` service 上执行（本机没有 PostgreSQL，`postgres_schema.py:8-11` 的 docstring
  把这条写明为"这里只能钉住交给数据库的语句"）；SQLite 上 `PRAGMA foreign_keys=ON` 的
  级联效果由 `test_schema_constraints.py` 覆盖，但"老 SQLite 库升级后仍缺约束"这一条没有测试
  （它按设计就不做）。
- 仍存在的限制：`needs_ocr` 也算合法状态，所以"扫码件没有被处理"这件事在数据库层是合法的，
  约束只能挡住拼错的值、挡不住业务上不该出现的值；跨租户一致性只做到
  "删除时带 `tenant_id` 条件、`rowcount != 1` 就回滚且不留 intent"（`delete_knowledge_base`，
  `app/core/store.py:232-267`），**没有**数据库级的行级安全或租户列 CHECK，
  也就是说直接连库的写入仍能跨租户；JSONB 只做了类型转换，没有建 GIN 索引、也没有用
  `jsonb_path_ops` 之类的查询优化——目前没有任何按 JSON 内容过滤的查询；
  `0011` 在 SQLite 上是 no-op，因此本地开发库与 CI 的 PG 库在"约束强度"上并不等价。

### 事项三十八：Prometheus 指标 + readiness 深检查（#17）

- 根因：`/metrics` 当时只有 HTTP 计数这类最外层信号，Cache、检索各阶段、Celery、数据库连接池
  四层全是黑盒——缓存命中率被拖低时看不出来，检索变慢时看不出是 dense 还是 sparse 还是融合，
  worker 积压时看不出是任务失败还是根本没起来，连接池打满时只能等请求超时报错；
  `/health/ready` 只回答"进程活着"，按配置启用的外部后端（Milvus/ES）挂掉时它照样返回 200，
  于是编排器继续把流量打进来。
- 设计选择：指标端手写 Prometheus 文本格式而不引入 `prometheus_client`
  （`app/core/metrics.py:74` 的 `class _Histogram`、:127 的 `class Metrics`），
  理由是这一层只需要 counter/gauge/histogram 三种原语与一个 `render()`，多一个依赖不值得；
  采集点选在"一次操作的边界"而不是函数内部：缓存走 `observe_cache_hit/miss/error`
  （:153/:156/:159，由 `cache_backend_name(cache)`（:97）从实例上读出 backend 标签，
  于是同一个 `MemoryTTLCache` 与 `RedisCache` 自动分开）、检索走
  `time_stage(stage)` 上下文管理器（:164/:169，通道自己计自己的时间，hybrid 只记合并那一段）、
  Celery 走 `observe_task_started/succeeded/failed`（:184/:187/:190）、连接池走
  `read_pool` / `read_engine_pool`（:195/:217，读不到 accessor 就**不发布样本**而不是抛异常，
  保证 scrape 永远能渲染）、外部后端健康走 `set_backend_health`（:224）。
  渲染出的指标名逐字为：`evalrag_http_requests_total{path,status}`、
  `evalrag_http_errors_total{path}`、`evalrag_http_request_duration_seconds_{bucket,sum,count}`、
  `evalrag_cache_{hits,misses,errors}_total{backend}`、
  `evalrag_retrieval_stage_duration_seconds_{bucket,sum,count}{stage}`、
  `evalrag_celery_tasks_{started,succeeded,failed}_total{task}`、
  `evalrag_db_pool_{size,checked_out,idle,overflow,max_overflow}{pool}`、
  `evalrag_external_backend_up{backend}`。readiness 侧（`app/api/routes/health.py`）把
  "按配置启用"写成显式分支：`_check_redis`（:28）用
  `await asyncio.wait_for(client.ping(), timeout=_timeout(settings))`，
  `_check_database`（:53）走连接探测，`_inspect_queue`（:62）用同步 broker 探针经
  `asyncio.to_thread` 包起来（`celery_app.control.inspect(timeout=_timeout(settings))`，
  没人应答就返回 `{"status": _ERROR, "error": "no Celery worker answered the ping"}`，
  顺带取 `active()`/`reserved()`），`_check_backends`（:99）对每个启用的后端调
  `probe()` 并把探测失败本身当作健康信号（probe 返回 false → `"probe returned false"`）；
  未启用的后端返回 `"skipped"` 而不是假装 OK。`GET /metrics`（:254）是
  `PlainTextResponse` 且 `include_in_schema=False`。
- 替代方案与取舍：最省事的是 `pip install prometheus_client` 然后用它的
  `Counter`/`Gauge`/`Histogram` 与 `generate_latest()`，放弃的理由有两个：
  一是多一个运行时依赖，二是它的默认 multiprocess 模式在 Celery prefork worker 下
  本来就要额外配置（否则各进程各记一份、互相覆盖），而这里需要的是"每个进程一份、
  按需渲染"；自己维护的代价是直方图的桶边界与累积语义要自己写对
  （`test_stage_histogram_exposes_cumulative_buckets` 就是钉这个的）。
  readiness 的另一个取舍是"探测失败算不算不健康"：外部后端探测本身抛异常（SDK 没连上）
  在语义上既有"后端挂了"也有"探测代码写错了"两种解释，这里选择**当作不健康**并带上
  `checks[<backend>]["error"]` 原文，因为对编排器来说"这个依赖不可确认"就等于不能接流量；
  代价是一次配置错误会让整个 readiness 变 503，排查时得先看 `/health` 的逐项明细。
  延迟不设门禁、指标不设阈值告警（阈值是部署侧的事，仓库里只保证指标存在且格式正确）。
- 新增测试：`tests/test_observability_metrics.py` 25 项，前半段钉采集点、后半段钉 readiness。
  缓存：`test_memory_cache_counts_hits_and_misses`、`test_expired_entry_counts_as_a_miss_not_a_hit`
  （过期不等于命中，这正是缓存指标最容易写错的地方）、
  `test_redis_cache_separates_an_outage_from_a_miss`（redis 报错记 error 而不是 miss）、
  `test_redis_cache_counts_hits_and_misses`、`test_cache_backend_name_reads_the_instance`、
  `test_a_cache_without_metrics_does_not_count`（`metrics=None` 时零开销路径）；
  检索：`test_local_retriever_times_its_channel`（`..._count{stage="sparse"} 1` 与
  `stage="dense"`）、`test_hybrid_retriever_times_both_channels_and_the_fusion`（dense/sparse/fusion 各 1）、
  `test_stage_histogram_exposes_cumulative_buckets`、`test_retrieval_service_times_the_fused_stage`
  （fusion 与 rerank 都记）、`test_create_retriever_carries_metrics_into_the_channels`
  （metrics 一路透传到通道，防止"造了 retriever 但没接指标"）；
  Celery 与池：`test_celery_counters_are_rendered_per_task`、
  `test_task_body_reports_started_succeeded_and_failed`、
  `test_process_document_run_counts_through_the_worker_metrics`、
  `test_pool_gauges_come_from_the_sqlalchemy_pool`（断言
  `evalrag_db_pool_size{pool="default"} 5`、`max_overflow ... 3`、`checked_out ... 0`）、
  `test_pool_gauge_reading_never_raises_on_a_pool_without_accessors`、
  `test_metrics_endpoint_publishes_pool_and_http_series`、`test_backend_health_gauge_renders_zero_and_one`；
  readiness：`test_readiness_keeps_the_legacy_fields_and_adds_checks`
  （`status == "ready"`、`database == "ok"`，而 redis/queue/milvus/elasticsearch 都是 `"skipped"`，
  证明"未启用就跳过"）、`test_readiness_degrades_when_the_broker_has_no_worker`
  （`status == "degraded"`、错误文本含 "no Celery worker"）、
  `test_readiness_degrades_when_the_redis_ping_fails`（错误文本含 "connection refused"）、
  `test_readiness_reports_a_reachable_redis_as_ok`、
  `test_readiness_degrades_when_an_enabled_backend_probe_fails`
  （503，错误文本含 "milvus offline"，且 gauge 变 0）、`test_readiness_marks_a_healthy_backend_up`、
  `test_readiness_returns_503_when_the_database_is_unreachable`。
- 验证命令及结果：`pytest tests/test_observability_metrics.py -q` 通过；
  全量 `pytest` → **451 passed, 9 skipped**。**未在本地验证**：真实 Redis 与真实
  Celery worker 的探测（测试里用 fake client 与 monkeypatch 代替，
  `test_observability_metrics.py:102` 用 `pytest.importorskip("redis.asyncio")`
  在缺 redis 包时跳过）；真实 Milvus/ES 的 `probe()` 实现（既有契约测试用 fake client，
  没有连过真实集群，这条限制在 `docs/code-review-2026-10-04.md` §5 里已经主动交代过）。
- 仍存在的限制：指标是**进程内**的，没有做多 worker 聚合——Celery prefork 下每个子进程各有一份
  计数器，Prometheus 侧要靠 `sum by (...)` 自己合，仓库里没有为此加 multiprocess 模式；
  直方图桶边界是常量（`DURATION_BUCKETS`），没有按端点/阶段分桶；
  没有暴露"当前队列深度"这类瞬时 gauge（`_inspect_queue` 的 `active()`/`reserved()` 只出现在
  readiness 响应里，没进指标）；readiness 的每个探测都有超时，但"探测器自己卡住"只由
  `_timeout(settings)` 兜底，没有熔断/降级缓存，外部后端持续超时时每次 `/health/ready`
  都会付一次超时代价。

### 事项三十九：上传落对象存储 + 预签名 URL（#18）

- 根因：上传文件写的是 API 与 Worker 共享的本地卷（`uploads_data:/app/data/uploads`），
  于是"能不能水平扩副本"这件事被一个本地卷绑定死：API 副本 A 收的文件，Worker 副本 B 不一定看得到；
  卷本身也没有版本、生命周期和校验，备份与迁移都要单独处理；对外的下载接口还要由 API 进程
  把整个文件读进内存再吐出去。
- 设计选择：抽一个 `ObjectStore` 协议（`app/core/storage.py:80`，方法
  `put/get/delete/presign_get`）与两个实现，默认仍是本地（`LocalObjectStore`，:104），
  配 `OBJECT_STORE=s3` 才切 `S3ObjectStore`（:163）——`create_object_store(settings)`（:225-242）
  里那句 `if settings.object_store.strip().lower() == "s3"` 意味着写错一个字母会**退回本地**而不是崩，
  `test_local_is_the_default_backend_and_a_typo_does_not_change_that` 钉的就是这个。
  key 由 id 与文件名推导、不改 schema：`document_key(document_id, filename) -> str`（:55-61）
  返回 `f"{UPLOAD_PREFIX}/{document_id}/{_safe_name(filename)}"`，其中 `_safe_name`（:42）
  把非 `alnum/._-` 的字符换成 `_`、空名回落成 `document.txt`——于是客户端传来的
  `../../etc/passwd` 落成 key `documents/doc-1/.._.._etc_passwd`，逃不出前缀。
  本地实现用"写 `.part` 再 `temporary.replace(target)`"保证半截文件不会以目标名出现，
  并对越界 key 显式报错 `object key escapes the store root`（另一层防护）；
  `presign_get` 在本地实现上抛 `PresignUnsupported`（:38），接口层捕获之后**退回读字节再返回**
  （`app/api/routes/documents.py:195-232` 的 `download_document`），
  于是 S3 走 307 重定向、本地走流式响应，同一路由两种后端都自洽。
  S3 实现里两个细节：`__init__(client, bucket, presign_client=None)` 的第二个可选 client
  用于签名 Host 与内部 endpoint 不同的公开端点（否则预签名 URL 里的主机名是
  `http://minio:9000`，浏览器解析不了）；`get` 在 `finally` 里 `body.close()` 防连接泄漏。
  下载响应头不是拼 `filename=` 而是 `download_filename(filename)`（:245-247）
  = `f"attachment; filename*=UTF-8''{quote(filename)}"`，因为中文文件名直接拼进响应头会破坏它。
  部署侧把这件事变成默认路径：`docker-compose.yml` 加 `minio` 服务与一次性的 `createbuckets`
  服务（`mc mb --ignore-existing`），API 与 Worker 的 env 都注入
  `OBJECT_STORE: s3` / `S3_ENDPOINT_URL: http://minio:9000` /
  `S3_PUBLIC_ENDPOINT_URL: http://localhost:${MINIO_PORT:-9000}` / `S3_BUCKET` / `S3_ACCESS_KEY` /
  `S3_SECRET_KEY`，并删掉两处 `uploads_data` 挂载、把卷换成 `minio_data`。
- 替代方案与取舍：另一条路是"继续用共享卷，但把卷换成 NFS/EFS"，放弃是因为它只解决多副本可见性，
  不解决生命周期、预签名与备份，而且把云厂商的文件系统语义引进来；也考虑过
  "把文件直接存进 PostgreSQL 的大对象/bytea"，放弃是因为会把数据库备份体积和 WAL 一起推高，
  且与 #16 的库结构改造方向相反。`presign_get` 的取舍是安全边界：
  预签名 URL 让客户端绕过 API 直连对象存储，好处是 API 不再转发大文件、坏处是 URL 在有效期内
  是**持有即可访问**的凭证——所以有效期做成配置（`s3_presign_seconds`，默认 300），
  并且本地后端明确不支持（抛 `PresignUnsupported` 而不是返回一个假 URL）。
  删除路径上做了明确的取舍：`delete_document`（`app/api/routes/documents.py:168-192`）先删数据库行，
  然后 `_remove_upload` 抛 `ObjectStoreError` 时**只 warning 不失败**——注释里的理由是
  "行已经没了，这正是请求承诺的；残留对象只花存储不损正确性"，也就是说这里选了
  "宁可漏一个孤儿对象，也不要让一次成功的删除返回 500"。
  上传路径相反，是 fail-fast：`container.storage.put` 抛错直接 503
  "document could not be stored"，Celery 排队失败也 503 并顺手 `_remove_upload` 清掉已写入的对象。
- 新增测试：`tests/test_storage.py` 19 项（纯单元，不碰网络）：
  `test_keys_are_derived_from_the_document_id_and_the_filename`（含中文名 →
  `documents/doc-1/借款规定_2024.md`）、`test_a_client_filename_cannot_escape_the_document_prefix`、
  `test_an_empty_filename_still_produces_a_usable_key`、
  `test_local_store_round_trips_bytes_and_reports_the_size`（并断言不留 `*.part`）、
  `test_local_store_overwrites_an_object_wholesale`、`test_local_store_reports_a_missing_object`、
  `test_deleting_an_object_that_is_not_there_is_not_an_error`（delete 幂等）、
  `test_local_store_refuses_a_key_that_escapes_its_root`、`test_local_store_cannot_hand_out_urls`、
  `test_local_is_the_default_backend_and_a_typo_does_not_change_that`、
  `test_s3_backend_is_built_only_when_asked_for`、`test_a_public_endpoint_gets_its_own_signing_client`
  （`set(endpoints) == {"https://objects.example.com", "http://minio:9000"}`）、
  `test_s3_put_sends_the_body_and_reports_its_size`、`test_s3_put_needs_a_measurable_body`、
  `test_s3_get_reads_the_body`、`test_s3_get_maps_a_missing_key_to_object_missing`、
  `test_s3_get_lets_a_real_backend_failure_through`（只把"确实不存在"映射成 `ObjectMissing`，
  真故障原样抛出）、`test_s3_delete_and_presign_use_the_configured_bucket_and_window`、
  `test_download_filename_is_encoded_so_a_client_cannot_break_the_header`
  （`assert "%E5%80%9F" in value`）。接口层新增 6 项在 `tests/test_upload_streaming.py`：
  `test_api_stores_the_upload_under_a_derived_key_and_queues_processing`（取代之前的
  `test_api_streams_upload_to_disk_and_queues_processing`）、
  `test_a_filename_cannot_walk_out_of_the_store_root`、
  `test_api_reports_503_when_the_object_store_refuses_the_write`、
  `test_content_route_returns_the_stored_bytes`、
  `test_content_route_redirects_to_a_presigned_url`、`test_content_route_reports_a_missing_object`；
  同一文件里的 `test_api_rejects_oversized_upload_and_stores_nothing` 与
  `test_upload_is_spooled_in_bounded_chunks` 是改名/改写后的既有用例（原来叫
  `..._and_leaves_nothing_on_disk` / `..._is_written_in_bounded_chunks`），
  断言从"磁盘上没有文件"改成"对象存储里什么都没有"。
- 验证命令及结果：`pytest tests/test_storage.py tests/test_upload_streaming.py tests/test_tasks.py -q`
  → **52 passed**（这三个文件是本项的专项口径）；全量 `pytest` → **451 passed, 9 skipped**。
  **未在本地验证**：真实 MinIO/S3 的端到端上传（`S3ObjectStore` 的测试全部用 fake client
  钉调用参数，没有起过 MinIO）；`docker-compose.yml` 里 `minio` / `createbuckets` 两个服务的
  实际拉起（`docker compose up` 未在本机执行），因此 `createbuckets` 的 entrypoint 循环
  与 healthcheck 时序只在代码层面审阅过；CI 的 `docker-build` job 只做 `push: false` 构建，
  不跑 compose。
- 仍存在的限制：`LocalObjectStore` 的"先写 `.part` 再 replace"只在同一文件系统内原子，
  跨设备/网络文件系统上不成立；本地后端没有并发写同一 key 的协调（后写覆盖先写），
  也没有配额与清理（P1 清单里"原始文件清理/归档策略"仍未勾）；
  预签名 URL 一旦签发，在有效期内无法吊销（对象存储的通用限制，仓库里没有做额外的
  "一次性 token"包装）；孤儿对象只 warning 不失败，长期会积累，
  而 `index_outbox` 的 delete intent 清的是**索引**、不是对象存储里的对象——
  这两条清理线目前是分开的，没有统一的对账任务。

### 事项四十：Alembic 迁移加 advisory lock（#19）

- 根因：每个 API/Worker 副本启动时都会跑一次 `alembic upgrade head`。在 PostgreSQL 上，
  两个进程可以同时读到相同的 `alembic_version`、同时判断"要应用修订 N"，
  然后一个建表成功、另一个撞上"relation already exists"，或者更糟——一个进程在修订 N 中间，
  另一个开始跑修订 N+1。Alembic 自身**没有跨进程锁**，它只保证单进程内的顺序。
  这件事在 SQLite 上不会发生（单写者 + 文件锁），所以问题只在生产形态的 PostgreSQL 上出现，
  属于"本地怎么测都测不出来"的一类。
- 设计选择：用一个**独立连接**上的 PostgreSQL session 级 advisory lock，把它包在迁移运行外面。
  新文件 `app/db/migration_lock.py`：常量 `MIGRATION_LOCK_KEY = 5_710_214_013_041_212`（:33）、
  `LOCK_ENV_VAR = "EVALRAG_MIGRATION_LOCK"`（:36）、
  `_DISABLED_VALUES = frozenset({"0", "false", "no", "off"})`（:38），
  加 `lock_enabled(env=None) -> bool`（:43）与
  `@contextmanager migration_lock(engine: Engine) -> Iterator[bool]`（:49-65）。
  接进 Alembic 只改一行结构：`alembic/env.py` 的 `run_migrations_online()` 里把
  `with connectable.connect() as connection:` 改成
  `with migration_lock(connectable), connectable.connect() as connection:`，
  并 `from app.db.migration_lock import migration_lock`。三个设计细节值得写下来：
  锁用的是**与跑迁移的连接分开**的那条连接（注释里的理由：commit/rollback 不能让它提前释放，
  也不会漏进应用连接池）；锁是 session 级（`SELECT pg_advisory_lock(:key)` /
  `SELECT pg_advisory_unlock(:key)`），并且用 `finally` 解锁，所以迁移抛异常也会释放；
  非 PostgreSQL 方言或显式关闭时 yield `False` 并且**完全不建立连接**，
  于是 `alembic upgrade head` 在 SQLite 上的行为与之前逐字相同（本机开发路径零变化）。
  关闭开关做成了"只有明确写 0/false/no/off 才关"（未设置 = 开）——
  理由是安全默认：忘记配等于有保护，而不是等于裸奔。
- 替代方案与取舍：其他三条路都考虑过。一是"用文件锁"（`flock`），放弃是因为它只在单机有效，
  而这里要防的正是多副本；二是"用数据库行锁/一张锁表"，放弃是因为拿到锁的进程崩溃后
  要额外做超时清理（advisory lock 随连接断开自动释放，不需要清理逻辑）；
  三是"把迁移搬出应用进程、改成部署流水线里单独一步"，那是最干净的方案，
  但会改变现有部署形态（compose 里的 api/worker 都靠启动时迁移），
  而本项的目标是"不改部署形态就把并发迁移变安全"——这个方案记为后续演进，
  不是本轮范围。代价方面：advisory lock 是 PostgreSQL 特有的，
  换成 MySQL 要另写一套（`GET_LOCK`）；锁的粒度是"整个迁移流程"，
  所以慢迁移会阻塞所有副本启动，副本多的时候启动时间等于"最长迁移时间"；
  另外 `MIGRATION_LOCK_KEY` 是写死的常量，同一个 PG 实例上跑两个项目如果键撞了会互相等
  ——`test_lock_key_is_a_positive_63_bit_integer` 钉的是"它是一个合法的 bigint 键"，不是唯一性。
- 新增测试：`tests/test_migration_lock.py` 9 项。纯单元部分用一个记录调用的
  `_RecordingConnection`（:34）与 `_FakeEngine`（:54）钉行为：
  `test_lock_key_is_a_positive_63_bit_integer`（`0 < MIGRATION_LOCK_KEY < 2**63`）、
  `test_lock_is_enabled_unless_explicitly_disabled`、`test_lock_can_be_disabled_by_configuration`、
  `test_postgres_engine_locks_before_the_migration_and_unlocks_after`、
  `test_unlock_happens_even_when_the_migration_raises`、
  `test_non_postgres_dialects_do_not_connect_at_all`（断言临时库文件根本没被创建）、
  `test_disabled_lock_does_not_connect_to_postgres`（`engine.connects == 0`）、
  `test_env_module_wires_the_lock_around_the_migration_run`——最后这条**读 `alembic/env.py`
  的源码文本**，断言里面确实有 `from app.db.migration_lock import migration_lock` 与
  `migration_lock(connectable)`，防的是"helper 写好了但没接上"这种最难发现的漏接线。
  真并发一条：`test_a_second_migrator_waits_for_the_advisory_lock`，
  需要 `EVALRAG_TEST_DATABASE_URL`（没有就 skip），持锁时第二个
  `command.upgrade(config, "head")` 必须在 2 秒内 FutureTimeout，释放后 60 秒内完成。
- 验证命令及结果：`pytest tests/test_migration_lock.py -q` 本地为
  **8 passed, 1 skipped**（跳过的那条就是需要 `EVALRAG_TEST_DATABASE_URL` 的真并发用例；
  这一条也是全量 `pytest` 从 8 个 skip 变成 **9 个 skip** 的来源——它是本轮新增的、
  且属于环境限制而非代码跳过）；全量 `pytest` → **451 passed, 9 skipped**。
  **未在本地验证**：真实 PostgreSQL 上两个进程同时 `alembic upgrade head` 的互斥行为
  （本机没有 PostgreSQL，该用例在 CI 的 `postgres:16` service 上跑）；
  "锁在 SQLite 上确实不建立连接"由 fake engine 断言覆盖，但没有在真实 SQLite 迁移流程里
  再验证一遍（`alembic/env.py` 的实际调用路径由上面那条源码断言守着）。
- 仍存在的限制：advisory lock 只在 PostgreSQL 上有效，其它方言仍然是"相信单写者"；
  锁的粒度是整个 `upgrade head`，没有按修订版本加锁，所以 N 个副本的启动延迟由最慢的一次迁移决定；
  没有给"等锁"设超时——如果持锁进程卡住（例如迁移里有一个长事务），其它副本会无限等下去
  （`_LOCK_SQL` 是阻塞式的 `pg_advisory_lock`，不是 `pg_try_advisory_lock` + 重试）；
  锁键是硬编码常量，没有按数据库名派生的命名空间；
  另外 `#19` 原文里"`0003` 的 downgrade 补齐或明确声明 forward-only"这半句属第 1 梯队 #8 的成果
  （`alembic/versions/0003_evaluation_datasets.py` 的 downgrade 已补 3 个索引与 7 列，
  见 `docs/code-review-2026-10-04.md` §7 第 8 行），本轮未再改动该文件。

### 事项四十一：CI 与 release 门禁（#20）

- 根因：三个问题叠在一起。第一，`release.yml` 打 tag 就直接构建并推送镜像，
  没有任何"测试过了吗"的前提，因为 GitHub 的 `needs` **只能引用同一个工作流文件里的 job**，
  写 `needs: [backend, frontend, eval-gate]` 去引用 `ci.yml` 里的 job 不是"不生效"，
  而是整个 `release.yml` 加载失败。第二，CI 里 `pip install -e ".[dev]"` 意味着
  `uv.lock` 只是个装饰品——真正的依赖解析每次都现场联网重新做一遍，锁文件与
  `pyproject.toml` 不一致也没人知道。第三，CI 没有 `docker build`、也没有依赖漏洞扫描，
  于是"镜像能不能构建"和"依赖里有没有已知漏洞"这两件事都在本地靠自觉。
- 设计选择：把三个门禁的定义抽成**可复用工作流**，让 CI 和 release 共用同一份步骤。
  新增 `.github/workflows/gate-backend.yml`（`on: workflow_call`）、`gate-frontend.yml`、
  `gate-eval.yml`，`ci.yml` 里原本内联的 backend/frontend/eval-gate 三个 job 改成
  `uses: ./.github/workflows/gate-*.yml`，`release.yml` 也用同样的 `uses:` 把
  `backend` / `frontend` / `eval-gate` 三个 job **真实地建出来**，
  然后 `images` job 写 `needs: [backend, frontend, eval-gate]`——这样
  "needs 不能跨文件"的限制就被绕开了，而步骤定义只有一份、不存在两处漂移
  （两条理由都写在 `gate-backend.yml:1-4` 与 `release.yml:12-16` 的注释里）。
  三个门禁的内容分别是：backend（`uv sync --frozen --extra dev` → `uv lock --check` →
  `uv run --frozen ruff check app tests alembic scripts` → `uv run --frozen mypy` →
  `uv run --frozen pytest -q --cov=app --cov-report=term-missing --cov-fail-under=80`，
  `gate-backend.yml:38-39`）；frontend（`npm ci` → `npm run lint` → `npm run format:check` →
  `npm run build` → `npm test`，`gate-frontend.yml:22-28`）；
  eval-gate（用仓库里提交的小夹具语料 `tests/fixtures/eval_gate/corpus` 与 `golden.json`
  跑 `python -m scripts.run_golden_experiment`，再用
  `python -m scripts.check_eval_regression --baseline tests/fixtures/eval_gate/baseline.json --tolerance 1e-6`
  逐项比对质量指标，失败时 `actions/upload-artifact@v4` 上传 `/tmp/eval-gate.json`，
  `gate-eval.yml:24-43`）。`ci.yml` 另外补了三样：`postgres` job 改用
  `astral-sh/setup-uv@v5` + `uv sync --frozen --extra dev`，后续
  `uv run --frozen alembic upgrade head` 与
  `uv run --frozen pytest tests/test_migrations.py tests/test_concurrency.py -q`；
  新增 `docker-build` job（matrix 两个镜像，用 `type=gha,scope=${{ matrix.image }},mode=max`
  做缓存，注释写明"按镜像名分 scope 否则互相覆盖缓存层"，`push: false`）；
  新增 `audit` job（`uv export --frozen --format requirements-txt --no-emit-project`
  导出锁文件为 requirements，再用 `uvx pip-audit -r ... --no-deps` 扫描，
  注释说明用 `uvx` 临时拉起是为了不让 pip-audit 进生产依赖树，而 pip-audit
  发现漏洞默认非 0 退出，所以天然就是门禁）。
- 替代方案与取舍：让 release 复用 CI 的 job，另一条路是"把步骤复制到 release.yml 里"，
  放弃的理由就是 `gate-backend.yml` 注释里那句——复制出来的副本迟早和 CI 里的不一致；
  第三条路是"release 里不再跑测试，只信任 tag 前的 CI 绿灯"，放弃是因为那等于把
  "门禁通过"和"发布的内容"之间的对应关系交给人的记忆。
  依赖安装用 `uv sync --frozen` 而不是 `pip install -e .`，取舍立刻可见：
  锁文件过期时 CI 会**失败**，而不是悄悄联网解析出一个新版本——这是把 `uv.lock`
  从装饰品变成事实来源的代价（代价就是升级依赖必须显式改锁文件）。
  eval 门禁只比质量指标、**不比延迟**（`gate-eval.yml:4` 的注释：同机都会 ±10%，CI 更吵），
  容差 1e-6 只用来吸收跨平台的 libm 浮点差异，真实回归至少在 0.01 量级——
  也就是说这个门禁刻意做"宁可不报也不要误报"。覆盖率下限 80% 也不是新定的，
  它是对着本地实测 86.7% 留的余量（`gate-backend.yml:36-37`）。
- 新增测试：这一项没有传统意义上的单元测试,它的"测试"就是工作流本身在两个触发器下被加载和执行。
  可被静态核对的部分：`gate-*.yml` 三个文件都声明 `on: workflow_call`；
  `ci.yml` 与 `release.yml` 都用 `uses: ./.github/workflows/gate-*.yml` 引用同一份定义；
  `release.yml` 的 `images` job 带 `needs: [backend, frontend, eval-gate]`；
  `gate-eval.yml` 引用的两个脚本与两个 fixture 路径都在仓库里存在
  （`scripts/run_golden_experiment.py`、`scripts/check_eval_regression.py`、
  `tests/fixtures/eval_gate/corpus`、`tests/fixtures/eval_gate/golden.json`、
  `tests/fixtures/eval_gate/baseline.json`）。
- 验证命令及结果：**未在本地验证**。工作流要 GitHub Actions 才跑得起来，本机没有执行过
  `ci.yml` / `release.yml` / `gate-*.yml` 的任何一步；`git diff` 只能证明文件内容，
  证明不了"release.yml 能被 GitHub 解析"或"gate-eval 在 CI 上真的绿"。
  可复现的相邻证据只有两条：`scripts.check_eval_regression` 在 `docs/code-review-2026-10-04.md`
  §7 的快照里记录为 `exit 0，35 行指标全部 +0.0000`（那是第 1 梯队的本地运行，
  不是本轮 CI 运行）；全量 `pytest` → **451 passed, 9 skipped** 是本轮的本地数字，
  与 CI 上 backend 门禁的命令（`pytest -q --cov ... --cov-fail-under=80`）不完全相同
  （本地没跑 `--cov`）。因此这一条在 §8 的表格里**不写成"已验证"**。
- 仍存在的限制：`gate-eval.yml` 的夹具语料是 8 份合成文档 / 16 题，
  它只能证明"质量指标没有被改坏"，不能证明"检索在真实语料上变好了"；
  质量门禁容差 1e-6 意味着**任何**真实的指标下降都会被抓住，
  但也意味着基线需要随每次有意的指标变化手工更新（本轮没有引入自动更新基线的机制）；
  `docker-build` 只构建不推送、也不做镜像漏洞扫描（`audit` 扫的是 Python 依赖，
  不扫基础镜像的 OS 包）；`audit` 用 `--no-deps` 只扫直接依赖，传递依赖里的漏洞不会被报出来；
  frontend 门禁没有覆盖率下限，也没有 E2E（Playwright 之类）；
  release 的 `images` job 只在三个门禁都有 job 的前提下成立——
  一旦以后有人把某个 gate 重命名，`release.yml` 会因 `needs` 引用不存在的 job 而整体加载失败，
  而这类错误只能在 GitHub 上被发现（本地没有 workflow 语法校验这一步）。

## 已完成事项复盘（2026-10-07：API Key 换服务端会话与短期令牌）

### 事项四十二：长期 API Key 每次请求都从浏览器发出，而它不可撤销、不会过期（后续项 A）

- 根因：第 1 梯队把前端密钥的默认落点从 `localStorage` 改成了 `sessionStorage`（见事项三十五），
  但**凭据本身没变**——它仍是配置里的长期共享密钥。后果有三条：
  没有到期时间；要吊销只能轮换 `API_KEYS`（等于让所有客户端同时失效）；
  服务端也没有任何"谁在什么时候用过"的记录，因为 `app/api/deps.py::authenticate`
  只做一件事——拿 `X-API-Key` 去 `settings.api_keys` 里查租户。
  于是 XSS、共享终端、浏览器同步任一条路径泄露的都是永久凭据，
  而"最小权限/可撤销/可审计"这三件安全评审必问的事一件都答不上。
- 设计选择：把密钥换成一个**服务端可撤销的会话对象**。浏览器先用密钥换一个
  不透明随机令牌（`secrets.token_urlsafe(32)`，前缀 `ers_`，见 `app/core/sessions.py::new_session_token`），
  之后一律发 `Authorization: Bearer ers_...`。
  数据库只存 `sha256(token)`（`hash_token`），因为令牌是 256 bit 随机数、
  穷举不可行，不需要 Argon2 这类慢 KDF；同表另存 16 位 `key_fingerprint`
  用于回答"这是哪把密钥开的会话"，**长期密钥本身任何地方都不落库**。
  行（`api_sessions` 表）带 `expires_at` / `revoked_at` / `last_used_at`，
  所以过期、吊销、审计都只是对一行做条件 UPDATE。
  - 端点：`POST /api/v1/auth/session`（`X-API-Key` → `{token, expires_at, expires_in, authenticated}`；
    启用鉴权时必须持有有效密钥，body 里的 `tenant_id` 与密钥归属不符则 403）、
    `GET /api/v1/auth/session`（token 自述）、`DELETE /api/v1/auth/session`（吊销自己）、
    `GET /api/v1/auth/sessions`、`DELETE /api/v1/auth/sessions/{id}`（同租户审计与"吊销某台设备"，
    后者跨租户返回 404）。
  - `authenticate()` 变成"先 bearer 会话、后裸 `X-API-Key`"：服务端到服务端的调用方不用改一行。
  - 令牌的凭据语义也进了限流身份（`app/middleware.py`：`session:<hash前16位>`），
    否则所有会话会共享同一个客户端 IP 桶。
  - `last_used_at` 按 `SESSION_TOUCH_SECONDS`（默认 60）节流写：
    "审计要准"与"每个请求一次 UPDATE"之间取中；TTL 由 `SESSION_TTL_SECONDS`（默认 3600）控制。
- 替代方案与取舍：**签名 JWT** 不必查库、水平扩展最省事，但吊销只能靠黑名单或短 TTL，
  等于把刚解决的问题换个地方；也要求再引入一个"必须永远正确"的签名密钥。
  **Redis 会话**读得快，但生产不一定要 Redis、多副本要共享同一份状态，
  而审计记录本来就该落在已经有租户/文档关系的主库里。**HttpOnly Cookie**
  能挡住 XSS 读令牌，但会引入 CSRF 面与跨域配置，且与现有"前端显式带 header"的调用方式冲突。
  最终选了"不透明令牌 + 主库一行"，用一次带唯一索引的点查换掉一个不可撤销的共享密钥。
  `AUTH_ENABLED=false` 时端点**照样发令牌**（租户取 body 或 `demo-enterprise`，`authenticated=false`）：
  演示环境里 API 本来就是开放的，但让同一套前端代码在两种模式下都跑在"会到期的凭据"上，
  比"关掉鉴权就连 token 都没有"更接近生产路径。
- 新增测试：`tests/test_auth_sessions.py`（19 项）——交换成功返回 `ers_` 前缀与 TTL；
  库里只有 hash 与 fingerprint（断言 token 不是行里的 `token_hash`、密钥不在 fingerprint 里）；
  错密钥 401 + `WWW-Authenticate: ApiKey` 且不留行；bearer 能调受保护接口而裸密钥仍然可用；
  无凭据 401；跨租户 403；未知令牌 401 `unknown session token`；
  直接造一条 `expires_at` 已过的行 → 401 `expired`；登出后同一字符串 401 `revoked` 且 `revoked_at` 有值；
  `last_used_at` 在一个区间内不被二次刷新；审计列表的 `current` 标记与 `include_revoked`；
  跨租户吊销 404 且对方会话不受影响；同租户吊销后对方 401、自己 200；
  `purge_api_sessions` 先删过期、两天后再删已吊销；`AUTH_ENABLED=false` 下令牌同样可吊销；
  默认租户回退；body 租户与密钥不符 403。
  另把 `api_sessions` 加进 `tests/test_models.py` 的期望表集合；
  `tests/test_schema_parity.py` 会自动校验迁移与 `Base.metadata` 的表/列/索引集合一致
  （这也钉住了"`unique=True, index=True` 只生成唯一索引、不生成表级 UNIQUE 约束"这个细节：
  迁移里最初多写的 `UniqueConstraint` 会被它抓出来）。
- 验证命令及结果：
  - `pytest tests/test_auth_sessions.py tests/test_models.py tests/test_schema_parity.py` → **21 passed**；
  - 全量 `pytest` → **470 passed, 9 skipped**（此前 451 passed，新增 19 项）；
  - `ruff check app tests alembic scripts` → `All checks passed!`
    （顺带修掉 `app/api/routes/auth.py` 里 8 处 B008：
    `container: AppContainer = Depends(get_container)` 会触发 `function-call-in-default-argument`，
    改成 `Annotated[AppContainer, Depends(get_container)]` 后干净——这也是 FastAPI 现在的推荐写法）；
  - `mypy`（`files = ["app"]`）→ `Success: no issues found in 49 source files`；
  - `alembic upgrade head` 在全新 SQLite 上建出 `api_sessions`（由 `tests/test_schema_parity.py`
    与 `tests/test_migrations.py` 覆盖），已有 SQLite 库则由 `_upgrade_sqlite_schema()`
    的 `create_all` 补建该表，不需要手写 ALTER。
- 仍存在的限制：**API Key 本身仍是配置里的明文共享密钥**，轮换仍需改配置并重启，
  也没有按密钥的独立审计——`docs/priority-fixes.md` 里"API Key 支持哈希存储、轮换、吊销和审计"
  这一条只完成了后半（会话 token 的哈希存储、吊销、审计）。
  令牌只在**签发它的那一份数据库**里有效（多副本共用一个 PostgreSQL 即可，SQLite 部署不行），
  且每个请求都会查一次 `api_sessions`（有 `token_hash` 唯一索引）——没有进程内缓存，
  所以刚吊销的令牌不会因为缓存而不生效。
  `purge_api_sessions()` 已实现但**没有任何调度在调用它**（过期行会一直留在表里，
  需要 Celery beat 或运维脚本定期跑）。
  前端把令牌放在 `sessionStorage`（隐私模式降级为仅内存）而非 HttpOnly Cookie，
  所以 XSS 仍能拿走"当前标签页内有效"的令牌——只是拿不到长期密钥，且能被立刻吊销。
  没有刷新/续期机制：TTL 到了必须重新用密钥换一次。

## P1：可靠性与安全

- [x] **修正 `document_version` 默认值与多版本语料的语义冲突**（2026-09-26 发现，2026-10-04 完成，见事项二十六）
  - 现象：带显式版本上传的文档，在默认查询参数下检索不到任何引用。
  - 复现：上传 `version=v9` 的文档并等待 `ready`（`chunks=1`），随后以默认参数调用 `/api/v1/retrieval/search`（`document_version` 缺省为 `latest`），`citations` 为 0，`answer` 为拒答文本。
  - 根因：`app/schemas.py` 将 `document_version` 默认值设为字面量 `"latest"`，`app/core/store.py::get_chunks` 将其作为等值条件（`ChunkRecord.version == "latest"`）；因此 `"latest"` 只是“未指定版本上传时的标签”，并非“最新版本”。前端 `frontend/src/App.vue` 在检索版本输入为空时也会回填 `'latest'`，与同页面显示逻辑（空值表示“全部”）不一致。
  - 备选修复：把默认值改为 `None`（表示不过滤版本），或让 `latest` 解析为“每个文档的最大版本”。
  - 验收：上传 `v9` 文档后，默认查询能命中该文档；新增覆盖多版本语料的测试。

- [x] 上传改为分块流式写入，在读取过程中执行大小限制。（2026-09-27 完成）
- [x] 无文字层文件（扫描件）不再静默变成"ready 但 0 chunk"。（2026-09-27 完成，见事项十八）
- [x] 无文字层文档路由到 `needs_ocr`，并留出可插拔 OCR 后端接口。（2026-09-27 完成，见事项二十）
- [ ] 在镜像里安装 OCR 引擎（tesseract + `tesseract-ocr-chi-sim`）并用真实扫描件做端到端验证；
      给 `needs_ocr` 文档提供"配好引擎后重投"的入口（现在只能重新上传或 `reindex --force`）。
- [x] 表格类文件（`.xlsx`）行级序列化 + 每行重复列名，避免数字与列名分家。（2026-09-27 完成，见事项二十）
      实测一份 54×6 的补贴测算表：压平后 39.1% 的取值所在 chunk 找不到自己的列名，行级自描述后 0.0%。
- [ ] 校验文件 magic/MIME，并为 PDF/DOCX 解析设置超时、内存限制和任务 time limit。
- [ ] 建立租户存储配额与原始文件清理/归档策略。
- [x] Celery 使用原子状态迁移或分布式锁，防止两个 Worker 同时处理同一文档。（2026-09-27 完成）
- [x] 为外部索引写入设计 generation/outbox/可重放机制，处理 PostgreSQL 与检索后端双写一致性。（2026-10-07 完成，见事项三十六）
- [x] 会话令牌支持哈希存储、吊销与审计（2026-10-07 完成，见事项四十二；长期 API Key 本身的哈希存储与轮换仍未做）。

## P1：性能与数据模型

- [ ] 消除"整库 Chunk 加载 + Python 全量扫描"的查询路径。（2026-09-27 已量化，见评测报告 F5）
  - 实测：316 chunk 下单次检索 p50 685 ms（sparse）/1087 ms（hybrid），缓存关闭。
  - 原因一：`app/core/retrieval.py::retrieve` 在 `mode` 分支之前无条件计算两个通道，
    dense 分数算完即丢，sparse 配置也在为全语料嵌入付钱（`retrieve` 结束才判 `mode == "sparse"`）。
  - 原因二：`RetrievalService._build_retriever` 每查询新建 `LocalRetriever`，BM25 每次都对全语料重新分词，
    没有倒排索引与文档长度统计的复用。
  - 原因三：`HybridRetriever` 组合两个 `LocalRetriever`，而每个 `LocalRetriever` 内部又走完整 `retrieve()`，
    同一语料被嵌入两遍（profile 实测每查询 `embed()` 634 次 = 316 × 2）。
  - 验收：改造后在同一脚本重跑，给出改造前后 p50/p95 对照（推断量级：sparse ~280 ms、hybrid ~700 ms）。
- [x] async 路由改用 AsyncSession、`redis.asyncio`，避免同步 I/O 阻塞事件循环。（2026-09-27 完成缓存/限流与存储调用部分；AsyncSession 未做）
- [x] 状态字段增加 Enum/CheckConstraint，评测参数和结果在 PostgreSQL 使用 JSONB。（2026-10-07 完成，见事项三十七）
- [x] 补齐外键、级联删除和数据库级跨租户一致性约束。（2026-10-07 完成，见事项三十七；跨租户只做到删除语句带 `tenant_id` 条件，未做库级行级安全）
- [x] 统一 Alembic Schema 演进，减少 SQLite 手写升级逻辑。（2026-09-27 完成）
- [x] 补齐所有 migration downgrade 或明确采用 forward-only 策略。（2026-10-07 完成，见事项四十；`0011`/`0012` 的 downgrade 都已实现并幂等，逐版本 downgrade 由 `tests/test_migrations.py` 守着）

## P1：评测、可观测性与 CI

- [ ] 提升评测集区分度：当前 52 题在 BM25+重排下文档级指标全为 1.000，配置之间无法区分。
  - 成因一（已量化）：文档级指标在 13 份文档上太粗。同一批结果是 passage@1 0.769（BM25+重排）
    vs 0.288（dense-hash），聚合层把差异吃掉了；随机 5-chunk 就有 0.246 的文档命中率。
  - 成因二（已量化）：49/52 的提问含《…》法规全称，而全称在原文里逐字出现；
    去掉书名号后 R@1 由 1.000 降到 0.885（约 11.5 个百分点来自"问题里报了法规名"）。
  - 修法顺序：先做 passage 级指标（见下一条），再改题集——否则改完题集没有可比基线。
  - 题集改造方向：提问不点名法规、同义改写（"贷款期限"→"借多久"）、跨文档多跳、
    引入真正相近的干扰文档、加入应拒答的负样本。
  - 复跑命令：`python -m scripts.probe_golden_difficulty --database data/experiments/golden.db --tenant golden-experiment`
- [x] 把"证据引文命中"做成产品内指标（`evaluation_example` 增 `evidence_quote`）。（2026-09-27 完成）
  - 设计：`EvaluationExampleCreate.evidence_quote`（可空）→ `evaluation_examples.evidence_quote`（migration 0008）
    → Runner 对**带引文的样例**计算 `passage_hit`/`passage_at_1`/`passage_mrr`，逐题结果里记 `passage_rank`。
  - 取舍：没有引文的样例不参与 passage 均值（不稀释到 0），因此老数据集加上新字段后指标含义不变；
    引文比对前先去掉所有空白，避免与分块时的空白折叠冲突；空引文一律视为"无证据"而不是"全命中"。
  - 新增测试：`tests/test_evaluation.py`（排名/空白/空引文边界）、`tests/test_evaluation_runner.py`
    （聚合只在标注样例上平均）、`tests/test_evaluation_api.py`（引文经 API 往返并进入指标）。
  - 验证：两处 RED 探针（掐掉存储写入、掐掉 Runner 计算）分别让断言失败；全量 126 passed。
  - 交叉校验：脚本侧独立算一遍引文命中率，与产品指标逐位一致（`passage_cross_check`）。
- [x] 多跳题与应拒答题的标注结构（migration `0009`）。（2026-09-27 完成）
  - 结构：`expected_document_id` 变可空；新增 `should_refuse`（布尔）与 `expected_evidence_json`
    （JSON 数组 `[{document_id, page, quote}]`，多跳每题一跳一个引文）；
    Pydantic 侧 `EvidenceSpan` + `model_validator`：应拒答不许带证据、其余样例必须至少有一个标签、
    且 `expected_document_id` 必须与第一跳一致。
  - 取舍：跳用 JSON 列而不是子表——标注是整体读取、从不 JOIN 或按跳过滤；代价是 SQL 层无法约束形状，
    改由 Pydantic 兜住。子表的代价是多一张表 + 每次读取两趟查询，收益在当前用法下为零。
  - 指标语义变化：`recall@k` 从"0/1"变为"期望文档集合中被命中的比例"，
    新增 `all_targets@k`（全部跳都进 top-k 才算命中）；`page_hit` 变为"每个期望 (文档,页) 都在 top-k 里"；
    `passage_hit` 要求**每一跳**的引文都命中，`passage_mrr` 按跳取平均。
    单跳数据集的这些数字与改造前逐位相同（已用既有测试守住）。
  - 应拒答样例不参与任何检索指标，只记 `retrieved_something` 与聚合的 `negative_retrieved_rate`
    ——这是检索层的假阳性代理，真正的拒答判定要走答案链路，故刻意不与召回混在一起。
  - 验证：`pytest` 全量 133 passed（新增 `tests/test_evaluation_multi_hop.py` 7 项）、
    全新库 `alembic upgrade head` → `0009 (head)`、`downgrade -1` 在有应拒答样本时
    **拒绝执行**并报出条数（避免静默丢标签）。
  - 仍存在的限制：应拒答的"该不该拒"目前只由检索是否为空近似，答案层的拒答正确率尚未纳入；
    多跳的 nDCG 按跳平均，没有做位置重复折扣（多跳 nDCG 本身没有公认口径）。
- [ ] 评测 Runner 增加有界并发、单样例超时、进度、checkpoint、失败样例重试和取消能力。
- [x] 接入标准 Prometheus Histogram/Counter，并覆盖 Cache、Retrieval、Celery、DB Pool 指标。（2026-10-07 完成，见事项三十八）
- [ ] LangSmith 关闭时将 Trace 持久化到结构化日志、数据库或 OpenTelemetry Collector。
- [x] Readiness 根据启用配置检查 DB、Redis、任务队列及外部检索后端。（2026-10-07 完成，见事项三十八）
- [x] CI 增加 PostgreSQL/Redis、Alembic、Celery、Docker Compose、类型检查、覆盖率和依赖/镜像扫描。（2026-10-07 完成，见事项四十一；Redis/Celery 仍是既有 job 内的 fake/契约测试，`docker-build` 只构建不推送、不扫镜像 OS 包，"镜像扫描"只覆盖 Python 依赖）

## P2：部署演进

- [ ] Production Compose 使用固定 tag/SHA 的已发布镜像，不在服务器现场构建。
- [x] 上传文件迁移到 S3/MinIO 等对象存储，解除 API/Worker 共享本地卷限制。（2026-10-07 完成，见事项三十九；compose 里的 `uploads_data` 卷已换成 `minio_data`，默认后端仍是本地，需 `OBJECT_STORE=s3` 才切）
- [ ] PostgreSQL/Redis 使用托管或高可用方案，并验证备份、PITR 与恢复演练。
- [ ] 增加 staging 自动部署、production 审批、回滚及数据库迁移策略。

## 复盘模板

每次完成事项时追加：

```text
事项：
根因：
设计选择：
替代方案与取舍：
新增测试：
验证命令及结果：
性能/质量数据：
仍存在的限制：
关联提交：
```
