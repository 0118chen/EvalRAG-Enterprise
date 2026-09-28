# Golden set experiment (20 questions, 18 documents, 386 chunks)

- commit: `87054f5`  
- generated: 2026-09-28T08:19:18.066066+00:00  
- embedding: hash (32d), dense=local, sparse=local  
- top_k: 5  
- random 5-chunk draw: document hit 0.168, page hit 0.088 (21.4 chunks/document)

| config | what it isolates | Recall@1 | Recall@3 | Recall@5 | MRR | nDCG@3 | nDCG@5 | page_hit | passage@1 | passage_mrr | all@5 | any@5 | neg_retrieved | quote_hit | p50 ms | p95 ms |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| sparse-bm25 | BM25 only, the no-frills baseline | 0.800 | 0.850 | 0.950 | 0.850 | 0.832 | 0.875 | 0.900 | 0.700 | 0.775 | 0.950 | — | — | 0.850 | 741.801 | 865.433 |
| sparse-bm25-rerank | the reranking stage on top of BM25 alone | 0.800 | 0.850 | 0.950 | 0.850 | 0.832 | 0.875 | 0.900 | 0.700 | 0.775 | 0.950 | — | — | 0.850 | 802.365 | 903.390 |
| dense-hash | the dense channel on its own (32-d hash embedding, no semantics) | 0.300 | 0.400 | 0.450 | 0.343 | 0.350 | 0.369 | 0.300 | 0.050 | 0.112 | 0.450 | — | — | 0.250 | 499.703 | 556.889 |
| hybrid-rrf | RRF fusion of both channels, no rerank | 0.550 | 0.750 | 0.850 | 0.667 | 0.670 | 0.713 | 0.750 | 0.400 | 0.497 | 0.850 | — | — | 0.650 | 1239.117 | 1391.720 |
| hybrid-rrf-rerank | reranking on top of the fusion | 0.650 | 0.850 | 0.850 | 0.733 | 0.763 | 0.763 | 0.750 | 0.550 | 0.589 | 0.850 | — | — | 0.700 | 1252.546 | 1365.286 |
| hybrid-rrf-rerank-rewrite | rule-based query expansion on top of the product default | 0.650 | 0.850 | 0.850 | 0.733 | 0.763 | 0.763 | 0.750 | 0.550 | 0.589 | 0.850 | — | — | 0.700 | 1296.960 | 1523.118 |

## Per category (product default configuration)

| category | examples | Recall@3 | page_hit | quote_hit | notes |
|---|---|---|---|---|---|
| 同义改写 | 16 | 0.812 | 0.688 | 0.625 |  |
| 测算表 | 4 | 1.000 | 1.000 | 1.000 |  |
