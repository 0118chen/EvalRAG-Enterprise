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

## P1：可靠性与安全

- [ ] **修正 `document_version` 默认值与多版本语料的语义冲突**（2026-09-26 端到端验证发现）
  - 现象：带显式版本上传的文档，在默认查询参数下检索不到任何引用。
  - 复现：上传 `version=v9` 的文档并等待 `ready`（`chunks=1`），随后以默认参数调用 `/api/v1/retrieval/search`（`document_version` 缺省为 `latest`），`citations` 为 0，`answer` 为拒答文本。
  - 根因：`app/schemas.py` 将 `document_version` 默认值设为字面量 `"latest"`，`app/core/store.py::get_chunks` 将其作为等值条件（`ChunkRecord.version == "latest"`）；因此 `"latest"` 只是“未指定版本上传时的标签”，并非“最新版本”。前端 `frontend/src/App.vue` 在检索版本输入为空时也会回填 `'latest'`，与同页面显示逻辑（空值表示“全部”）不一致。
  - 备选修复：把默认值改为 `None`（表示不过滤版本），或让 `latest` 解析为“每个文档的最大版本”。
  - 验收：上传 `v9` 文档后，默认查询能命中该文档；新增覆盖多版本语料的测试。

- [x] 上传改为分块流式写入，在读取过程中执行大小限制。（2026-09-27 完成）
- [ ] 校验文件 magic/MIME，并为 PDF/DOCX 解析设置超时、内存限制和任务 time limit。
- [ ] 建立租户存储配额与原始文件清理/归档策略。
- [x] Celery 使用原子状态迁移或分布式锁，防止两个 Worker 同时处理同一文档。（2026-09-27 完成）
- [ ] 为外部索引写入设计 generation/outbox/可重放机制，处理 PostgreSQL 与检索后端双写一致性。
- [ ] API Key 支持哈希存储、轮换、吊销和审计。

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
- [ ] 状态字段增加 Enum/CheckConstraint，评测参数和结果在 PostgreSQL 使用 JSONB。
- [ ] 补齐外键、级联删除和数据库级跨租户一致性约束。
- [x] 统一 Alembic Schema 演进，减少 SQLite 手写升级逻辑。（2026-09-27 完成）
- [ ] 补齐所有 migration downgrade 或明确采用 forward-only 策略。

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
- [ ] 接入标准 Prometheus Histogram/Counter，并覆盖 Cache、Retrieval、Celery、DB Pool 指标。
- [ ] LangSmith 关闭时将 Trace 持久化到结构化日志、数据库或 OpenTelemetry Collector。
- [ ] Readiness 根据启用配置检查 DB、Redis、任务队列及外部检索后端。
- [ ] CI 增加 PostgreSQL/Redis、Alembic、Celery、Docker Compose、类型检查、覆盖率和依赖/镜像扫描。

## P2：部署演进

- [ ] Production Compose 使用固定 tag/SHA 的已发布镜像，不在服务器现场构建。
- [ ] 上传文件迁移到 S3/MinIO 等对象存储，解除 API/Worker 共享本地卷限制。
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
