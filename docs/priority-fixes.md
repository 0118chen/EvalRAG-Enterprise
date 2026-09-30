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

## P1：可靠性与安全

- [ ] **修正 `document_version` 默认值与多版本语料的语义冲突**（2026-09-26 端到端验证发现）
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
