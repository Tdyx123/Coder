# PDDL Run RAG 方案

> 当前运行链路已改为任务分解专用 RAG：只召回 `decompose` 阶段的“任务 + 分解方案”作为 few-shot。本文后续关于 `allocate`、`problem_generation` 的阶段级 RAG 设计属于历史方案记录。

## 摘要

本方案从 `logs/intermediate_runs` 构建轻量本地 RAG 语料，不调用 LLM，不运行 planner，也不改变当前 `pddlrun_llmseparate.py` 主流程。

当前推荐检索输入是 clean dataset，而不是原始 corpus。原始 corpus 保留为上游构建、审计和重新 judge 的来源。

当前产物状态：

- 原始 corpus：`214,906` docs。
- clean corpus：`123,296` docs。
- 单一簇自然保留：`113,729` docs。
- 非单一簇 winner 保留：`9,567` docs。
- 超大簇 `cluster:problem_generation:000001` 已整簇剔除：`52,404` docs。
- 其他重复文档删除：`39,206` docs。
- clean dataset 校验：`validation.ok = true`。

v1 采用阶段级 RAG，为以下阶段分别汇总历史样例：

- `decompose`
- `allocate`
- `problem_generation`

失败和部分成功样例会保留质量标签，供后续分析或负例实验使用；默认只有成功样例参与检索。

## 语料 Schema

原始 corpus `data/rag/pddlrun_corpus.jsonl` 和 clean corpus `data/rag/pddlrun_clean_corpus.jsonl` 使用同一文档 schema。每一行是一条 JSON 文档，固定字段如下：

```json
{
  "id": "decompose:abc123",
  "stage": "decompose",
  "query_text": "task and context used for retrieval",
  "content": "example content to inject or inspect",
  "metadata": {
    "task": "open the book",
    "task_index": 0,
    "test_set": "final_test_new_0528_1",
    "floor_plan": "308",
    "task_run_dir": "/absolute/path/to/run",
    "source_paths": []
  },
  "quality": "success",
  "retrieval_eligible": true
}
```

`quality` 取值：

- `success`：completion 全通过，且所有 planner `return_code` 都是 `0`。
- `partial`：至少一个 subtask 有 planner 成功记录，但整体没有全通过。
- `failed`：没有 planner 成功记录，或缺少足够成功证据。

## 阶段文档

`decompose` 文档按 task run 生成一条，包含 task、robot 摘要、object/key-object 摘要和 decomposition 输出。

`allocate` 文档按 task run 生成一条，包含 task、subtasks、robot 能力摘要、key objects、allocation 输出，以及可用时的 final sequence of operations。

`problem_generation` 文档按 generated subtask 生成一条，包含 subtask 文本、可用的 assigned robot/domain 证据、raw/validated PDDL problem 内容和 planner 结果元数据。

## Cluster Winner Judge

原始 corpus 先按 `data/rag/pddlrun_similarity_clusters.json` 聚类。多文档簇需要挑选一个代表文档进入 clean corpus；单一簇不需要 judge。

本轮 winner 选择结果：

- 输入：`data/rag/pddlrun_corpus.jsonl`、`data/rag/pddlrun_similarity_clusters.json`。
- 输出：`data/rag/pddlrun_cluster_winners.jsonl`。
- 汇总：`data/rag/pddlrun_cluster_winners_summary.json`。
- 分片：`data/rag/pddlrun_cluster_judge_shards/`。
- 审核：10 个子 agent 分片检查，`10/10 PASS`，抽查 `59` 个 cluster，`0` 个修正建议。

Judge 优先级：

- 优先选择 `quality == "success"` 且 `retrieval_eligible == true` 的文档。
- 多个 success 候选并列时，按阶段内容完整性评估。
- 仍并列时，依次参考 `representative_doc_id`、`similarity_to_representative` 和稳定 `doc_id` 排序。

## 去重与 Clean Dataset

clean dataset 由原始 corpus、similarity clusters 和 winner 清单 materialize 得到。规则如下：

- 单一簇：自然保留其唯一文档。
- 非单一簇：保留 `pddlrun_cluster_winners.jsonl` 中的 winner 文档。
- 超大簇：整簇剔除，目前仅剔除 `cluster:problem_generation:000001`。
- 输出保持原始 corpus 文档字段和原始 corpus 顺序。

clean dataset 输入：

- `data/rag/pddlrun_corpus.jsonl`
- `data/rag/pddlrun_similarity_clusters.json`
- `data/rag/pddlrun_cluster_winners.jsonl`

clean dataset 输出：

- `data/rag/pddlrun_clean_corpus.jsonl`
- `data/rag/pddlrun_clean_index.json`
- `data/rag/pddlrun_clean_summary.json`

`data/rag/pddlrun_clean_summary.json` 的关键校验：

- `clean_document_count = 123296`
- `skipped_doc_count = 52404`
- `deduplicated_removed_document_count = 39206`
- `validation.ok = true`

clean corpus 按阶段和质量分布：

- `allocate`：`success=28530`，`partial=9140`，`failed=1543`。
- `decompose`：`success=27225`，`partial=8478`，`failed=1975`。
- `problem_generation`：`success=35479`，`partial=9446`，`failed=1480`。

## 构建命令

默认构建原始 corpus：

```bash
/home/dwb/.pyenv/bin/pyenv exec python scripts/build_pddl_rag_corpus.py
```

调试小样本：

```bash
/home/dwb/.pyenv/bin/pyenv exec python scripts/build_pddl_rag_corpus.py --limit-runs 10
```

原始 corpus 输出：

- `data/rag/pddlrun_corpus.jsonl`
- `data/rag/pddlrun_index.json`
- `data/rag/pddlrun_summary.json`

选择多文档簇 winner：

```bash
/home/dwb/.pyenv/bin/pyenv exec python scripts/judge_pddl_rag_clusters.py
```

生成 clean dataset：

```bash
/home/dwb/.pyenv/bin/pyenv exec python scripts/materialize_pddl_rag_clean_dataset.py
```

clean dataset 输出：

- `data/rag/pddlrun_clean_corpus.jsonl`
- `data/rag/pddlrun_clean_index.json`
- `data/rag/pddlrun_clean_summary.json`

## 后续接入

语料构建稳定后，可以在默认关闭的配置项后面让 `pddlrun_llmseparate.py` 加载 clean dataset，并把检索到的示例追加到 decompose、allocate、problem-generation prompt 中。

默认接入应加载：

- `data/rag/pddlrun_clean_index.json`
- `data/rag/pddlrun_clean_corpus.jsonl`

原始 `data/rag/pddlrun_corpus.jsonl` 仅用于重建、审计、重新聚类或重新 judge。

追加的 prompt block 应明确说明：检索样例只用于参考结构和推理方式，不能复制当前 task 中不存在的 object name、robot token 或 floor-plan fact。
