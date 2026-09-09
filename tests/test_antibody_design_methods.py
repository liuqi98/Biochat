"""Method-only regressions: use synthetic gate/metric evidence, no sequences."""

from __future__ import annotations

import copy
from pathlib import Path
from types import SimpleNamespace

import pytest

from biochat.knowledge import KnowledgeRegistry
from biochat.tool.antibody_design import assess_antibody_design_evidence, get_antibody_design_method
from biochat.tool.antibody_design.methods import METHOD_DOC_IDS, METHOD_TOOL_NAMES
from biochat.tool.registry import ToolRegistry
from biochat.utils.io_utils import load_all_tool_descriptions


def passed_evidence(stage="candidate"):
    """Synthetic evidence references; no actual project identifiers or results."""
    gates = get_antibody_design_method()["required_gates"][stage]
    return {"gates": {name: {"status": "pass", "evidence_ref": f"synthetic/{name}.json"} for name in gates}}


@pytest.mark.parametrize("route", ["fixed_backbone", "partial_diffusion", "h3_only", "h3_length_redesign"])
def test_method_routes_keep_configuration_project_specific(route):
    plan = get_antibody_design_method(route)
    assert plan["route"] == route
    assert plan["execution_status"] == "plan_only"
    assert "protected_positions" in plan["project_specific_configuration"]
    assert "geometry_thresholds" in plan["project_specific_configuration"]
    assert "hotspots" in plan["project_specific_configuration"]
    assert plan["experimental_validation"] == "not_established"
    assert not any("sequence" in key for key in plan)  # input requirements are names, never stored sequences


def test_plan_return_values_do_not_mutate_later_requests():
    plan = get_antibody_design_method()
    plan["inputs"].clear()
    assert get_antibody_design_method()["inputs"]


@pytest.mark.parametrize("stage,decision", [("pilot", "stop_before_batch"), ("candidate", "redesign_required")])
def test_favorable_energy_and_green_reference_flags_never_override_epitope_failure(stage, decision):
    evidence = passed_evidence(stage)
    evidence["gates"]["epitope"]["status"] = "fail"
    evidence["scores"] = [{"kind": "interface_dG", "value": -1.0, "unit": "REU", "evidence_ref": "synthetic/score"}]
    evidence["developability"] = {
        "method": "TNP", "structure_source": "RF2", "conformation_state": "bound_extracted",
        "reference_population": "clinical_vhh", "evidence_ref": "synthetic/tnp", "reference_flag": "green",
    }
    original = copy.deepcopy(evidence)
    result = assess_antibody_design_evidence(evidence, stage)
    assert result["decision"] == decision
    assert result["failed_gates"] == ["epitope"]
    assert not result["experimental_affinity_established"]
    assert evidence == original


def test_missing_evidence_is_not_success():
    evidence = passed_evidence()
    evidence["gates"]["geometry"] = {"status": "pass"}
    evidence["gates"]["provenance"]["status"] = "not_run"
    result = assess_antibody_design_evidence(evidence)
    assert result["decision"] == "insufficient_evidence"
    assert set(result["missing_evidence"]) == {"geometry", "provenance"}
    assert assess_antibody_design_evidence({})["decision"] == "insufficient_evidence"


def test_passed_candidate_only_enters_computational_prioritization():
    result = assess_antibody_design_evidence(passed_evidence())
    assert result["decision"] == "eligible_for_computational_prioritization"
    assert not result["experimental_affinity_established"]
    assert "not_independently_verified" in result["assessment_basis"]


def test_pairing_and_rmsd_context_are_required():
    evidence = passed_evidence()
    evidence["scores"] = [
        {"kind": "paired_delta_interface_dG", "value": -1.0, "unit": "REU", "evidence_ref": "synthetic/score"},
        {"kind": "rmsd", "value": 0.1, "unit": "angstrom", "evidence_ref": "synthetic/rmsd"},
    ]
    result = assess_antibody_design_evidence(evidence)
    assert result["decision"] == "insufficient_evidence"
    assert "missing_paired_reference" in str(result["evidence_issues"])
    assert "missing_measurement_region" in str(result["evidence_issues"])
    evidence["scores"][0].update(reference_id="synthetic-parent", same_backbone=True, matched_protocol=True)
    evidence["scores"][1].update(reference_id="synthetic-reference", alignment_region="framework_CA", measurement_region="H3_CA")
    assert assess_antibody_design_evidence(evidence)["decision"] == "eligible_for_computational_prioritization"
    evidence["scores"][0]["same_backbone"] = False
    assert assess_antibody_design_evidence(evidence)["decision"] == "insufficient_evidence"


@pytest.mark.parametrize("value", [float("nan"), float("inf"), True, "-1"])
def test_invalid_score_values_cannot_be_interpreted(value):
    evidence = passed_evidence()
    evidence["scores"] = [{"kind": "interface_dG", "value": value, "unit": "REU", "evidence_ref": "synthetic"}]
    assert assess_antibody_design_evidence(evidence)["score_reviews"][0]["interpretable"] is False


def test_energy_unit_and_tap_applicability_are_checked():
    evidence = passed_evidence()
    evidence["scores"] = [{"kind": "interface_dG", "value": -1, "unit": "kcal/mol", "evidence_ref": "synthetic"}]
    evidence["developability"] = {"method": "TAP", "evidence_ref": "synthetic"}
    result = assess_antibody_design_evidence(evidence, antibody_format="human_vh_only")
    assert "TAP_requires_paired_VH_VL" in result["evidence_issues"]
    assert "incorrect_or_missing_unit" in str(result["evidence_issues"])
    evidence["scores"] = []
    assert assess_antibody_design_evidence(evidence, antibody_format="vh_vl")["evidence_issues"] == []


def test_unknown_gate_cannot_be_silently_ignored():
    evidence = passed_evidence()
    evidence["gates"]["custom_check"] = {"status": "fail", "evidence_ref": "synthetic"}
    result = assess_antibody_design_evidence(evidence)
    assert result["decision"] != "eligible_for_computational_prioritization"
    assert result["unrecognized_gates"] == ["custom_check"]


@pytest.mark.parametrize("bad", [False, "False", {"status": True}, {"status": "passed"}])
def test_malformed_gates_raise(bad):
    evidence = passed_evidence()
    evidence["gates"]["geometry"] = bad
    with pytest.raises(ValueError):
        assess_antibody_design_evidence(evidence)


def test_docs_load_without_candidate_dataset():
    registry = KnowledgeRegistry()
    for doc_id in METHOD_DOC_IDS:
        doc = registry.get_document_by_id(doc_id)
        assert doc and doc["description"] and doc["content_without_metadata"]
    # No original data files were imported as part of the method-only integration.
    cases = Path(__file__).resolve().parents[1] / "biochat/knowledge/cases/cd19"
    assert not any(p.is_file() for p in cases.rglob("*"))


@pytest.mark.parametrize("profile", ["minimal", "full"])
def test_method_tools_are_registered_and_importable(profile):
    from biochat.tool import antibody_design

    registry = ToolRegistry(load_all_tool_descriptions(profile=profile), profile=profile)
    for name in METHOD_TOOL_NAMES:
        assert registry.get_tool_by_name(name)
        assert callable(getattr(antibody_design, name))


@pytest.mark.parametrize("prompt,expected", [
    ("把 CD19 项目的方法用于新的 VH-only 设计", True),
    ("Design an antibody using partial diffusion", True),
    ("CD19 在 B 细胞中的表达", False),
    ("设计单细胞分析流程", False),
])
def test_retrieval_keeps_methods_for_design_only(prompt, expected):
    from biochat.agent.retrieval import retrieve_relevant_resources

    calls = []
    def select(*args, **kwargs):
        calls.append(args)
        return {"tools": [], "data_lake": [], "libraries": [], "know_how": []}

    agent = SimpleNamespace(
        use_tool_retriever=True, path="/tmp/unused-method-test", data_lake_dict={}, library_content_dict={},
        know_how_loader=KnowledgeRegistry(), llm=None,
        tool_registry=ToolRegistry(load_all_tool_descriptions(profile="minimal"), profile="minimal"),
        retriever=SimpleNamespace(prompt_based_retrieval=select),
    )
    result = retrieve_relevant_resources(agent, prompt)
    assert len(calls) == 1
    assert {d["id"] for d in result["know_how"]} == (set(METHOD_DOC_IDS) if expected else set())
    assert {t["name"] for t in result["tools"]} == (set(METHOD_TOOL_NAMES) if expected else set())


def test_prompt_builders_teach_method_route_and_score_semantics():
    from biochat.prompts.system_prompt import SystemPromptBuilder
    from biochat.prompts.system_prompt_v2 import SystemPromptBuilder as BuilderV2

    for builder in (SystemPromptBuilder, BuilderV2):
        text = builder(tool_desc={}, data_lake_content=[], library_content_list=[],
                       data_lake_path="", data_lake_dict={}, library_content_dict={}).build()
        assert "get_antibody_design_method" in text
        assert "assess_antibody_design_evidence" in text
        assert "matched comparison context" in text
