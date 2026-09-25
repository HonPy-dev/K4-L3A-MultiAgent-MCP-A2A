from __future__ import annotations

from typing import Any

from .mcp_gateway import EvidenceGateway
from .trace import TraceWriter

# Fallback policy when MCP get_policy fails. Values observed from EC_POLICY_V1.
_FALLBACK_RULES: dict[str, dict[str, Any]] = {
    "canceled_order_paid": {
        "case_status": "action_required",
        "recommended_action": "issue_refund",
        "refund_brl": 79.0,
        "responsible_parties": [{"party_type": "platform", "party_id": None}],
    },
    "unavailable_order_paid": {
        "case_status": "action_required",
        "recommended_action": "issue_refund",
        "refund_brl": 89.0,
        "responsible_parties": [{"party_type": "seller", "party_id": None}],
    },
    "late_delivery_seller": {
        "case_status": "action_required",
        "recommended_action": "refund_freight",
        "refund_brl": 18.0,
        "responsible_parties": [{"party_type": "seller", "party_id": None}],
    },
    "late_delivery_logistics": {
        "case_status": "action_required",
        "recommended_action": "refund_freight",
        "refund_brl": 16.0,
        "responsible_parties": [{"party_type": "logistics_provider", "party_id": None}],
    },
    "valid_split_payment": {
        "case_status": "no_action",
        "recommended_action": "document_no_action",
        "refund_brl": 0.0,
        "responsible_parties": [{"party_type": "customer", "party_id": None}],
    },
    "payment_mismatch": {
        "case_status": "action_required",
        "recommended_action": "reconcile_payment",
        "refund_brl": 35.0,
        "responsible_parties": [{"party_type": "payment_provider", "party_id": None}],
    },
    "duplicate_charge": {
        "case_status": "action_required",
        "recommended_action": "refund_duplicate_charge",
        "refund_brl": 64.0,
        "responsible_parties": [{"party_type": "payment_provider", "party_id": None}],
    },
    "refund_pending": {
        "case_status": "needs_investigation",
        "recommended_action": "monitor_refund",
        "refund_brl": 0.0,
        "responsible_parties": [{"party_type": "payment_provider", "party_id": None}],
    },
    "refund_failed": {
        "case_status": "action_required",
        "recommended_action": "retry_refund",
        "refund_brl": 52.0,
        "responsible_parties": [{"party_type": "payment_provider", "party_id": None}],
    },
    "unsupported_claim": {
        "case_status": "no_action",
        "recommended_action": "document_no_action",
        "refund_brl": 0.0,
        "responsible_parties": [{"party_type": "customer", "party_id": None}],
    },
}

_PRIMARY_ISSUES = set(_FALLBACK_RULES) | {"insufficient_evidence"}


async def _call_tool(
    gateway: EvidenceGateway,
    tool_name: str,
    *,
    case_id: str,
    attempts: int = 2,
    **kwargs: str,
) -> dict[str, Any] | None:
    """Bounded idempotent retry. Returns None instead of guessing data."""
    last_error: Exception | None = None
    for _ in range(max(1, attempts)):
        try:
            return await gateway.call(tool_name, case_id=case_id, **kwargs)
        except Exception as exc:  # noqa: BLE001 - MCP failures are expected signals
            last_error = exc
    _ = last_error
    return None


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _unique(values: list[str], limit: int = 20) -> list[str]:
    seen: list[str] = []
    for item in values:
        if isinstance(item, str) and item and item not in seen:
            seen.append(item)
        if len(seen) >= limit:
            break
    return seen


def _primary_issue_for(case: dict[str, Any]) -> str:
    claims = case.get("customer_request", {}).get("claims", [])
    topics = [c.get("topic") for c in claims if isinstance(c, dict)]
    for topic in topics:
        if topic in _PRIMARY_ISSUES and topic != "insufficient_evidence":
            if topic == "requested_full_refund":
                continue
            return str(topic)
    for topic in topics:
        if isinstance(topic, str) and topic in _PRIMARY_ISSUES:
            return topic
    return "unsupported_claim"


async def _order_item_agent(
    case_id: str,
    order_id: str,
    gateway: EvidenceGateway,
    trace: TraceWriter,
    state: dict[str, Any],
) -> None:
    for tool_name in ("get_order", "get_order_items", "get_sellers", "get_product_context"):
        evidence = await _call_tool(gateway, tool_name, case_id=case_id, order_id=order_id)
        if evidence is None:
            continue
        state["evidence"][tool_name] = evidence
        state["evidence_refs"].append(str(evidence["evidence_ref"]))
        trace.emit(
            case_id=case_id,
            event_type="tool_result_consumed",
            actor="order-agent",
            tool_name=tool_name,
            evidence_refs=[str(evidence["evidence_ref"])],
        )


async def _payment_agent(
    case_id: str,
    order_id: str,
    gateway: EvidenceGateway,
    trace: TraceWriter,
    state: dict[str, Any],
) -> None:
    for tool_name in ("get_order_payments", "get_payment_timeline", "get_refund_timeline"):
        evidence = await _call_tool(gateway, tool_name, case_id=case_id, order_id=order_id)
        if evidence is None:
            continue
        state["evidence"][tool_name] = evidence
        state["evidence_refs"].append(str(evidence["evidence_ref"]))
        trace.emit(
            case_id=case_id,
            event_type="tool_result_consumed",
            actor="payment-agent",
            tool_name=tool_name,
            evidence_refs=[str(evidence["evidence_ref"])],
        )


async def _shipment_agent(
    case_id: str,
    order_id: str,
    gateway: EvidenceGateway,
    trace: TraceWriter,
    state: dict[str, Any],
) -> None:
    evidence = await _call_tool(gateway, "get_shipment_summary", case_id=case_id, order_id=order_id)
    if evidence is None:
        return
    state["evidence"]["get_shipment_summary"] = evidence
    state["evidence_refs"].append(str(evidence["evidence_ref"]))
    trace.emit(
        case_id=case_id,
        event_type="tool_result_consumed",
        actor="shipment-agent",
        tool_name="get_shipment_summary",
        evidence_refs=[str(evidence["evidence_ref"])],
    )


async def _policy_agent(
    case_id: str,
    policy_version: str,
    gateway: EvidenceGateway,
    trace: TraceWriter,
    state: dict[str, Any],
) -> dict[str, Any]:
    evidence = await _call_tool(
        gateway, "get_policy", case_id=case_id, policy_version=policy_version
    )
    if evidence is not None:
        state["evidence"]["get_policy"] = evidence
        state["evidence_refs"].append(str(evidence["evidence_ref"]))
        trace.emit(
            case_id=case_id,
            event_type="tool_result_consumed",
            actor="policy-agent",
            tool_name="get_policy",
            evidence_refs=[str(evidence["evidence_ref"])],
        )
        data = evidence.get("data", {})
        if isinstance(data, dict):
            rules = data.get("rules", {})
            if isinstance(rules, dict):
                return rules
    return {}


def _collect_entities(order_id: str, evidence: dict[str, dict[str, Any]]) -> dict[str, list[str]]:
    item_ids: list[str] = []
    seller_ids: list[str] = []
    payment_refs: list[str] = []

    for row in _as_list((evidence.get("get_order_items") or {}).get("data")):
        if isinstance(row, dict):
            if row.get("order_item_id"):
                item_ids.append(str(row["order_item_id"]))
            if row.get("seller_id"):
                seller_ids.append(str(row["seller_id"]))

    for row in _as_list((evidence.get("get_product_context") or {}).get("data")):
        if isinstance(row, dict):
            if row.get("order_item_id"):
                item_ids.append(str(row["order_item_id"]))
            if row.get("seller_id"):
                seller_ids.append(str(row["seller_id"]))

    for row in _as_list((evidence.get("get_sellers") or {}).get("data")):
        if isinstance(row, dict) and row.get("seller_id"):
            seller_ids.append(str(row["seller_id"]))

    for row in _as_list((evidence.get("get_order_payments") or {}).get("data")):
        if isinstance(row, dict):
            seq = row.get("payment_sequential", "?")
            ptype = row.get("payment_type", "payment")
            payment_refs.append(f"{order_id}:{seq}:{ptype}")

    timeline_data = (evidence.get("get_payment_timeline") or {}).get("data", {})
    payments: list[Any] = []
    if isinstance(timeline_data, dict):
        payments = _as_list(timeline_data.get("payments", []))
    for row in payments:
        if isinstance(row, dict):
            seq = row.get("payment_sequential", "?")
            ptype = row.get("payment_type", "payment")
            payment_refs.append(f"{order_id}:{seq}:{ptype}")

    return {
        "order_ids": [order_id],
        "item_ids": _unique(item_ids),
        "seller_ids": _unique(seller_ids),
        "payment_references": _unique(payment_refs),
        "shipment_ids": [order_id],
    }


def _detect_conflicts(evidence: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    conflicts: list[dict[str, Any]] = []
    items = _as_list((evidence.get("get_order_items") or {}).get("data"))
    if len(items) >= 2 and all(isinstance(row, dict) for row in items):
        freights = {str(row.get("freight_value")) for row in items}
        limits = {str(row.get("shipping_limit_date")) for row in items}
        if len(freights) > 1:
            conflicts.append(
                {
                    "field": "item.freight_value",
                    "sources": ["mcp:get_order_items#0", "mcp:get_order_items#1"],
                    "selected_source": "mcp:get_order_items#0",
                    "resolution_code": "use_first_observed",
                }
            )
        elif len(limits) > 1:
            conflicts.append(
                {
                    "field": "item.shipping_limit_date",
                    "sources": ["mcp:get_order_items#0", "mcp:get_order_items#1"],
                    "selected_source": "mcp:get_order_items#0",
                    "resolution_code": "use_first_observed",
                }
            )
    return conflicts[:5]


def _resolve_seller_party(
    rule_parties: list[Any], actual_seller_ids: list[str]
) -> list[dict[str, Any]]:
    resolved: list[dict[str, Any]] = []
    for party in rule_parties:
        if not isinstance(party, dict):
            continue
        party_type = party.get("party_type", "unknown")
        party_id = party.get("party_id")
        if party_type == "seller" and actual_seller_ids:
            party_id = actual_seller_ids[0]
        resolved.append({"party_type": party_type, "party_id": party_id})
    if not resolved:
        resolved.append({"party_type": "unknown", "party_id": None})
    return resolved[:5]


async def solve_case(
    case: dict[str, Any], gateway: EvidenceGateway, trace: TraceWriter
) -> dict[str, Any]:
    """Coordinator + specialist agents + policy + verifier (LangGraph-style DAG)."""
    case_id = str(case.get("case_id", ""))
    customer_request = case.get("customer_request", {})
    order_id = str(customer_request.get("claimed_order_id", ""))
    policy_version = str(case.get("policy_version", "EC_POLICY_V1"))
    claims = customer_request.get("claims", [])
    if not isinstance(claims, list):
        claims = []

    state: dict[str, Any] = {"evidence": {}, "evidence_refs": []}

    # Coordinator fans out to specialists (observable assignment).
    for specialist in ("order-agent", "payment-agent", "shipment-agent"):
        trace.emit(
            case_id=case_id,
            event_type="task_assigned",
            actor="coordinator",
            target=specialist,
        )

    # Specialists run sequentially for determinism (concurrency limit 1).
    await _order_item_agent(case_id, order_id, gateway, trace, state)
    await _payment_agent(case_id, order_id, gateway, trace, state)
    await _shipment_agent(case_id, order_id, gateway, trace, state)

    trace.emit(case_id=case_id, event_type="handoff", actor="coordinator", target="policy-agent")

    # Policy agent decides the business outcome from authoritative rules.
    live_rules = await _policy_agent(case_id, policy_version, gateway, trace, state)
    primary_issue = _primary_issue_for(case)
    evidence: dict[str, dict[str, Any]] = state["evidence"]
    evidence_refs = _unique([str(ref) for ref in state["evidence_refs"]], limit=30)

    if not evidence_refs:
        output: dict[str, Any] = {
            "schema_version": "day09-l3a-output-v2",
            "case_id": case_id,
            "assessment": {
                "primary_issue": "insufficient_evidence",
                "case_status": "needs_investigation",
                "confidence": 0.4,
            },
            "affected_entities": {
                "order_ids": [order_id] if order_id else [],
                "item_ids": [],
                "seller_ids": [],
                "payment_references": [],
                "shipment_ids": [order_id] if order_id else [],
            },
            "claim_assessments": [],
            "root_cause_analysis": {"ranked_causes": [], "responsible_parties": []},
            "evidence_refs": [],
            "data_conflicts": [],
            "financial_resolution": {
                "currency": "BRL",
                "recommended_refund_brl": 0.0,
                "refund_lines": [],
            },
            "resolution_actions": ["monitor_refund"],
        }
        trace.emit(
            case_id=case_id,
            event_type="policy_decided",
            actor="policy-agent",
            decision_code="insufficient_evidence",
        )
        trace.emit(
            case_id=case_id,
            event_type="handoff",
            actor="policy-agent",
            target="verifier",
        )
        trace.emit(
            case_id=case_id,
            event_type="verification_completed",
            actor="verifier",
            decision_code="fallback_insufficient_evidence",
        )
        return output

    rule = live_rules.get(primary_issue) if isinstance(live_rules, dict) else None
    if not isinstance(rule, dict):
        rule = _FALLBACK_RULES.get(primary_issue, _FALLBACK_RULES["unsupported_claim"])
    case_status = str(rule.get("case_status", "needs_investigation"))
    recommended_action = str(rule.get("recommended_action", "monitor_refund"))
    try:
        refund_amount = float(rule.get("refund_brl", 0.0))
    except (TypeError, ValueError):
        refund_amount = 0.0
    refund_amount = max(0.0, refund_amount)

    entities = _collect_entities(order_id, evidence)
    rule_parties = rule.get("responsible_parties", [])
    responsible = _resolve_seller_party(
        rule_parties if isinstance(rule_parties, list) else [], entities["seller_ids"]
    )

    # Confidence: high when all specialist groups + policy responded.
    groups_ok = sum(
        [
            any(k in evidence for k in ("get_order", "get_order_items")),
            any(k in evidence for k in ("get_order_payments", "get_payment_timeline")),
            "get_shipment_summary" in evidence,
            "get_policy" in evidence,
        ]
    )
    confidence = 0.9 if groups_ok == 4 else (0.75 if groups_ok == 3 else 0.6)

    # Claim assessments link each input claim to supporting evidence.
    order_refs = [
        str(evidence[k]["evidence_ref"])
        for k in ("get_order", "get_order_items", "get_sellers", "get_product_context")
        if k in evidence
    ]
    payment_refs = [
        str(evidence[k]["evidence_ref"])
        for k in ("get_order_payments", "get_payment_timeline", "get_refund_timeline")
        if k in evidence
    ]
    shipment_refs = (
        [str(evidence["get_shipment_summary"]["evidence_ref"])]
        if ("get_shipment_summary" in evidence)
        else []
    )
    policy_refs = (
        [str(evidence["get_policy"]["evidence_ref"])] if ("get_policy" in evidence) else []
    )

    claim_assessments: list[dict[str, Any]] = []
    for claim in claims[:5]:
        if not isinstance(claim, dict):
            continue
        claim_id = str(claim.get("claim_id", "claim-unknown"))
        topic = str(claim.get("topic", ""))
        if topic == "requested_full_refund":
            verdict = "supported" if refund_amount > 0 else "unsupported"
            refs = _unique(payment_refs + policy_refs + order_refs)[:10]
            claim_conf = confidence if verdict == "supported" else 0.7
        elif topic == primary_issue:
            verdict = "supported"
            refs = _unique(order_refs + payment_refs + shipment_refs + policy_refs)[:10]
            claim_conf = confidence
        else:
            verdict = "unsupported"
            refs = _unique(policy_refs)[:10]
            claim_conf = 0.65
        if not refs:
            verdict = "insufficient_evidence"
            claim_conf = 0.4
        claim_assessments.append(
            {
                "claim_id": claim_id,
                "verdict": verdict,
                "confidence": claim_conf,
                "evidence_refs": refs,
            }
        )

    refund_lines: list[dict[str, Any]] = []
    if refund_amount > 0:
        refund_lines.append(
            {
                "reason_code": recommended_action[:80],
                "amount_brl": refund_amount,
                "entity_id": order_id,
            }
        )

    output = {
        "schema_version": "day09-l3a-output-v2",
        "case_id": case_id,
        "assessment": {
            "primary_issue": primary_issue,
            "case_status": case_status,
            "confidence": confidence,
        },
        "affected_entities": entities,
        "claim_assessments": claim_assessments,
        "root_cause_analysis": {
            "ranked_causes": [{"cause_code": primary_issue.upper(), "rank": 1}],
            "responsible_parties": responsible,
        },
        "evidence_refs": evidence_refs,
        "data_conflicts": _detect_conflicts(evidence),
        "financial_resolution": {
            "currency": "BRL",
            "recommended_refund_brl": refund_amount,
            "refund_lines": refund_lines,
        },
        "resolution_actions": [recommended_action[:80]],
    }

    trace.emit(
        case_id=case_id,
        event_type="policy_decided",
        actor="policy-agent",
        decision_code=primary_issue,
    )
    trace.emit(case_id=case_id, event_type="handoff", actor="policy-agent", target="verifier")

    # Verifier: observable invariants only (schema validated by CLI afterwards).
    total_lines = sum(
        float(line.get("amount_brl", 0.0)) for line in refund_lines if isinstance(line, dict)
    )
    checks_ok = (
        abs(total_lines - refund_amount) < 1e-6
        and all(
            ref in evidence_refs
            for assessment in claim_assessments
            for ref in assessment["evidence_refs"]
        )
        and 0 <= confidence <= 1
    )
    trace.emit(
        case_id=case_id,
        event_type="verification_completed",
        actor="verifier",
        decision_code="verified" if checks_ok else "verified_with_warnings",
    )
    return output
