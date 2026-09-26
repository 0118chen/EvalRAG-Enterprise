# EvalRAG Enterprise 优先修复清单

> 建立日期：2026-09-26  
> 用途：代码复盘、版本规划、简历真实性检查。完成事项应补充验证命令和结果，不只勾选状态。

## P0：公开仓库或简历演示前

- [ ] **LangSmith Dataset 增加租户命名空间**
  - 风险：不同租户使用相同 Dataset 名称时，远端样本可能混存。
  - 位置：`app/api/routes/evaluations.py`、`app/core/langsmith_eval.py`
  - 验收：两个租户创建同名 Dataset 后映射到不同远端名称；测试覆盖同步与实验查询。

- [ ] **修复 Redis 故障降级异常类型**
  - 风险：Redis 断连时 Cache/Rate Limit 抛出未处理异常，API 返回 500。
  - 位置：`app/core/cache.py`、`app/core/rate_limit.py`
  - 验收：捕获 `redis.exceptions.RedisError`；Redis 不可用时按既定策略降级；补充集成或故障注入测试。

- [ ] **保护真实 LLM/LangSmith 健康检查**
  - 风险：公开 `/health/llm` 可被匿名重复调用并产生模型费用，且绕过 `/api/` 限流。
  - 位置：`app/api/routes/health.py`、`app/middleware.py`、`deploy/nginx/https.conf`
  - 验收：live/ready 保持廉价；外部连通检查需要认证、内网访问或独立管理端点，并有测试。

- [ ] **校验 Baseline Evaluation 租户归属**
  - 风险：异步 Runner 可按其他租户的 Evaluation ID 计算差值，泄露聚合指标并污染结果。
  - 位置：`app/api/routes/evaluations.py`、`app/core/evaluation_runner.py`
  - 验收：创建任务及 Runner 执行时均验证当前评测与基线属于同一租户。

- [ ] **Staging 默认启用认证或仅绑定本机地址**
  - 风险：认证关闭时服务端直接信任请求中的 `tenant_id`。
  - 位置：`.env.staging.example`、`deploy/docker-compose.staging.yml`
  - 验收：示例配置默认不暴露无认证多租户 API。

- [ ] **升级存在安全公告的 pytest 开发依赖**
  - 当前：`pytest>=8.3,<9`，环境中为 8.4.2。
  - 扫描：`pip-audit` 报告 `PYSEC-2026-1845`，修复版本 9.0.3。
  - 验收：升级后完整测试通过。

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

## P1：可靠性与安全

- [ ] **修正 `document_version` 默认值与多版本语料的语义冲突**（2026-09-26 端到端验证发现）
  - 现象：带显式版本上传的文档，在默认查询参数下检索不到任何引用。
  - 复现：上传 `version=v9` 的文档并等待 `ready`（`chunks=1`），随后以默认参数调用 `/api/v1/retrieval/search`（`document_version` 缺省为 `latest`），`citations` 为 0，`answer` 为拒答文本。
  - 根因：`app/schemas.py` 将 `document_version` 默认值设为字面量 `"latest"`，`app/core/store.py::get_chunks` 将其作为等值条件（`ChunkRecord.version == "latest"`）；因此 `"latest"` 只是“未指定版本上传时的标签”，并非“最新版本”。前端 `frontend/src/App.vue` 在检索版本输入为空时也会回填 `'latest'`，与同页面显示逻辑（空值表示“全部”）不一致。
  - 备选修复：把默认值改为 `None`（表示不过滤版本），或让 `latest` 解析为“每个文档的最大版本”。
  - 验收：上传 `v9` 文档后，默认查询能命中该文档；新增覆盖多版本语料的测试。

- [ ] 上传改为分块流式写入，在读取过程中执行大小限制。
- [ ] 校验文件 magic/MIME，并为 PDF/DOCX 解析设置超时、内存限制和任务 time limit。
- [ ] 建立租户存储配额与原始文件清理/归档策略。
- [ ] Celery 使用原子状态迁移或分布式锁，防止两个 Worker 同时处理同一文档。
- [ ] 为外部索引写入设计 generation/outbox/可重放机制，处理 PostgreSQL 与检索后端双写一致性。
- [ ] API Key 支持哈希存储、轮换、吊销和审计。

## P1：性能与数据模型

- [ ] 消除“整库 Chunk 加载 + Python 全量扫描”的查询路径。
- [ ] async 路由改用 AsyncSession、`redis.asyncio`，避免同步 I/O 阻塞事件循环。
- [ ] 状态字段增加 Enum/CheckConstraint，评测参数和结果在 PostgreSQL 使用 JSONB。
- [ ] 补齐外键、级联删除和数据库级跨租户一致性约束。
- [ ] 统一 Alembic Schema 演进，减少 SQLite 手写升级逻辑。
- [ ] 补齐所有 migration downgrade 或明确采用 forward-only 策略。

## P1：评测、可观测性与 CI

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
