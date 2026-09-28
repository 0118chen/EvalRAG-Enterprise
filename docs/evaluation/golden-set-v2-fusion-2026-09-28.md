# Golden set experiment (75 questions, 18 documents, 386 chunks)

- commit: `56db26f`  
- generated: 2026-09-28T11:41:00.602895+00:00  
- embedding: hash (32d), dense=local, sparse=local  
- top_k: 5  
- rerank backend: `lexical`  
- random 5-chunk draw: document hit 0.265, page hit 0.207 (21.4 chunks/document)

| config | what it isolates | Recall@1 | Recall@3 | Recall@5 | MRR | nDCG@3 | nDCG@5 | page_hit | passage@1 | passage_mrr | all@5 | any@5 | neg_retrieved | quote_hit | p50 ms | p95 ms |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| sparse-bm25 | BM25 only, the no-frills baseline | 0.660 | 0.851 | 0.944 | 0.790 | 0.792 | 0.829 | 0.851 | 0.612 | 0.728 | 0.934 | 1.000 | 1.000 | 0.821 | 864.454 | 1087.174 |
| sparse-bm25-rerank | the reranking stage on top of BM25 alone | 0.660 | 0.858 | 0.944 | 0.791 | 0.796 | 0.830 | 0.851 | 0.612 | 0.729 | 0.934 | 1.000 | 1.000 | 0.821 | 875.139 | 1152.739 |
| dense-hash | the dense channel on its own (32-d hash embedding, no semantics) | 0.276 | 0.423 | 0.527 | 0.380 | 0.379 | 0.423 | 0.328 | 0.119 | 0.199 | 0.492 | 0.833 | 1.000 | 0.284 | 462.001 | 547.516 |
| hybrid-rrf | RRF fusion of both channels, no rerank | 0.527 | 0.765 | 0.853 | 0.672 | 0.690 | 0.724 | 0.657 | 0.254 | 0.403 | 0.852 | 1.000 | 1.000 | 0.567 | 1206.553 | 1416.971 |
| hybrid-rrf-weighted | equal-weight fusion was the problem: weight the weak dense channel down | 0.581 | 0.818 | 0.887 | 0.734 | 0.757 | 0.775 | 0.687 | 0.313 | 0.457 | 0.869 | 1.000 | 1.000 | 0.612 | 1199.537 | 1528.032 |
| hybrid-rrf-truncate | only the top 5 of each channel may contribute, not its tail | 0.537 | 0.807 | 0.868 | 0.697 | 0.732 | 0.744 | 0.821 | 0.373 | 0.583 | 0.852 | 1.000 | 1.000 | 0.791 | 1161.765 | 1398.954 |
| hybrid-convex | keep the score magnitudes RRF throws away (min-max normalised sum) | 0.557 | 0.836 | 0.903 | 0.721 | 0.748 | 0.770 | 0.836 | 0.388 | 0.594 | 0.885 | 1.000 | 1.000 | 0.791 | 1219.909 | 1659.871 |
| hybrid-convex-weighted | normalised scores plus a down-weighted dense channel | 0.646 | 0.868 | 0.929 | 0.786 | 0.801 | 0.823 | 0.866 | 0.582 | 0.719 | 0.918 | 1.000 | 1.000 | 0.836 | 1214.543 | 1659.223 |
| hybrid-convex-weighted-rerank | the same shortlist through the reranker; which backend that is depends on RERANK_BACKEND, so this config name is the apples-to-apples slot for comparing lexical against TypeSafe | 0.646 | 0.868 | 0.929 | 0.786 | 0.801 | 0.823 | 0.866 | 0.597 | 0.727 | 0.918 | 1.000 | 1.000 | 0.836 | 1184.951 | 1429.491 |
| hybrid-rrf-rerank | reranking on top of the fusion | 0.546 | 0.818 | 0.878 | 0.705 | 0.735 | 0.753 | 0.687 | 0.299 | 0.452 | 0.869 | 1.000 | 1.000 | 0.612 | 1170.303 | 1394.932 |
| hybrid-rrf-rerank-rewrite | rule-based query expansion on top of the product default | 0.546 | 0.818 | 0.878 | 0.704 | 0.734 | 0.752 | 0.687 | 0.328 | 0.470 | 0.869 | 1.000 | 1.000 | 0.627 | 1283.408 | 2643.011 |

## Per category (product default configuration)

| category | examples | Recall@3 | page_hit | quote_hit | notes |
|---|---|---|---|---|---|
| 同义改写 | 52 | 0.827 | 0.673 | 0.635 |  |
| 多跳 | 9 | 0.889 | 0.667 | 0.444 |  |
| 等价多标签 | 6 | 0.639 | 0.833 | 0.833 |  |
| 应拒答 | 8 | — | — | — | 无检索指标；检索到内容的比例 1.000，top 分数中位数 0.7 |
