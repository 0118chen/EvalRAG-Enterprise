# Golden set experiment (75 questions, 18 documents, 386 chunks)

- commit: `b4d30d5`  
- generated: 2026-09-27T15:39:33.080617+00:00  
- embedding: hash (32d), dense=local, sparse=local  
- top_k: 5  
- random 5-chunk draw: document hit 0.265, page hit 0.207 (21.4 chunks/document)

| config | what it isolates | Recall@1 | Recall@3 | Recall@5 | MRR | nDCG@3 | nDCG@5 | page_hit | passage@1 | passage_mrr | all@5 | any@5 | neg_retrieved | quote_hit | p50 ms | p95 ms |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| sparse-bm25 | BM25 only, the no-frills baseline | 0.660 | 0.851 | 0.944 | 0.790 | 0.792 | 0.829 | 0.851 | 0.612 | 0.728 | 0.934 | 1.000 | 1.000 | 0.821 | 816.252 | 1065.481 |
| sparse-bm25-rerank | the reranking stage on top of BM25 alone | 0.660 | 0.858 | 0.944 | 0.791 | 0.796 | 0.830 | 0.851 | 0.612 | 0.729 | 0.934 | 1.000 | 1.000 | 0.821 | 809.822 | 1070.445 |
| dense-hash | the dense channel on its own (32-d hash embedding, no semantics) | 0.276 | 0.423 | 0.527 | 0.380 | 0.379 | 0.423 | 0.328 | 0.119 | 0.199 | 0.492 | 0.833 | 1.000 | 0.284 | 463.829 | 671.698 |
| hybrid-rrf | RRF fusion of both channels, no rerank | 0.512 | 0.735 | 0.853 | 0.660 | 0.668 | 0.715 | 0.657 | 0.239 | 0.387 | 0.852 | 1.000 | 1.000 | 0.567 | 1324.091 | 1867.768 |
| hybrid-rrf-rerank | reranking on top of the fusion | 0.546 | 0.818 | 0.878 | 0.705 | 0.735 | 0.753 | 0.687 | 0.299 | 0.452 | 0.869 | 1.000 | 1.000 | 0.612 | 1644.788 | 1916.560 |
| hybrid-rrf-rerank-rewrite | rule-based query expansion on top of the product default | 0.546 | 0.818 | 0.878 | 0.704 | 0.734 | 0.752 | 0.687 | 0.328 | 0.469 | 0.869 | 1.000 | 1.000 | 0.612 | 1316.368 | 2574.164 |

## Per category (product default configuration)

| category | examples | Recall@3 | page_hit | quote_hit | notes |
|---|---|---|---|---|---|
| 同义改写 | 52 | 0.827 | 0.673 | 0.635 |  |
| 多跳 | 9 | 0.889 | 0.667 | 0.333 |  |
| 等价多标签 | 6 | 0.639 | 0.833 | 0.833 |  |
| 应拒答 | 8 | — | — | — | 无检索指标；检索到内容的比例 1.000，top 分数中位数 0.7 |
