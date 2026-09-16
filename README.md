# EvalRAG Enterprise

评测驱动的多租户企业 RAG 平台，面向政务、金融和合规文档问答。

当前版本提供可运行的 FastAPI MVP：健康检查、知识库创建、租户隔离、PDF/DOCX/TXT/Markdown 上传、结构化分块、关键词/混合检索、引用、反馈和评测任务接口，以及可选的 LangSmith 观测配置。`app/core/evaluation.py` 同时提供可复用的 Recall@K、MRR 计算基础。

问答接口 `POST /api/v1/chat/stream` 在 `hybrid` 模式下会并行运行 Dense/Sparse Retriever，再使用 RRF 融合结果。当前本地 Dense 实现用于离线开发；生产环境可替换为 Milvus 和 Elasticsearch 后端。

```bash
python -m venv .venv
pip install -e ".[dev]"
uvicorn app.main:app --reload
```

Docker：

```bash
cp .env.example .env
docker compose up --build
```

复制 `.env.example` 为 `.env`，填入 `LANGSMITH_API_KEY` 并设置 `LANGSMITH_ENABLED=true`。生产环境应仅发送脱敏 metadata、文档 ID、页码和受控摘要，不上传完整原文。LangSmith 不可用时不应阻塞主业务。
