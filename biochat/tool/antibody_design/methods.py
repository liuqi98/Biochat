"""Reusable design methods distilled from CD19; contains no project sequences.

These tools plan work and assess supplied evidence. They do not launch models,
recompute structures, verify external files, or establish experimental affinity.
"""

from __future__ import annotations

import copy
import math


METHOD_DOC_IDS = (
    "cd19_design_method", "vh_only_design_workflow", "antibody_design_failure_modes",
)
METHOD_TOOL_NAMES = ("get_antibody_design_method", "assess_antibody_design_evidence")

_ROUTES = {
    "fixed_backbone": {
        "purpose": "已有可用复合物骨架，希望在指定可变位点探索序列。",
        "generation": "固定抗原与保护位点，使用 ProteinMPNN；按构象和设计掩码分组采样。",
        "inputs": ["complex_structure", "chain_and_residue_mapping", "mutable_positions", "protected_positions"],
    },
    "partial_diffusion": {
        "purpose": "已有复合物构象，需要在保持目标表位的条件下探索局部骨架。",
        "generation": "RFdiffusion 局部扩散→骨架硬筛→通过的骨架上 ProteinMPNN 序列设计。",
        "inputs": ["complex_structure", "chain_and_residue_mapping", "hotspot_mapping", "diffusion_mask"],
    },
    "h3_only": {
        "purpose": "已有母体与复合物，保持 H3 长度，仅重新设计指定 H3 位点。",
        "generation": "定义编号体系与 H3 范围，固定保护位点和 H3 以外区域；按母体分别运行 ProteinMPNN。",
        "inputs": ["parent_sequence", "parent_complex_structure", "numbering_scheme", "h3_span", "protected_positions"],
    },
    "h3_length_redesign": {
        "purpose": "改变 H3 长度，需要重新验证边界连接、几何与表位保持。",
        "generation": "用支持可变长度 H3 的结构设计流程（如 RFantibody），再进行序列设计与复合物结构检查。",
        "inputs": ["parent_complex_structure", "numbering_scheme", "h3_span", "allowed_h3_lengths", "hotspot_mapping"],
    },
}

_REQUIRED_GATES = {
    "pilot": ("input_mapping", "fixed_regions", "sequence", "geometry", "target_preservation", "epitope", "provenance"),
    "candidate": ("input_mapping", "fixed_regions", "sequence", "geometry", "target_preservation", "epitope", "provenance"),
}
_FORMATS = ("human_vh_only", "vhh", "vh_vl")


def get_antibody_design_method(route: str = "h3_only", antibody_format: str = "human_vh_only") -> dict:
    """Return a reusable design plan, required inputs and decision gates.

    Select a route explicitly. Position numbers, thresholds and budgets must be
    supplied for the new project; historical CD19 settings are never defaults.
    """
    if route not in _ROUTES:
        raise ValueError(f"route must be one of {tuple(_ROUTES)}")
    if antibody_format not in _FORMATS:
        raise ValueError(f"antibody_format must be one of {_FORMATS}")
    return {
        "method_version": "1.0", "origin": "CD19 project methodology; no candidate data included",
        "route": route, "antibody_format": antibody_format,
        **copy.deepcopy(_ROUTES[route]),
        "steps": [
            {"stage": "prepare", "action": "核对链、残基编号、抗原裁剪、热点映射与可变/保护区域；检查工具和权重可用性。"},
            {"stage": "pilot", "action": "小批量试跑，核查产物和所有 pilot 门槛；失败或缺失时不扩量。"},
            {"stage": "generate", "action": _ROUTES[route]["generation"]},
            {"stage": "sequence_screen", "action": "检查固定区和序列风险；失败者保留原因，不进入正式优选。"},
            {"stage": "structure", "action": "区分游离态 VH 建模和结合态复合物；分别检查框架、CDR、连接边界及冲突。"},
            {"stage": "hard_screen", "action": "核查几何、抗原保持和目标表位；保留失败原因。局部扩散骨架须在序列设计前先通过骨架检查。"},
            {"stage": "score", "action": "通过硬筛后解释计算分数；配对差值要求明确参考、相同骨架和匹配协议。"},
            {"stage": "developability", "action": "按抗体格式选择方法；记录构象来源和参考分布，不能用参考颜色覆盖硬筛失败。"},
            {"stage": "panel", "action": "结合母体对照、路线平衡、序列多样性与不确定性选择面板；输出证据来源及缺失项。"},
            {"stage": "experiment", "action": "计划表达、单体比例、稳定性、同价态结合、竞争及靶标阳性/阴性细胞验证。"},
        ],
        "required_gates": copy.deepcopy(_REQUIRED_GATES),
        "gate_evidence_schema": {"status": "pass | fail | not_run", "evidence_ref": "source artifact identifier"},
        "score_schema": {
            "kind": "interface_dG | paired_delta_interface_dG | rmsd | proxy",
            "value": "finite number", "unit": "REU | angstrom | arbitrary",
            "evidence_ref": "source artifact identifier",
            "paired_delta_fields": ["reference_id", "same_backbone", "matched_protocol"],
            "rmsd_fields": ["reference_id", "alignment_region", "measurement_region"],
        },
        "format_notes": _format_notes(antibody_format),
        "project_specific_configuration": [
            "chain_mapping", "numbering_scheme", "h3_span", "protected_positions", "hotspots",
            "geometry_thresholds", "epitope_thresholds", "sampling_budget", "panel_diversity_policy",
        ],
        "knowledge_documents": list(METHOD_DOC_IDS),
        "execution_status": "plan_only", "experimental_validation": "not_established",
    }


def _format_notes(antibody_format: str) -> list[str]:
    if antibody_format == "vh_vl":
        return ["TAP 的使用需要有效配对 VH/VL，并满足其输入协议。"]
    return [
        "VH-only 不应为运行 TAP 而虚构或随意补配 VL。",
        "TNP 必须记录建模/结构来源及参考分布；提取结合态 VH 不等于预测游离态构象。",
        "人源 VH-only 的 TNP 参考颜色仅作探索性信息，不等同于临床纳米抗体验证结论。"
        if antibody_format == "human_vh_only" else "纳米抗体参考颜色仍不构成实验开发性证明。",
    ]


def assess_antibody_design_evidence(evidence: dict, stage: str = "candidate",
                                    antibody_format: str = "human_vh_only") -> dict:
    """Apply hard-gate precedence to caller-supplied evidence, without reading files.

    evidence contains gates (named status/evidence_ref objects), optional scores
    (typed value/unit/context objects), and optional developability metadata.
    Missing evidence never passes. A favourable score cannot override a failed gate.
    """
    if stage not in _REQUIRED_GATES:
        raise ValueError("stage must be pilot or candidate")
    if antibody_format not in _FORMATS:
        raise ValueError(f"antibody_format must be one of {_FORMATS}")
    if not isinstance(evidence, dict) or not isinstance(evidence.get("gates", {}), dict):
        raise ValueError("evidence and evidence.gates must be objects")
    gates = evidence.get("gates", {})
    failed, missing = [], []
    for name in _REQUIRED_GATES[stage]:
        gate = gates.get(name, {})
        if not isinstance(gate, dict) or gate.get("status") not in ("pass", "fail", "not_run", None):
            raise ValueError(f"Invalid gate {name}: use status pass/fail/not_run and evidence_ref")
        if gate.get("status") == "fail":
            failed.append(name)
        if gate.get("status") in (None, "not_run") or not _has_text(gate.get("evidence_ref")):
            missing.append(name)

    scores = evidence.get("scores", [])
    if not isinstance(scores, list):
        raise ValueError("evidence.scores must be a list")
    score_reviews = [_review_score(score) for score in scores]
    issues = [f"score[{i}]: {issue}" for i, review in enumerate(score_reviews) for issue in review["issues"]]
    unknown_gates = sorted(set(gates) - set(_REQUIRED_GATES[stage]))
    if unknown_gates:
        issues.append("unrecognized_gates: " + ", ".join(unknown_gates))
    development = evidence.get("developability", {})
    if not isinstance(development, dict):
        raise ValueError("evidence.developability must be an object")
    method = str(development.get("method", "")).upper()
    if method and method not in ("TAP", "TNP"):
        issues.append("unrecognized_developability_method")
    if method == "TAP" and antibody_format != "vh_vl":
        issues.append("TAP_requires_paired_VH_VL")
    if method == "TAP" and not _has_text(development.get("evidence_ref")):
        issues.append("TAP_missing_evidence_ref")
    if method == "TNP":
        for key in ("structure_source", "conformation_state", "reference_population", "evidence_ref"):
            if not _has_text(development.get(key)):
                issues.append(f"TNP_missing_{key}")

    if failed:
        decision = "stop_before_batch" if stage == "pilot" else "redesign_required"
    elif missing or issues:
        decision = "insufficient_evidence"
    else:
        decision = "pilot_gates_passed" if stage == "pilot" else "eligible_for_computational_prioritization"
    return {
        "decision": decision, "stage": stage, "failed_gates": failed, "missing_evidence": missing,
        "evidence_issues": issues, "score_reviews": score_reviews,
        "unrecognized_gates": unknown_gates,
        "next_action": (
            "先修复失败项并重跑对应检查，保留原始失败记录；不能用能量或开发性颜色覆盖失败。" if failed else
            "补齐缺失证据或评分语义后重新检查。" if missing or issues else
            "可按资源预算扩量，并持续执行各项门槛。" if stage == "pilot" else
            "进入计算优先级与多样性面板选择；仍需单独完成开发性评估与实验验证。"
        ),
        "format_notes": _format_notes(antibody_format),
        "assessment_basis": "caller_supplied_evidence_not_independently_verified",
        "experimental_affinity_established": False,
    }


def _has_text(value) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _review_score(score: dict) -> dict:
    if not isinstance(score, dict):
        raise ValueError("Each score must be an object")
    kind, value = score.get("kind"), score.get("value")
    units = {"interface_dG": "REU", "paired_delta_interface_dG": "REU", "rmsd": "angstrom", "proxy": "arbitrary"}
    issues = []
    if kind not in units:
        issues.append("unknown_score_kind")
    if type(value) not in (int, float) or not math.isfinite(value):
        issues.append("value_must_be_finite_number")
    if score.get("unit") != units.get(kind):
        issues.append("incorrect_or_missing_unit")
    if not _has_text(score.get("evidence_ref")):
        issues.append("missing_source")
    if kind == "paired_delta_interface_dG":
        if not _has_text(score.get("reference_id")):
            issues.append("missing_paired_reference")
        if score.get("same_backbone") is not True or score.get("matched_protocol") is not True:
            issues.append("pairing_requires_same_backbone_and_matched_protocol")
    if kind == "rmsd":
        for key in ("reference_id", "alignment_region", "measurement_region"):
            if not _has_text(score.get(key)):
                issues.append(f"missing_{key}")
        if type(value) in (int, float) and value < 0:
            issues.append("negative_rmsd")
    interpretations = {
        "interface_dG": "单个复合物的计算界面能；不等于相对母体的配对差值，不能直接跨骨架比较。",
        "paired_delta_interface_dG": "候选减配对参考；负值仅表示该匹配协议下计算方向有利，不代表实验亲和力提升。",
        "rmsd": "只解释指定参考、对齐区域与测量区域的结构偏差；低 RMSD 不能证明结合。",
        "proxy": "代理分数仅按自身定义解释，不可改名为界面能或实验亲和力。",
    }
    return {"kind": kind, "issues": issues, "interpretable": not issues,
            "interpretation": interpretations.get(kind, "未知评分，需先明确方法与单位。"),
            "supports_experimental_affinity": False}
