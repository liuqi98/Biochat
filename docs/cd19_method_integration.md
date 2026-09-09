# CD19 方法经验集成

此功能将 CD19 项目中的决策方法用于通用抗体任务。只包含方法文档、规划工具和证据检查，
不包含项目序列、候选库、候选排名、结构文件或模型权重。

## 在聊天中使用

- “参考 CD19 项目的方法，为我的 VH-only 抗体规划固定骨架序列设计。”
- “我想改变 H3 长度，需要哪些输入和筛选门槛？”
- “候选的能量有利，但表位检查失败，下一步应该怎么办？”
- “检查这组评分能否作为同骨架配对差值解释。”

方法文档通过 `KnowledgeRegistry` 加载。设计相关请求会在常规资源检索后补入方法文档和两个工具，
避免它们被语义检索遗漏；这一步不增加 LLM 调用。仅询问 CD19 的细胞表达等问题不触发该补充规则。
关闭工具检索时，沿用 Agent 原本加载全部知识文档的行为。

## 工具接口

```python
from biochat.tool.antibody_design import (
    get_antibody_design_method,
    assess_antibody_design_evidence,
)

plan = get_antibody_design_method(
    route="h3_length_redesign",
    antibody_format="human_vh_only",
)
print(plan["inputs"])
print(plan["required_gates"]["pilot"])

# 接入真实执行产物之前，明确标记缺失；不要用默认 pass 填充。
evidence = {
    "gates": {
        name: {"status": "not_run", "evidence_ref": ""}
        for name in plan["required_gates"]["pilot"]
    }
}
review = assess_antibody_design_evidence(evidence, stage="pilot")
assert review["decision"] == "insufficient_evidence"
```

`route` 支持 `fixed_backbone`、`partial_diffusion`、`h3_only`、`h3_length_redesign`。
`antibody_format` 支持 `human_vh_only`、`vhh`、`vh_vl`。
两个工具在 `minimal` 和 `full` 配置下均可用，不需要 GPU 或原项目目录。

`evidence.gates` 使用 `input_mapping`、`fixed_regions`、`sequence`、`geometry`、
`target_preservation`、`epitope`、`provenance`；每项有 `status` 和 `evidence_ref`。
证据检查只解释调用方提供的记录，不自动核验外部文件存在性或重算指标。
执行层需要填入真实产物标识，并按当前项目配置检查阈值。

可选的 `scores` 是对象列表，包含 `kind`、`value`、`unit`、`evidence_ref`：

| kind | unit | 额外上下文 |
|---|---|---|
| `interface_dG` | `REU` | 单个复合物界面能，不自动作为突变差值 |
| `paired_delta_interface_dG` | `REU` | `reference_id`、`same_backbone=true`、`matched_protocol=true` |
| `rmsd` | `angstrom` | `reference_id`、`alignment_region`、`measurement_region` |
| `proxy` | `arbitrary` | 仅按代理分数定义解释 |

可选的 `developability` 记录 `method`。TAP 需要配对 VH/VL 与 `evidence_ref`；
TNP 还需要 `structure_source`、`conformation_state`、`reference_population` 与 `evidence_ref`。
TNP 参考颜色不参与硬筛通过判定。

## 结果含义

- `stop_before_batch`：pilot 有失败，先修复再扩量。
- `redesign_required`：候选有失败，不能由能量或开发性颜色覆盖。
- `insufficient_evidence`：缺证据或语义不完整，需要补齐。
- `pilot_gates_passed`：调用方报告的 pilot 门槛通过，可按预算继续。
- `eligible_for_computational_prioritization`：允许继续计算优先级和面板选择。

所有结果都不构成实验亲和力或开发性验证。新项目的编号、保护位点、热点、阈值和预算必须独立配置。
这些工具不会自动调用 ProteinMPNN、RFdiffusion、RFantibody、IgFold 或 Rosetta；
实际执行仍需相应的已安装工具和适配器。

## 验证与更新

方法测试使用合成的门槛与指标记录，不含项目序列：

```bash
pytest -q tests/test_antibody_design_methods.py tests/test_knowledge_registry.py \
  tests/test_tool_profiles.py tests/test_tool_catalog.py
```

合并更新后重启 BioChat 进程，以刷新缓存 Agent、工具目录和知识注册表。
本集成与实时进度显示 PR 独立；不要求先合并那个 PR。
