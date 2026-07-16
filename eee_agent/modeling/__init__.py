"""Strict modeling intent contracts and deterministic ChangeSet compiler."""

from eee_agent.modeling.compiler import (
    CompilationResult,
    ModelingCompileError,
    NodeCatalog,
    NodeTypeDefinition,
    ParmDefinition,
    WorkspaceBootstrapContext,
    compile_bootstrap_procedural_spec,
    compile_procedural_spec,
)
from eee_agent.modeling.catalog import (
    houdini_21_minimal_catalog,
    houdini_21_minimal_quality_profile,
)
from eee_agent.modeling.bootstrap import (
    BootstrapFinalizeError,
    derive_bootstrap_manifest,
)
from eee_agent.modeling.contracts import (
    Axis,
    BriefConstraint,
    ComponentSpec,
    FrontAxis,
    InputBinding,
    MAX_MODELING_JSON_BYTES,
    ModelingBrief,
    NodeSpec,
    ParmAssignment,
    ProceduralSpec,
    QualityProfile,
    RepairAttempt,
    RepairBudget,
    RepairStatus,
    RepairTicket,
    UnitSystem,
    ValidatorKind,
    parse_modeling_brief,
    parse_procedural_spec,
    parse_quality_profile,
    parse_repair_budget,
    parse_repair_ticket,
)
from eee_agent.modeling.validation import (
    EvidenceRef,
    ValidationReport,
    ValidationStatus,
    ValidatorResult,
    issue_repair_ticket,
    validate_applied_scene,
    validate_compilation,
    validate_scene_query,
)

__all__ = [
    "Axis",
    "BriefConstraint",
    "BootstrapFinalizeError",
    "CompilationResult",
    "ComponentSpec",
    "EvidenceRef",
    "FrontAxis",
    "InputBinding",
    "MAX_MODELING_JSON_BYTES",
    "ModelingBrief",
    "ModelingCompileError",
    "NodeCatalog",
    "NodeSpec",
    "NodeTypeDefinition",
    "ParmAssignment",
    "ParmDefinition",
    "ProceduralSpec",
    "QualityProfile",
    "RepairAttempt",
    "RepairBudget",
    "RepairStatus",
    "RepairTicket",
    "ValidationReport",
    "ValidationStatus",
    "ValidatorResult",
    "issue_repair_ticket",
    "validate_applied_scene",
    "UnitSystem",
    "ValidatorKind",
    "WorkspaceBootstrapContext",
    "compile_bootstrap_procedural_spec",
    "compile_procedural_spec",
    "derive_bootstrap_manifest",
    "houdini_21_minimal_catalog",
    "houdini_21_minimal_quality_profile",
    "parse_modeling_brief",
    "parse_procedural_spec",
    "parse_quality_profile",
    "parse_repair_budget",
    "parse_repair_ticket",
    "validate_compilation",
    "validate_scene_query",
    "ModelingProposalContext",
    "ModelingProposalCoordinator",
    "ModelingProposalError",
    "ModelingProposalSummary",
    "ModelingToolContext",
    "propose_modeling",
]

_PROPOSAL_EXPORTS = frozenset(
    {
        "ModelingProposalContext",
        "ModelingProposalCoordinator",
        "ModelingProposalError",
        "ModelingProposalSummary",
        "ModelingToolContext",
        "propose_modeling",
    }
)


def __getattr__(name: str):
    if name in _PROPOSAL_EXPORTS:
        from eee_agent.modeling import proposal

        return getattr(proposal, name)
    raise AttributeError(name)
