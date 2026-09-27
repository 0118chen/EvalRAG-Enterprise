# Golden set experiment (75 questions, 13 documents, 316 chunks)

- commit: `9f4ce82`  
- generated: 2026-09-27T14:44:03.866087+00:00  
- embedding: hash (32d), dense=local, sparse=local  
- top_k: 5  
- random 5-chunk draw: document hit 0.292, page hit 0.223 (24.3 chunks/document)

| config | what it isolates | Recall@1 | Recall@3 | Recall@5 | MRR | nDCG@3 | nDCG@5 | page_hit | passage@1 | passage_mrr | all@5 | any@5 | neg_retrieved | quote_hit | p50 ms | p95 ms |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| sparse-bm25 | BM25 only, the no-frills baseline | 0.660 | 0.851 | 0.944 | 0.793 | 0.794 | 0.831 | 0.851 | 0.612 | 0.731 | 0.934 | 1.000 | 1.000 | 0.821 | 703.344 | 915.107 |
| sparse-bm25-rerank | the reranking stage on top of BM25 alone | 0.660 | 0.866 | 0.944 | 0.794 | 0.802 | 0.832 | 0.851 | 0.612 | 0.731 | 0.934 | 1.000 | 1.000 | 0.821 | 725.183 | 893.923 |
| dense-hash | the dense channel on its own (32-d hash embedding, no semantics) | 0.321 | 0.473 | 0.527 | 0.418 | 0.434 | 0.452 | 0.328 | 0.164 | 0.229 | 0.492 | 0.833 | 1.000 | 0.284 | 421.138 | 605.350 |
| hybrid-rrf | RRF fusion of both channels, no rerank | 0.532 | 0.741 | 0.869 | 0.679 | 0.681 | 0.733 | 0.687 | 0.269 | 0.406 | 0.869 | 1.000 | 1.000 | 0.597 | 1138.788 | 1672.566 |
| hybrid-rrf-rerank | reranking on top of the fusion | 0.532 | 0.820 | 0.883 | 0.698 | 0.730 | 0.748 | 0.701 | 0.299 | 0.459 | 0.869 | 1.000 | 1.000 | 0.642 | 1111.077 | 1397.960 |
| hybrid-rrf-rerank-rewrite | rule-based query expansion on top of the product default | 0.532 | 0.820 | 0.883 | 0.697 | 0.729 | 0.747 | 0.701 | 0.328 | 0.476 | 0.869 | 1.000 | 1.000 | 0.642 | 1151.458 | 2292.503 |

## Per category (product default configuration)

| category | examples | Recall@3 | page_hit | quote_hit | notes |
|---|---|---|---|---|---|
| 同义改写 | 52 | 0.827 | 0.692 | 0.673 |  |
| 多跳 | 9 | 0.889 | 0.667 | 0.333 |  |
| 等价多标签 | 6 | 0.653 | 0.833 | 0.833 |  |
| 应拒答 | 8 | — | — | — | 无检索指标；检索到内容的比例 1.000，top 分数中位数 0.7 |
