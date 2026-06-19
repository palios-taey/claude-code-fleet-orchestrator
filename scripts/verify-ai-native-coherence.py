#!/usr/bin/env python3
from __future__ import annotations

import argparse
import ast
import hashlib
import re
import shutil
import sys
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


ROOT = Path(__file__).resolve().parents[1]
REGISTRY = ROOT / "docs" / "ai_native_surface_audit.md"
START_MARKER = "<!-- ai-native-surfaces:start -->"
END_MARKER = "<!-- ai-native-surfaces:end -->"

IN_SCOPE_API_MODULES = (
    "fleet_orchestrator/tasks_api.py",
    "fleet_orchestrator/chat_layer.py",
    "fleet_orchestrator/public_readonly.py",
)
EXCLUDED_API_MODULES: dict[str, str] = {}
ORCH_SCHEMA = "fleet_orchestrator/orch_schema.py"
CONTEXT_ASSEMBLER = "fleet_orchestrator/context_assembler.py"
CLI_FILES = (
    "fleet_orchestrator/cli_taey_plan.py",
    "fleet_orchestrator/cli_taey_task.py",
)
ALLOWED_CLASSIFICATIONS = {"teaches", "needs-fix", "exempt"}
STRUCTURED_NEXT_STEP_KEYS = {"next_step", "next_action", "enable_with"}
ERROR_PAYLOAD_KEYS = {"error", "detail", "reason", *STRUCTURED_NEXT_STEP_KEYS}
ENDPOINT_RE = re.compile(r"\b(?P<method>GET|POST|PATCH)\s+(?P<path>/api/[A-Za-z0-9_./{}<>:-]+)")
CLI_TOKEN_RE = re.compile(r"\btaey-[a-z0-9-]+\b")
GENERIC_NEXT_STEP_RE = re.compile(r"^\s*(?:see docs|ask the operator|try again)\s*\.?\s*$", re.I)
EMPTY_CLI_MESSAGES = {
    "No projects.",
    "no in-progress work",
    "next: none",
    "No pending tasks.",
}
PATH_PLACEHOLDER_NAMES = {
    "project_id",
    "task_id",
    "phase_id",
    "condition_id",
    "question_id",
    "session_id",
    "loop_id",
    "lineage",
    "target",
}

# Frozen bootstrap debt from the registry generated in PR #171. New rows may not
# use needs-fix without changing this set in the same reviewed PR.
BASELINE_NEEDS_FIX_KEYS = frozenset({
    "fleet_orchestrator/chat_layer.py::_http_error::http_exception_detail::1::c76409965486615c",
    "fleet_orchestrator/cli_taey_dispatch.py::<module>::cli_failure_message::1::415f3a0a1ba97cb6",
    "fleet_orchestrator/cli_taey_dispatch.py::parse_json_object::cli_failure_message::1::f3a02b3922c82153",
    "fleet_orchestrator/cli_taey_dispatch.py::parse_json_object::cli_failure_message::2::881fd581dc03f92f",
    "fleet_orchestrator/cli_taey_plan.py::api_call::cli_failure_message::1::d6a4c7ed4db44ab6",
    "fleet_orchestrator/cli_taey_plan.py::api_call::cli_failure_message::2::ff10f2f9f802abb0",
    "fleet_orchestrator/cli_taey_plan.py::cmd_assign::cli_failure_message::1::131a1db00c12a6b2",
    "fleet_orchestrator/cli_taey_plan.py::cmd_current::cli_empty_state_message::1::ce2f7a0dad29b856",
    "fleet_orchestrator/cli_taey_plan.py::cmd_ingest::cli_failure_message::1::b77e13eb1600f2b2",
    "fleet_orchestrator/cli_taey_plan.py::cmd_list::cli_empty_state_message::1::9adab5ee4bb7e866",
    "fleet_orchestrator/cli_taey_plan.py::cmd_next::cli_empty_state_message::1::9910fa4c017cead1",
    "fleet_orchestrator/cli_taey_plan.py::main::cli_failure_message::1::6cacfcce8ba6174b",
    "fleet_orchestrator/cli_taey_question.py::<module>::cli_failure_message::1::415f3a0a1ba97cb6",
    "fleet_orchestrator/cli_taey_question.py::api_call::cli_failure_message::1::d6a4c7ed4db44ab6",
    "fleet_orchestrator/cli_taey_question.py::api_call::cli_failure_message::2::a0e828fd89855c39",
    "fleet_orchestrator/cli_taey_question.py::parse_refs::cli_failure_message::1::069d74193310e63b",
    "fleet_orchestrator/cli_taey_question.py::parse_refs::cli_failure_message::2::3b7ed5bf6dd6f46c",
    "fleet_orchestrator/cli_taey_receipts.py::<module>::cli_failure_message::1::25f0190aef36cb37",
    "fleet_orchestrator/cli_taey_receipts.py::_cmd_list::cli_failure_message::1::d599b3a9b207c556",
    "fleet_orchestrator/cli_taey_task.py::api_call::cli_failure_message::1::5babffc31c6083fc",
    "fleet_orchestrator/cli_taey_task.py::api_call::cli_failure_message::2::5d9dd2cd7ab34b26",
    "fleet_orchestrator/cli_taey_task.py::cmd_dispatch::cli_failure_message::1::252f70151008a700",
    "fleet_orchestrator/cli_taey_task.py::cmd_dispatch::cli_failure_message::2::f1e8f1505630373a",
    "fleet_orchestrator/cli_taey_task.py::cmd_dispatch::cli_failure_message::3::21caa5fe78c7fc9f",
    "fleet_orchestrator/cli_taey_task.py::cmd_list::cli_empty_state_message::1::fcad575660346a31",
    "fleet_orchestrator/cli_taey_task.py::cmd_status::cli_failure_message::1::15ac09e58ff7b290",
    "fleet_orchestrator/cli_taey_task.py::cmd_update::cli_failure_message::1::899355c6a0e4a5d7",
    "fleet_orchestrator/cli_taey_task.py::cmd_update::cli_failure_message::2::968a4d6cd4a87440",
    "fleet_orchestrator/cli_taey_task.py::cmd_update::cli_failure_message::3::0f1cd4262a1ee9a8",
    "fleet_orchestrator/cli_taey_task.py::parse_evidence_arg::cli_failure_message::1::25c98ca4c6f95876",
    "fleet_orchestrator/cli_taey_task.py::parse_evidence_arg::cli_failure_message::2::7c6e8e65b8cc161c",
    "fleet_orchestrator/context_assembler.py::_render_operating_section::wake_operating_line::11::cdd56b4136d1349d",
    "fleet_orchestrator/context_assembler.py::_render_operating_section::wake_operating_line::12::d69cc2959200b632",
    "fleet_orchestrator/context_assembler.py::_render_operating_section::wake_operating_line::14::19dc7c89c4cfeef2",
    "fleet_orchestrator/context_assembler.py::_render_operating_section::wake_operating_line::15::cb00754c4edf7b7c",
    "fleet_orchestrator/context_assembler.py::_render_operating_section::wake_operating_line::3::69572e26ef65f036",
    "fleet_orchestrator/context_assembler.py::_render_operating_section::wake_operating_line::4::a7d4a41ea27a2b2e",
    "fleet_orchestrator/context_assembler.py::_render_operating_section::wake_operating_line::5::4f42649b1ca1baa9",
    "fleet_orchestrator/context_assembler.py::_render_operating_section::wake_operating_line::6::0c709f4c646908ba",
    "fleet_orchestrator/context_assembler.py::_render_operating_section::wake_operating_line::9::c85185e9d57a2230",
    "fleet_orchestrator/orch_schema.py::_guard_creatable::orch_raise_error::1::d0ac52e08c8962f2",
    "fleet_orchestrator/orch_schema.py::_normalize_non_success_terminal_evidence::orch_raise_error::1::0fb7b9c4370be4ef",
    "fleet_orchestrator/orch_schema.py::_normalize_non_success_terminal_evidence::orch_raise_error::2::b0459814cfafae31",
    "fleet_orchestrator/orch_schema.py::_normalize_non_success_terminal_evidence::orch_raise_error::3::fe6fd38dd1ed3046",
    "fleet_orchestrator/orch_schema.py::_normalize_non_success_terminal_evidence::orch_raise_error::4::c253ff80cec9cf29",
    "fleet_orchestrator/orch_schema.py::_normalize_non_success_terminal_evidence::orch_raise_error::5::0fb7b9c4370be4ef",
    "fleet_orchestrator/orch_schema.py::_notify_human_review_gate::orch_raise_error::1::00d4b005ba9d04df",
    "fleet_orchestrator/orch_schema.py::_parse_pause_expires_at::orch_raise_error::1::b39343b9e78b052b",
    "fleet_orchestrator/orch_schema.py::_parse_pause_expires_at::orch_raise_error::2::1ea435cccd4861db",
    "fleet_orchestrator/orch_schema.py::_project_record::orch_raise_error::1::ad44addeff81d189",
    "fleet_orchestrator/orch_schema.py::_raw_stop_decision::orch_reason_return::10::bd3a6f4ac26fdd9b",
    "fleet_orchestrator/orch_schema.py::_raw_stop_decision::orch_reason_return::11::f9937e828712938a",
    "fleet_orchestrator/orch_schema.py::_raw_stop_decision::orch_reason_return::12::ed4f8b6dbb573032",
    "fleet_orchestrator/orch_schema.py::_raw_stop_decision::orch_reason_return::13::7f2b97e7d92af4d8",
    "fleet_orchestrator/orch_schema.py::_raw_stop_decision::orch_reason_return::14::22ee8dedee67466c",
    "fleet_orchestrator/orch_schema.py::_raw_stop_decision::orch_reason_return::15::f4884ddc69b2a50f",
    "fleet_orchestrator/orch_schema.py::_raw_stop_decision::orch_reason_return::16::f59c16e9fc3f185c",
    "fleet_orchestrator/orch_schema.py::_raw_stop_decision::orch_reason_return::17::22ee8dedee67466c",
    "fleet_orchestrator/orch_schema.py::_raw_stop_decision::orch_reason_return::1::22ee8dedee67466c",
    "fleet_orchestrator/orch_schema.py::_raw_stop_decision::orch_reason_return::3::d193e5d777fa6916",
    "fleet_orchestrator/orch_schema.py::_raw_stop_decision::orch_reason_return::4::d0851ceb845ab672",
    "fleet_orchestrator/orch_schema.py::_raw_stop_decision::orch_reason_return::5::1142af1f7c16e205",
    "fleet_orchestrator/orch_schema.py::_raw_stop_decision::orch_reason_return::7::f9937e828712938a",
    "fleet_orchestrator/orch_schema.py::_raw_stop_decision::orch_reason_return::8::ed4f8b6dbb573032",
    "fleet_orchestrator/orch_schema.py::_raw_stop_decision::orch_reason_return::9::351357a6bbb74caa",
    "fleet_orchestrator/orch_schema.py::_resolve_phase_project::orch_raise_error::1::6048e3fb90bd899f",
    "fleet_orchestrator/orch_schema.py::_resolve_phase_project::orch_raise_error::2::b77f179b8c83fc3e",
    "fleet_orchestrator/orch_schema.py::_send_wake::orch_raise_error::1::668ace3beff5b3ad",
    "fleet_orchestrator/orch_schema.py::_validate_terminal_status_write::orch_raise_error::1::59d1f863d6f9b70e",
    "fleet_orchestrator/orch_schema.py::_validate_terminal_status_write::orch_raise_error::3::b9226c7ac0abc971",
    "fleet_orchestrator/orch_schema.py::create_task::orch_raise_error::1::7f54b3d92ee6300a",
    "fleet_orchestrator/orch_schema.py::create_task::orch_raise_error::2::a0c81e66bc3ecc98",
    "fleet_orchestrator/orch_schema.py::resolve_ref_path::orch_return_none_error::1::169ae424dede9728",
    "fleet_orchestrator/orch_schema.py::resolve_ref_path::orch_return_none_error::2::1d4083e77d945e0c",
    "fleet_orchestrator/orch_schema.py::resolve_ref_path::orch_return_none_error::3::9d6856933337a22c",
    "fleet_orchestrator/orch_schema.py::resolve_ref_path::orch_return_none_error::4::9d6856933337a22c",
    "fleet_orchestrator/orch_schema.py::resolve_ref_path::orch_return_none_error::5::9d6856933337a22c",
    "fleet_orchestrator/orch_schema.py::resolve_ref_path::orch_return_none_error::6::280aac95441d2580",
    "fleet_orchestrator/orch_schema.py::resolve_ref_path::orch_return_none_error::7::9d6856933337a22c",
    "fleet_orchestrator/orch_schema.py::resolve_ref_path::orch_return_none_error::8::0e1f65290972361a",
    "fleet_orchestrator/orch_schema.py::set_project_stop_reason::orch_raise_error::1::56b412382c3a1675",
    "fleet_orchestrator/orch_schema.py::set_project_stop_reason::orch_raise_error::2::826b4a2776a027af",
    "fleet_orchestrator/orch_schema.py::set_project_stop_reason::orch_raise_error::3::c173af264a0f02dc",
    "fleet_orchestrator/orch_schema.py::set_session_pause::orch_raise_error::1::d3e0be4598fd09af",
    "fleet_orchestrator/orch_schema.py::set_session_pause::orch_raise_error::2::333a0224c8269efd",
    "fleet_orchestrator/orch_schema.py::set_supervisor_refs::orch_raise_error::1::e4efc41d4fcc04d3",
    "fleet_orchestrator/orch_schema.py::validate_source_path_for_refs::orch_return_none_error::1::2fa8eabf2359aa69",
    "fleet_orchestrator/orch_schema.py::validate_source_path_for_refs::orch_return_none_error::2::489823ea7b06396f",
    "fleet_orchestrator/orch_schema.py::validate_source_path_for_refs::orch_return_none_error::3::5e504430131fcf5e",
    "fleet_orchestrator/orch_schema.py::validate_source_path_for_refs::orch_return_none_error::4::aa8f71d24ad5ef6b",
    "fleet_orchestrator/orch_schema.py::validate_source_path_for_refs::orch_return_none_error::5::489823ea7b06396f",
    "fleet_orchestrator/orch_schema.py::validate_source_path_for_refs::orch_return_none_error::6::794a5041d3dcf60d",
    "fleet_orchestrator/public_readonly.py::_public_summary_or_404::http_exception_detail::1::e01225fda10e5dc0",
    "fleet_orchestrator/public_readonly.py::_public_summary_or_404::http_exception_detail::2::e01225fda10e5dc0",
    "fleet_orchestrator/public_readonly.py::_require_visible_session::http_exception_detail::1::458ac6f0f55b5967",
    "fleet_orchestrator/public_readonly.py::health::json_response_error::1::0f9cb10f6ef4b99f",
    "fleet_orchestrator/tasks_api.py::_ensure_registered_session::http_exception_detail::1::2e82c2d3e7d60d71",
    "fleet_orchestrator/tasks_api.py::_optional_mutable_auth::json_response_error::1::2f857139ba14bcbe",
    "fleet_orchestrator/tasks_api.py::_strict_force_flag::http_exception_detail::1::b35691f41936cbb7",
    "fleet_orchestrator/tasks_api.py::_validated_source_path::http_exception_detail::1::45872ce65bfabe35",
    "fleet_orchestrator/tasks_api.py::add_project_condition_endpoint::http_exception_detail::1::348b384465a1a6c1",
    "fleet_orchestrator/tasks_api.py::answer_question_endpoint::http_exception_detail::1::de4b91f77f815463",
    "fleet_orchestrator/tasks_api.py::answer_question_endpoint::json_response_error::2::a40ba0877882c1b9",
    "fleet_orchestrator/tasks_api.py::complete_project_endpoint::http_exception_detail::1::5a863880623fcdcd",
    "fleet_orchestrator/tasks_api.py::create::http_exception_detail::1::30d3d10afa6f0672",
    "fleet_orchestrator/tasks_api.py::create::http_exception_detail::2::46718850d351ec57",
    "fleet_orchestrator/tasks_api.py::create::http_exception_detail::3::c76409965486615c",
    "fleet_orchestrator/tasks_api.py::create::http_exception_detail::4::c76409965486615c",
    "fleet_orchestrator/tasks_api.py::create::http_exception_detail::5::c76409965486615c",
    "fleet_orchestrator/tasks_api.py::create_human_review_gate_endpoint::http_exception_detail::1::e4d3efaa60a80bd5",
    "fleet_orchestrator/tasks_api.py::create_human_review_gate_endpoint::http_exception_detail::2::1bd0f40eeef7e893",
    "fleet_orchestrator/tasks_api.py::create_human_review_gate_endpoint::http_exception_detail::3::a812cbdcc0d9a7cc",
    "fleet_orchestrator/tasks_api.py::create_human_review_gate_endpoint::json_response_error::2::a40ba0877882c1b9",
    "fleet_orchestrator/tasks_api.py::create_phase_endpoint::http_exception_detail::1::03cbe7a41214d45d",
    "fleet_orchestrator/tasks_api.py::create_phase_endpoint::http_exception_detail::2::c76409965486615c",
    "fleet_orchestrator/tasks_api.py::create_phase_endpoint::http_exception_detail::3::c76409965486615c",
    "fleet_orchestrator/tasks_api.py::create_project_endpoint::http_exception_detail::1::967f9eeb6599e5d2",
    "fleet_orchestrator/tasks_api.py::create_project_endpoint::http_exception_detail::2::93cd082293003940",
    "fleet_orchestrator/tasks_api.py::create_project_endpoint::http_exception_detail::3::96e995a1735b6464",
    "fleet_orchestrator/tasks_api.py::edit_project_condition_endpoint::http_exception_detail::1::f973b1c722145bc6",
    "fleet_orchestrator/tasks_api.py::edit_project_condition_endpoint::http_exception_detail::2::c76409965486615c",
    "fleet_orchestrator/tasks_api.py::load_plan_md::http_exception_detail::1::7d7613f0006ef1d4",
    "fleet_orchestrator/tasks_api.py::load_plan_md::http_exception_detail::2::fd7bdfb1c31ff91c",
    "fleet_orchestrator/tasks_api.py::load_plan_md::http_exception_detail::3::ba1cb964857ce16f",
    "fleet_orchestrator/tasks_api.py::load_plan_md::http_exception_detail::4::c76409965486615c",
    "fleet_orchestrator/tasks_api.py::load_plan_md::http_exception_detail::5::c76409965486615c",
    "fleet_orchestrator/tasks_api.py::loop_advance::http_exception_detail::1::f291de805d336ffd",
    "fleet_orchestrator/tasks_api.py::loop_advance::http_exception_detail::2::68be0b3eb9cc356c",
    "fleet_orchestrator/tasks_api.py::loop_advance::http_exception_detail::3::c76409965486615c",
    "fleet_orchestrator/tasks_api.py::loop_advance::http_exception_detail::5::c76409965486615c",
    "fleet_orchestrator/tasks_api.py::loop_declare::http_exception_detail::2::c76409965486615c",
    "fleet_orchestrator/tasks_api.py::loop_should_stop::http_exception_detail::1::68be0b3eb9cc356c",
    "fleet_orchestrator/tasks_api.py::patch_project_endpoint::http_exception_detail::1::42f4e7d6f844b5ac",
    "fleet_orchestrator/tasks_api.py::patch_project_endpoint::http_exception_detail::2::c76409965486615c",
    "fleet_orchestrator/tasks_api.py::pause_session_endpoint::http_exception_detail::1::c76409965486615c",
    "fleet_orchestrator/tasks_api.py::session_notify::http_exception_detail::1::10789411811a64cf",
    "fleet_orchestrator/tasks_api.py::session_notify::http_exception_detail::3::d235f23d0cd8ae89",
    "fleet_orchestrator/tasks_api.py::session_wake_packet::http_exception_detail::1::a36559bf1fdc83bb",
    "fleet_orchestrator/tasks_api.py::session_wake_packet::http_exception_detail::2::c3b52d7c1280774e",
    "fleet_orchestrator/tasks_api.py::set_project_stop_reason_endpoint::http_exception_detail::1::c76409965486615c",
    "fleet_orchestrator/tasks_api.py::set_project_stop_reason_endpoint::http_exception_detail::2::c76409965486615c",
    "fleet_orchestrator/tasks_api.py::set_project_user_stop_conditions_endpoint::http_exception_detail::1::6c6df5e668fdb3b2",
    "fleet_orchestrator/tasks_api.py::ship_project_endpoint::http_exception_detail::1::2e76fb50cb7518b3",
    "fleet_orchestrator/tasks_api.py::ui_answer_human_review_gate_endpoint::http_exception_detail::1::ccbb342a67fa7d9a",
    "fleet_orchestrator/tasks_api.py::ui_answer_human_review_gate_endpoint::http_exception_detail::2::06038769011515f1",
    "fleet_orchestrator/tasks_api.py::ui_answer_human_review_gate_endpoint::json_response_error::2::a40ba0877882c1b9",
    "fleet_orchestrator/tasks_api.py::update::json_response_error::1::d6c61be575531467",
    "fleet_orchestrator/tasks_api.py::update::json_response_error::3::ace60ea8e3e7a0f5",
})


@dataclass(frozen=True)
class SourceContext:
    source: str
    tree: ast.AST
    parents: dict[ast.AST, ast.AST]
    constants: dict[str, ast.AST]
    helpers: dict[str, ast.AST]


@dataclass(frozen=True)
class TextInfo:
    text: str
    dynamic: bool


@dataclass(frozen=True)
class TeachingResult:
    teaches: bool
    evidence: tuple[str, ...]
    errors: tuple[str, ...]


@dataclass(frozen=True)
class Surface:
    file: str
    function: str
    kind: str
    ordinal: int
    line: int
    column: int
    fingerprint: str
    text: str
    dynamic: bool
    source: str

    @property
    def key(self) -> tuple[str, str, str, int]:
        return (self.file, self.function, self.kind, self.ordinal)

    @property
    def baseline_key(self) -> str:
        return "::".join((self.file, self.function, self.kind, str(self.ordinal), self.fingerprint))

    @property
    def label(self) -> str:
        return f"{self.file}:{self.line} {self.function} {self.kind}#{self.ordinal}"


@dataclass(frozen=True)
class RegistryEntry:
    file: str
    function: str
    kind: str
    ordinal: int
    line_hint: int
    fingerprint: str
    classification: str
    teaching_evidence: str
    rationale: str
    review: str

    @property
    def key(self) -> tuple[str, str, str, int]:
        return (self.file, self.function, self.kind, self.ordinal)

    @property
    def baseline_key(self) -> str:
        return "::".join((self.file, self.function, self.kind, str(self.ordinal), self.fingerprint))

    @property
    def label(self) -> str:
        return f"{self.file}:{self.line_hint} {self.function} {self.kind}#{self.ordinal}"


def _parents(tree: ast.AST) -> dict[ast.AST, ast.AST]:
    result: dict[ast.AST, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            result[child] = node
    return result


def _function_path(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> str:
    names: list[str] = []
    parent = parents.get(node)
    while parent is not None:
        if isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.append(parent.name)
        parent = parents.get(parent)
    return ".".join(reversed(names)) or "<module>"


def _call_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _call_name(node.value)
        return f"{base}.{node.attr}" if base else node.attr
    if isinstance(node, ast.Call):
        return _call_name(node.func)
    return ""


def _literal_value(node: ast.AST | None) -> object:
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        value = _literal_value(node.operand)
        if isinstance(value, (int, float)):
            return -value
    return None


def _module_context(root: Path, file: str) -> SourceContext:
    path = root / file
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=file)
    parents = _parents(tree)
    constants: dict[str, ast.AST] = {}
    helpers: dict[str, ast.AST] = {}
    for node in getattr(tree, "body", []):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    constants[target.id] = node.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.value is not None:
            constants[node.target.id] = node.value
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            helpers[node.name] = node
    return SourceContext(source=source, tree=tree, parents=parents, constants=constants, helpers=helpers)


def _resolve_name(context: SourceContext, name: str, seen: set[str]) -> ast.AST | None:
    if name in seen:
        return None
    value = context.constants.get(name)
    if value is None:
        return None
    seen.add(name)
    return value


def _return_values(function: ast.AST) -> list[ast.AST]:
    values: list[ast.AST] = []
    for node in ast.walk(function):
        if isinstance(node, ast.Return) and node.value is not None:
            values.append(node.value)
    return values


def _text_from_expr(node: ast.AST | None, context: SourceContext, seen_names: set[str] | None = None) -> TextInfo:
    if node is None:
        return TextInfo("", True)
    seen = set(seen_names or set())
    if isinstance(node, ast.Constant):
        if isinstance(node.value, str):
            return TextInfo(node.value, False)
        return TextInfo(repr(node.value), False)
    if isinstance(node, ast.Name):
        resolved = _resolve_name(context, node.id, seen)
        if resolved is not None:
            return _text_from_expr(resolved, context, seen)
        return TextInfo(node.id, True)
    if isinstance(node, ast.JoinedStr):
        chunks: list[str] = []
        dynamic = False
        for value in node.values:
            if isinstance(value, ast.Constant):
                chunks.append(str(value.value))
            elif isinstance(value, ast.FormattedValue):
                item = _text_from_expr(value.value, context, seen)
                chunks.append(item.text)
                dynamic = dynamic or item.dynamic
            else:
                dynamic = True
        return TextInfo("".join(chunks), dynamic)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = _text_from_expr(node.left, context, seen)
        right = _text_from_expr(node.right, context, seen)
        return TextInfo(left.text + right.text, left.dynamic or right.dynamic)
    if isinstance(node, ast.Dict):
        parts: list[str] = []
        dynamic = False
        for key, value in zip(node.keys, node.values):
            key_info = _text_from_expr(key, context, seen)
            value_info = _text_from_expr(value, context, seen)
            parts.append(f"{key_info.text}: {value_info.text}")
            dynamic = dynamic or key_info.dynamic or value_info.dynamic
        return TextInfo("; ".join(parts), dynamic)
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        items = [_text_from_expr(item, context, seen) for item in node.elts]
        return TextInfo("; ".join(item.text for item in items), any(item.dynamic for item in items))
    if isinstance(node, ast.Call):
        name = _call_name(node.func).split(".")[-1]
        helper = context.helpers.get(name)
        if helper is not None:
            returns = _return_values(helper)
            if len(returns) == 1:
                resolved = _text_from_expr(returns[0], context, seen)
                return TextInfo(resolved.text, True)
        literal_parts = [_text_from_expr(arg, context, seen) for arg in node.args]
        for keyword in node.keywords:
            if keyword.arg:
                value = _text_from_expr(keyword.value, context, seen)
                literal_parts.append(TextInfo(f"{keyword.arg}: {value.text}", value.dynamic))
        text = " ".join(part.text for part in literal_parts if part.text)
        if text:
            return TextInfo(f"{name} {text}", True)
        return TextInfo(name, True)
    try:
        return TextInfo(ast.unparse(node), True)
    except Exception:
        return TextInfo(type(node).__name__, True)


def _callee_names(node: ast.AST | None) -> list[str]:
    if node is None:
        return []
    names: list[str] = []
    for child in ast.walk(node):
        if isinstance(child, ast.Call):
            name = _call_name(child.func)
            if name:
                names.append(name)
    return names


def _fingerprint(kind: str, expr: ast.AST | None, text: str) -> str:
    dumped = ast.dump(expr, include_attributes=False) if expr is not None else "<none>"
    payload = "\n".join((kind, dumped, text, " ".join(sorted(_callee_names(expr)))))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _keyword_value(call: ast.Call, name: str) -> ast.AST | None:
    for keyword in call.keywords:
        if keyword.arg == name:
            return keyword.value
    return None


def _http_exception_detail(call: ast.Call) -> ast.AST | None:
    detail = _keyword_value(call, "detail")
    if detail is not None:
        return detail
    if len(call.args) >= 2:
        return call.args[1]
    return None


def _json_response_payload(call: ast.Call) -> ast.AST | None:
    content = _keyword_value(call, "content")
    if content is not None:
        return content
    if call.args:
        return call.args[0]
    return None


def _json_response_status(call: ast.Call) -> int | None:
    status = _keyword_value(call, "status_code")
    value = _literal_value(status)
    if isinstance(value, int):
        return value
    if call.args:
        value = _literal_value(call.args[0])
        if isinstance(value, int):
            return value
    return None


def _dict_has_error_key(node: ast.AST | None) -> bool:
    if isinstance(node, ast.Dict):
        for key in node.keys:
            if isinstance(key, ast.Constant) and isinstance(key.value, str) and key.value in ERROR_PAYLOAD_KEYS:
                return True
    return False


def _is_http_exception_call(node: ast.AST) -> bool:
    return isinstance(node, ast.Call) and _call_name(node.func).split(".")[-1] == "HTTPException"


def _is_json_response_call(node: ast.AST) -> bool:
    return isinstance(node, ast.Call) and _call_name(node.func).split(".")[-1] == "JSONResponse"


def _is_route_decorator(node: ast.AST) -> bool:
    if not isinstance(node, ast.Call):
        return False
    if not isinstance(node.func, ast.Attribute):
        return False
    return node.func.attr in {"get", "post", "patch", "put", "delete", "api_route"}


def _module_guard_reasons(root: Path, file: str) -> list[str]:
    context = _module_context(root, file)
    reasons: set[str] = set()
    for node in ast.walk(context.tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported = {alias.name for alias in node.names}
            if "HTTPException" in imported:
                reasons.add("HTTPException")
            if "JSONResponse" in imported:
                reasons.add("JSONResponse")
            if "FastAPI" in imported:
                reasons.add("FastAPI")
            if "APIRouter" in imported:
                reasons.add("APIRouter")
        elif _is_http_exception_call(node):
            reasons.add("HTTPException")
        elif _is_json_response_call(node):
            status = _json_response_status(node)
            if status is not None and status >= 400:
                reasons.add("JSONResponse>=400")
        elif isinstance(node, ast.Call) and _call_name(node.func).split(".")[-1] in {"FastAPI", "APIRouter"}:
            reasons.add(_call_name(node.func).split(".")[-1])
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if any(_is_route_decorator(decorator) for decorator in node.decorator_list):
                reasons.add("route")
    return sorted(reasons)


def api_module_classification_errors(
    root: Path,
    *,
    in_scope_api_modules: Iterable[str] = IN_SCOPE_API_MODULES,
    excluded_api_modules: dict[str, str] | None = None,
) -> list[str]:
    in_scope = set(in_scope_api_modules)
    excluded = excluded_api_modules or EXCLUDED_API_MODULES
    errors: list[str] = []
    for path in sorted((root / "fleet_orchestrator").glob("*.py")):
        file = path.relative_to(root).as_posix()
        reasons = _module_guard_reasons(root, file)
        if not reasons:
            continue
        if file in in_scope:
            continue
        rationale = excluded.get(file, "")
        if rationale:
            continue
        errors.append(f"{file} has API/error sink markers ({', '.join(reasons)}) but is not classified in scope or excluded")
    return errors


def _raw_surfaces_for_api_module(root: Path, file: str) -> list[tuple[str, int, int, str, str, ast.AST | None, str, bool]]:
    context = _module_context(root, file)
    raw: list[tuple[str, int, int, str, str, ast.AST | None, str, bool]] = []
    for node in ast.walk(context.tree):
        if _is_http_exception_call(node):
            expr = _http_exception_detail(node)
            text_info = _text_from_expr(expr, context)
            raw.append(
                (
                    file,
                    node.lineno,
                    node.col_offset,
                    _function_path(node, context.parents),
                    "http_exception_detail",
                    expr,
                    text_info.text,
                    text_info.dynamic,
                )
            )
        elif _is_json_response_call(node):
            status = _json_response_status(node)
            payload = _json_response_payload(node)
            if (status is not None and status >= 400) or _dict_has_error_key(payload):
                text_info = _text_from_expr(payload, context)
                raw.append(
                    (
                        file,
                        node.lineno,
                        node.col_offset,
                        _function_path(node, context.parents),
                        "json_response_error",
                        payload,
                        text_info.text,
                        text_info.dynamic,
                    )
                )
    return raw


def _is_none_tuple_return(node: ast.Return) -> bool:
    value = node.value
    if not isinstance(value, (ast.Tuple, ast.List)) or len(value.elts) < 2:
        return False
    return isinstance(value.elts[0], ast.Constant) and value.elts[0].value is None


def _message_from_raise(node: ast.Raise) -> ast.AST | None:
    exc = node.exc
    if isinstance(exc, ast.Call):
        if exc.args:
            return exc.args[0]
        return _keyword_value(exc, "message")
    return exc


def _stop_reason_function(function: str) -> bool:
    tail = function.split(".")[-1]
    return (
        tail.endswith("_block_reason")
        or tail.endswith("_stop_reason")
        or tail in {"_raw_stop_decision", "get_session_stop_decision", "get_session_stop_status"}
    )


def _stop_payload_candidate(node: ast.AST | None) -> bool:
    if isinstance(node, ast.Dict):
        for key in node.keys:
            if isinstance(key, ast.Constant) and key.value in {"reason", "next_action", "next_step", "block_reason", "wake_reason", "detail"}:
                return True
    return isinstance(node, ast.Constant) and isinstance(node.value, str)


def _raw_surfaces_for_orch_schema(root: Path) -> list[tuple[str, int, int, str, str, ast.AST | None, str, bool]]:
    file = ORCH_SCHEMA
    context = _module_context(root, file)
    raw: list[tuple[str, int, int, str, str, ast.AST | None, str, bool]] = []
    for node in ast.walk(context.tree):
        if isinstance(node, ast.Return) and _is_none_tuple_return(node):
            assert isinstance(node.value, (ast.Tuple, ast.List))
            expr = node.value.elts[1]
            text_info = _text_from_expr(expr, context)
            raw.append((file, node.lineno, node.col_offset, _function_path(node, context.parents), "orch_return_none_error", expr, text_info.text, text_info.dynamic))
        elif isinstance(node, ast.Raise):
            expr = _message_from_raise(node)
            text_info = _text_from_expr(expr, context)
            raw.append((file, node.lineno, node.col_offset, _function_path(node, context.parents), "orch_raise_error", expr, text_info.text, text_info.dynamic))
        elif isinstance(node, ast.Return):
            function = _function_path(node, context.parents)
            if _stop_reason_function(function) and _stop_payload_candidate(node.value):
                text_info = _text_from_expr(node.value, context)
                raw.append((file, node.lineno, node.col_offset, function, "orch_reason_return", node.value, text_info.text, text_info.dynamic))
    return raw


def _raw_surfaces_for_operating_section(root: Path) -> list[tuple[str, int, int, str, str, ast.AST | None, str, bool]]:
    file = CONTEXT_ASSEMBLER
    context = _module_context(root, file)
    raw: list[tuple[str, int, int, str, str, ast.AST | None, str, bool]] = []
    operating_function: ast.AST | None = None
    for node in ast.walk(context.tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "_render_operating_section":
            operating_function = node
            break
    local_lists: dict[str, ast.List] = {}
    if operating_function is not None:
        for node in ast.walk(operating_function):
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.List):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        local_lists[target.id] = node.value

    def list_items(expr: ast.AST | None) -> list[ast.AST]:
        if isinstance(expr, ast.List):
            return list(expr.elts)
        if isinstance(expr, ast.Name):
            return list_items(local_lists.get(expr.id))
        if isinstance(expr, ast.BinOp) and isinstance(expr.op, ast.Add):
            return list_items(expr.left) + list_items(expr.right)
        return []

    if operating_function is not None:
        for node in ast.walk(operating_function):
            if not isinstance(node, ast.Return):
                continue
            for item in list_items(node.value):
                text_info = _text_from_expr(item, context)
                raw.append((file, item.lineno, item.col_offset, "_render_operating_section", "wake_operating_line", item, text_info.text, text_info.dynamic))
    for node in ast.walk(context.tree):
        if not isinstance(node, ast.Call):
            continue
        if _function_path(node, context.parents) != "_render_operating_section":
            continue
        if not isinstance(node.func, ast.Attribute):
            continue
        if not isinstance(node.func.value, ast.Name) or node.func.value.id != "lines":
            continue
        if node.func.attr == "append" and node.args:
            expr = node.args[0]
            text_info = _text_from_expr(expr, context)
            raw.append((file, node.lineno, node.col_offset, "_render_operating_section", "wake_operating_line", expr, text_info.text, text_info.dynamic))
        elif node.func.attr == "extend" and node.args:
            expr = node.args[0]
            if isinstance(expr, ast.List):
                for item in expr.elts:
                    text_info = _text_from_expr(item, context)
                    raw.append((file, item.lineno, item.col_offset, "_render_operating_section", "wake_operating_line", item, text_info.text, text_info.dynamic))
            else:
                text_info = _text_from_expr(expr, context)
                raw.append((file, node.lineno, node.col_offset, "_render_operating_section", "wake_operating_line", expr, text_info.text, text_info.dynamic))
    return raw


def _is_stderr_print(call: ast.Call) -> bool:
    if _call_name(call.func) != "print":
        return False
    for keyword in call.keywords:
        if keyword.arg == "file" and _call_name(keyword.value) == "sys.stderr":
            return True
    return False


def _is_parser_error(call: ast.Call) -> bool:
    return _call_name(call.func).endswith(".error")


def _is_system_exit(call: ast.Call) -> bool:
    return _call_name(call.func).split(".")[-1] == "SystemExit"


def _raw_surfaces_for_cli(root: Path, cli_files: Iterable[str] = CLI_FILES) -> list[tuple[str, int, int, str, str, ast.AST | None, str, bool]]:
    files = set(cli_files)
    cli_dir = root / "fleet_orchestrator"
    if cli_dir.exists():
        for path in cli_dir.glob("cli_taey_*.py"):
            files.add(path.relative_to(root).as_posix())
    raw: list[tuple[str, int, int, str, str, ast.AST | None, str, bool]] = []
    for file in sorted(files):
        if not (root / file).exists():
            continue
        context = _module_context(root, file)
        for node in ast.walk(context.tree):
            if not isinstance(node, ast.Call):
                continue
            expr: ast.AST | None = None
            kind = "cli_failure_message"
            if _is_stderr_print(node) and node.args:
                expr = node.args[0]
            elif _is_system_exit(node) and node.args:
                expr = node.args[0]
            elif _is_parser_error(node) and node.args:
                expr = node.args[0]
            elif _call_name(node.func) == "print" and node.args:
                text_info = _text_from_expr(node.args[0], context)
                if text_info.text not in EMPTY_CLI_MESSAGES:
                    continue
                expr = node.args[0]
                kind = "cli_empty_state_message"
            if expr is None:
                continue
            text_info = _text_from_expr(expr, context)
            raw.append((file, node.lineno, node.col_offset, _function_path(node, context.parents), kind, expr, text_info.text, text_info.dynamic))
    return raw


def _surface_from_raw(raw: tuple[str, int, int, str, str, ast.AST | None, str, bool], ordinal: int) -> Surface:
    file, line, column, function, kind, expr, text, dynamic = raw
    return Surface(
        file=file,
        function=function,
        kind=kind,
        ordinal=ordinal,
        line=line,
        column=column,
        fingerprint=_fingerprint(kind, expr, text),
        text=" ".join(text.split()),
        dynamic=dynamic,
        source=ast.unparse(expr) if expr is not None else "",
    )


def discover_surfaces(
    root: Path = ROOT,
    *,
    in_scope_api_modules: Iterable[str] = IN_SCOPE_API_MODULES,
    cli_files: Iterable[str] = CLI_FILES,
) -> list[Surface]:
    raw: list[tuple[str, int, int, str, str, ast.AST | None, str, bool]] = []
    for file in in_scope_api_modules:
        if not (root / file).exists():
            continue
        for item in _raw_surfaces_for_api_module(root, file):
            raw.append(item)
    if (root / ORCH_SCHEMA).exists():
        raw.extend(_raw_surfaces_for_orch_schema(root))
    if (root / CONTEXT_ASSEMBLER).exists():
        raw.extend(_raw_surfaces_for_operating_section(root))
    raw.extend(_raw_surfaces_for_cli(root, cli_files))
    raw = sorted(raw, key=lambda item: (item[0], item[3], item[4], item[1], item[2]))
    ordinals: defaultdict[tuple[str, str, str], int] = defaultdict(int)
    surfaces: list[Surface] = []
    for item in raw:
        group = (item[0], item[3], item[4])
        ordinals[group] += 1
        surfaces.append(_surface_from_raw(item, ordinals[group]))
    return sorted(surfaces, key=lambda item: (item.file, item.line, item.column, item.kind))


def _route_prefixes(tree: ast.AST) -> dict[str, str]:
    prefixes: dict[str, str] = {"app": ""}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        if not isinstance(node.value, ast.Call) or _call_name(node.value.func).split(".")[-1] != "APIRouter":
            continue
        prefix_node = _keyword_value(node.value, "prefix")
        prefix = _literal_value(prefix_node)
        if not isinstance(prefix, str):
            prefix = ""
        for target in node.targets:
            if isinstance(target, ast.Name):
                prefixes[target.id] = prefix
    return prefixes


def discover_routes(root: Path = ROOT) -> set[tuple[str, str]]:
    routes: set[tuple[str, str]] = set()
    api_dir = root / "fleet_orchestrator"
    if not api_dir.exists():
        return routes
    for path in sorted(api_dir.glob("*.py")):
        file = path.relative_to(root).as_posix()
        context = _module_context(root, file)
        prefixes = _route_prefixes(context.tree)
        for node in ast.walk(context.tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for decorator in node.decorator_list:
                if not _is_route_decorator(decorator):
                    continue
                assert isinstance(decorator, ast.Call)
                assert isinstance(decorator.func, ast.Attribute)
                method_name = decorator.func.attr
                owner = _call_name(decorator.func.value)
                prefix = prefixes.get(owner, "")
                path_node = decorator.args[0] if decorator.args else _keyword_value(decorator, "path")
                route_path = _literal_value(path_node)
                if not isinstance(route_path, str):
                    continue
                if method_name == "api_route":
                    methods_node = _keyword_value(decorator, "methods")
                    methods: list[str] = []
                    if isinstance(methods_node, (ast.List, ast.Tuple)):
                        for item in methods_node.elts:
                            value = _literal_value(item)
                            if isinstance(value, str):
                                methods.append(value.upper())
                    if not methods:
                        methods = ["GET"]
                else:
                    methods = [method_name.upper()]
                full_path = _join_route_path(prefix, route_path)
                for method in methods:
                    routes.add((method, _normalize_endpoint_path(full_path)))
    return routes


def _join_route_path(prefix: str, path: str) -> str:
    if not prefix:
        return path
    if path == "/":
        return prefix
    return prefix.rstrip("/") + "/" + path.lstrip("/")


def _normalize_endpoint_path(path: str) -> str:
    cleaned = path.strip("`'\".,;)")
    parts: list[str] = []
    for part in cleaned.split("/"):
        if not part:
            continue
        if (part.startswith("{") and part.endswith("}")) or (part.startswith("<") and part.endswith(">")):
            parts.append("{}")
        elif part in PATH_PLACEHOLDER_NAMES:
            parts.append("{}")
        else:
            parts.append(part)
    return "/" + "/".join(parts)


def _repo_cli_commands(root: Path = ROOT) -> set[str]:
    commands: set[str] = set()
    setup_py = root / "setup.py"
    if setup_py.exists():
        source = setup_py.read_text(encoding="utf-8")
        commands.update(re.findall(r"['\"](taey-[a-z0-9-]+)\s*=", source))
    scripts_dir = root / "scripts"
    if scripts_dir.exists():
        for path in scripts_dir.iterdir():
            if path.name.startswith("taey-"):
                commands.add(path.name)
    for command in ("taey-notify", "taey-stop-reason", "taey-ack", "taey-handoff", "taey-trace"):
        if shutil.which(command) or command == "taey-notify":
            commands.add(command)
    return commands


def validate_teaching_text(text: str, *, routes: set[tuple[str, str]], commands: set[str]) -> TeachingResult:
    evidence: list[str] = []
    errors: list[str] = []
    for command in sorted(set(CLI_TOKEN_RE.findall(text))):
        if command not in commands:
            errors.append(f"unknown CLI command {command!r}")
        else:
            evidence.append(command)
    for match in ENDPOINT_RE.finditer(text):
        method = match.group("method")
        path = _normalize_endpoint_path(match.group("path"))
        if (method, path) not in routes:
            errors.append(f"unknown API endpoint {method} {match.group('path')!r}")
        else:
            evidence.append(f"{method} {path}")
    for key in STRUCTURED_NEXT_STEP_KEYS:
        if re.search(rf"\b{re.escape(key)}\b", text):
            if not GENERIC_NEXT_STEP_RE.fullmatch(text):
                evidence.append(key)
    return TeachingResult(teaches=bool(evidence) and not errors, evidence=tuple(dict.fromkeys(evidence)), errors=tuple(errors))


def _split_markdown_row(line: str) -> list[str]:
    raw = line.strip()
    if not raw.startswith("|") or not raw.endswith("|"):
        return []
    cells: list[str] = []
    current: list[str] = []
    escaped = False
    for char in raw[1:-1]:
        if escaped:
            current.append(char)
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == "|":
            cells.append("".join(current).strip())
            current = []
        else:
            current.append(char)
    cells.append("".join(current).strip())
    return cells


def parse_registry(path: Path = REGISTRY) -> list[RegistryEntry]:
    text = path.read_text(encoding="utf-8")
    if START_MARKER not in text or END_MARKER not in text:
        raise ValueError(f"{path} is missing {START_MARKER} / {END_MARKER} markers")
    body = text.split(START_MARKER, 1)[1].split(END_MARKER, 1)[0]
    entries: list[RegistryEntry] = []
    for raw_line in body.splitlines():
        line = raw_line.strip()
        if not line.startswith("| fleet_orchestrator/"):
            continue
        cells = _split_markdown_row(line)
        if len(cells) != 10:
            raise ValueError(f"registry row must have 10 cells: {raw_line}")
        file, function, kind, ordinal, line_hint, fingerprint, classification, evidence, rationale, review = cells
        entries.append(
            RegistryEntry(
                file=file,
                function=function,
                kind=kind,
                ordinal=int(ordinal),
                line_hint=int(line_hint),
                fingerprint=fingerprint,
                classification=classification,
                teaching_evidence=evidence,
                rationale=rationale,
                review=review,
            )
        )
    return entries


def check(
    root: Path = ROOT,
    *,
    registry_path: Path | None = None,
    in_scope_api_modules: Iterable[str] = IN_SCOPE_API_MODULES,
    excluded_api_modules: dict[str, str] | None = None,
    cli_files: Iterable[str] = CLI_FILES,
    baseline_needs_fix_keys: frozenset[str] = BASELINE_NEEDS_FIX_KEYS,
) -> list[str]:
    registry = registry_path or (root / "docs" / "ai_native_surface_audit.md")
    errors = api_module_classification_errors(
        root,
        in_scope_api_modules=in_scope_api_modules,
        excluded_api_modules=excluded_api_modules,
    )
    surfaces = discover_surfaces(root, in_scope_api_modules=in_scope_api_modules, cli_files=cli_files)
    entries = parse_registry(registry)
    routes = discover_routes(root)
    commands = _repo_cli_commands(root)
    surface_by_key = {surface.key: surface for surface in surfaces}
    entry_by_key: dict[tuple[str, str, str, int], RegistryEntry] = {}
    for entry in entries:
        if entry.classification not in ALLOWED_CLASSIFICATIONS:
            errors.append(f"{entry.label} has invalid classification {entry.classification!r}")
        if entry.key in entry_by_key:
            errors.append(f"{entry.label} duplicates registry entry")
        entry_by_key[entry.key] = entry
    for surface in surfaces:
        if surface.key not in entry_by_key:
            errors.append(f"{surface.label} is missing from AI-native registry")
    for entry in entries:
        surface = surface_by_key.get(entry.key)
        if surface is None:
            errors.append(f"{entry.label} is registered but no matching surface exists")
            continue
        if entry.fingerprint != surface.fingerprint:
            errors.append(
                f"{entry.label} fingerprint mismatch: registry {entry.fingerprint}, code {surface.fingerprint}"
            )
        if entry.classification == "exempt" and not entry.rationale:
            errors.append(f"{entry.label} exempt row missing rationale")
        if entry.classification == "needs-fix":
            if not entry.review:
                errors.append(f"{entry.label} needs-fix row missing review marker")
            if entry.baseline_key not in baseline_needs_fix_keys:
                errors.append(f"{entry.label} adds non-baseline needs-fix debt")
        if entry.classification == "teaches":
            result = validate_teaching_text(surface.text, routes=routes, commands=commands)
            if surface.dynamic and not result.teaches and entry.teaching_evidence:
                result = validate_teaching_text(entry.teaching_evidence, routes=routes, commands=commands)
            if not result.teaches:
                detail = "; ".join(result.errors) if result.errors else "no real CLI, API endpoint, or structured next-step field"
                errors.append(f"{entry.label} is classified teaches but {detail}")
            elif result.errors:
                errors.append(f"{entry.label} has invalid teaching evidence: {'; '.join(result.errors)}")
        if entry.teaching_evidence:
            evidence_result = validate_teaching_text(entry.teaching_evidence, routes=routes, commands=commands)
            if evidence_result.errors:
                errors.append(f"{entry.label} teaching evidence invalid: {'; '.join(evidence_result.errors)}")
        if not entry.rationale:
            errors.append(f"{entry.label} missing rationale")
    return errors


def _cell(value: object) -> str:
    return str(value).replace("\\", "\\\\").replace("|", "\\|").replace("\n", " ").strip()


def _registry_rows(root: Path = ROOT, surfaces: list[Surface] | None = None) -> tuple[list[str], int, int]:
    discovered = surfaces if surfaces is not None else discover_surfaces(root)
    routes = discover_routes(root)
    commands = _repo_cli_commands(root)
    rows = [
        "| File | Function | Kind | Ordinal | Line Hint | Fingerprint | Classification | Teaching Evidence | Rationale | Review |",
        "| --- | --- | --- | ---: | ---: | --- | --- | --- | --- | --- |",
    ]
    teaches = 0
    needs_fix = 0
    for surface in sorted(discovered, key=lambda item: (item.file, item.function, item.kind, item.ordinal)):
        result = validate_teaching_text(surface.text, routes=routes, commands=commands)
        if result.teaches:
            classification = "teaches"
            evidence = result.evidence[0] if result.evidence else ""
            rationale = "Static teaching assertion passes."
            teaches += 1
        else:
            classification = "needs-fix"
            evidence = ""
            if result.errors:
                rationale = "Baseline non-teaching debt with invalid teaching token: " + "; ".join(result.errors)
            elif surface.dynamic:
                rationale = "Baseline dynamic surface not statically proven to teach; follow-up must add explicit in-band next step or reviewed evidence."
            else:
                rationale = "Baseline non-teaching surface; follow-up must add explicit in-band next step."
            needs_fix += 1
        rows.append(
            "| "
            + " | ".join(
                _cell(value)
                for value in (
                    surface.file,
                    surface.function,
                    surface.kind,
                    surface.ordinal,
                    surface.line,
                    surface.fingerprint,
                    classification,
                    evidence,
                    rationale,
                    "baseline-pr171",
                )
            )
            + " |"
        )
    return rows, teaches, needs_fix


def write_registry(root: Path = ROOT, path: Path = REGISTRY) -> tuple[int, int, int]:
    surfaces = discover_surfaces(root)
    rows, teaches, needs_fix = _registry_rows(root, surfaces)
    content = "\n".join(
        [
            "# AI-Native Surface Audit",
            "",
            "Status: machine-checkable registry. The verifier owns completeness; line hints are diagnostics only.",
            "",
            "Principle under test: an AI with no surrounding chat context should be able to read the emitted error, rejection, wake state, or CLI output and know what it has, why it is blocked or ready, and what to do next.",
            "",
            "Registry rules:",
            "",
            "- Identity is `File + Function + Kind + Ordinal`; line hints do not define identity.",
            "- `Fingerprint` guards against same-ordinal dynamic-row misattribution.",
            "- `teaches` requires a real repo CLI, real API endpoint, or structured next-step field.",
            "- `needs-fix` is frozen bootstrap debt from PR #171 and may not grow silently.",
            "- `exempt` requires a rationale.",
            "",
            "Summary:",
            "",
            f"- Enumerated surfaces: {len(surfaces)}.",
            f"- Teaches: {teaches}.",
            f"- Needs-fix baseline debt: {needs_fix}.",
            "",
            START_MARKER,
            *rows,
            END_MARKER,
            "",
        ]
    )
    path.write_text(content, encoding="utf-8")
    return len(surfaces), teaches, needs_fix


def print_baseline_literal(path: Path = REGISTRY) -> None:
    for entry in sorted(parse_registry(path), key=lambda item: item.baseline_key):
        if entry.classification == "needs-fix":
            print(f'    "{entry.baseline_key}",')


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify AI-native surface registry coherence.")
    parser.add_argument("--write-registry", action="store_true", help="rewrite docs/ai_native_surface_audit.md from current code")
    parser.add_argument("--print-needs-fix-baseline", action="store_true", help="print Python literal rows for BASELINE_NEEDS_FIX_KEYS")
    args = parser.parse_args()
    if args.write_registry:
        total, teaches, needs_fix = write_registry(ROOT, REGISTRY)
        print(f"ai-native coherence registry written: surfaces={total} teaches={teaches} needs_fix={needs_fix}")
        return 0
    if args.print_needs_fix_baseline:
        print_baseline_literal(REGISTRY)
        return 0
    errors = check(ROOT)
    surfaces = discover_surfaces(ROOT)
    entries = parse_registry(REGISTRY)
    needs_fix = sum(1 for entry in entries if entry.classification == "needs-fix")
    if errors:
        print("AI-native coherence check FAILED:", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        print(f"ai-native coherence: surfaces={len(surfaces)} needs_fix={needs_fix}", file=sys.stderr)
        return 1
    print(f"ai-native coherence: PASS (surfaces={len(surfaces)} needs_fix={needs_fix})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
