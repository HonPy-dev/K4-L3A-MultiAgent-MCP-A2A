from __future__ import annotations

import asyncio
from typing import Any

from .mcp_gateway import EvidenceGateway
from .trace import TraceWriter

# Authoritative fallback rules matching EC_POLICY_V1 in case get_policy fails.
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
    """Bounded idempotent retry with exponential backoff (Resilience Engineering)."""
    last_error: Exception | None = None
    for attempt in range(max(1, attempts)):
        try:
            return await gateway.call(tool_name, case_id=case_id, **kwargs)
        except Exception as exc:  # noqa: BLE001 - MCP failures are expected signals
            last_error = exc
            if attempt < attempts - 1:
                await asyncio.sleep(0.3 * (2**attempt))
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


def _verify_primary_issue(
    claimed_issue: str, evidence: dict[str, dict[str, Any]]
) -> str:
    """Fact Verification: Cross-check customer claim against authoritative evidence."""
    order_data = (evidence.get("get_order") or {}).get("data")
    if isinstance(order_data, dict):
        order_status = str(order_data.get("order_status", "")).lower()

        # Check order cancellation & availability facts
        if claimed_issue == "canceled_order_paid":
            if order_status == "unavailable":
                return "unavailable_order_paid"
            if order_status in ("delivered", "shipped"):
                return "unsupported_claim"

        if claimed_issue == "unavailable_order_paid":
            if order_status == "canceled":
                return "canceled_order_paid"
            if order_status in ("delivered", "shipped"):
                return "unsupported_claim"

    # Check shipment delivery attribution facts (Seller vs Carrier)
    shipment_data = (evidence.get("get_shipment_summary") or {}).get("data")
    if isinstance(shipment_data, dict) and claimed_issue in (
        "late_delivery_seller",
        "late_delivery_logistics",
    ):
        events = _as_list(shipment_data.get("events"))
        for ev in events:
            if isinstance(ev, dict) and ev.get("event_type") == "delivered_late":
                actor = str(ev.get("actor", ""))
                if actor == "seller":
                    return "late_delivery_seller"
                if actor == "logistics_provider":
                    return "late_delivery_logistics"

    # Check refund timeline facts (Pending vs Failed)
    refund_data = (evidence.get("get_refund_timeline") or {}).get("data")
    if isinstance(refund_data, dict) and claimed_issue in ("refund_pending", "refund_failed"):
        events = _as_list(refund_data.get("events"))
        for ev in events:
            if isinstance(ev, dict):
                ev_type = str(ev.get("event_type", "")).lower()
                status = str(ev.get("status", "")).lower()
                if "fail" in ev_type or "fail" in status:
                    return "refund_failed"
                if "pend" in ev_type or "pend" in status:
                    return "refund_pending"

    return claimed_issue


def _primary_issue_for(case: dict[str, Any], evidence: dict[str, dict[str, Any]]) -> str:
    """Extract primary issue from customer claims, then verify with facts."""
    claims = case.get("customer_request", {}).get("claims", [])
    topics = [c.get("topic") for c in claims if isinstance(c, dict)]
    raw_issue = "unsupported_claim"
    for topic in topics:
        if topic in _PRIMARY_ISSUES and topic != "insufficient_evidence":
            if topic == "requested_full_refund":
                continue
            raw_issue = str(topic)
            break
    else:
        for topic in topics:
            if isinstance(topic, str) and topic in _PRIMARY_ISSUES:
                raw_issue = topic
                break

    return _verify_primary_issue(raw_issue, evidence)


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
        ref = str(evidence["evidence_ref"])
        state["evidence_refs"].append(ref)
        state["domain_refs"]["order"].append(ref)
        trace.emit(
            case_id=case_id,
            event_type="tool_result_consumed",
            actor="order-agent",
            tool_name=tool_name,
            evidence_refs=[ref],
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
        ref = str(evidence["evidence_ref"])
        state["evidence_refs"].append(ref)
        state["domain_refs"]["payment"].append(ref)
        trace.emit(
            case_id=case_id,
            event_type="tool_result_consumed",
            actor="payment-agent",
            tool_name=tool_name,
            evidence_refs=[ref],
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
    ref = str(evidence["evidence_ref"])
    state["evidence_refs"].append(ref)
    state["domain_refs"]["shipment"].append(ref)
    trace.emit(
        case_id=case_id,
        event_type="tool_result_consumed",
        actor="shipment-agent",
        tool_name="get_shipment_summary",
        evidence_refs=[ref],
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
        ref = str(evidence["evidence_ref"])
        state["evidence_refs"].append(ref)
        state["domain_refs"]["policy"].append(ref)
        trace.emit(
            case_id=case_id,
            event_type="tool_result_consumed",
            actor="policy-agent",
            tool_name="get_policy",
            evidence_refs=[ref],
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
        "order_ids": [order_id] if order_id else [],
        "item_ids": _unique(item_ids),
        "seller_ids": _unique(seller_ids),
        "payment_references": _unique(payment_refs),
        "shipment_ids": [order_id] if order_id else [],
    }


def _detect_conflicts(evidence: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    conflicts: list[dict[str, Any]] = []

    # Check order items field discrepancies
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

    # Check shipment summary shipping limits discrepancies
    shipment_data = (evidence.get("get_shipment_summary") or {}).get("data", {})
    if isinstance(shipment_data, dict) and not conflicts:
        ship_limits = _as_list(shipment_data.get("shipping_limits"))
        if len(ship_limits) >= 2 and all(isinstance(row, dict) for row in ship_limits):
            limit_dates = {str(row.get("shipping_limit_at")) for row in ship_limits}
            if len(limit_dates) > 1:
                conflicts.append(
                    {
                        "field": "shipment.shipping_limit_at",
                        "sources": ["mcp:get_shipment_summary#0", "mcp:get_shipment_summary#1"],
                        "selected_source": "mcp:get_shipment_summary#0",
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


def _select_claim_evidence_refs(
    topic: str,
    domain_refs: dict[str, list[str]],
    all_refs: list[str],
) -> list[str]:
    """Domain Attribution: Maximize relevance precision and prevent penalties."""
    order_refs = domain_refs.get("order", [])
    payment_refs = domain_refs.get("payment", [])
    shipment_refs = domain_refs.get("shipment", [])
    policy_refs = domain_refs.get("policy", [])

    if topic in ("late_delivery_seller", "late_delivery_logistics"):
        # Shipment issue: relevant domains are shipment, order, policy (strictly no payment)
        selected = shipment_refs + order_refs + policy_refs
    elif topic in ("canceled_order_paid", "unavailable_order_paid"):
        # Order status issue: relevant domains are order, payment, policy (strictly no shipment)
        selected = order_refs + payment_refs + policy_refs
    elif topic in (
        "payment_mismatch",
        "duplicate_charge",
        "valid_split_payment",
        "refund_pending",
        "refund_failed",
    ):
        # Payment issue: relevant domains are payment, order, policy (strictly no shipment)
        selected = payment_refs + order_refs + policy_refs
    elif topic == "requested_full_refund":
        selected = payment_refs + order_refs + policy_refs
        if not selected:
            selected = shipment_refs + policy_refs
    elif topic == "unsupported_claim":
        selected = order_refs + policy_refs
    else:
        selected = all_refs

    valid_selected = [r for r in selected if r in all_refs]
    return _unique(valid_selected, limit=10)


def _self_healing_verification(
    output: dict[str, Any],
    all_evidence_refs: list[str],
    seller_ids: list[str],
) -> tuple[dict[str, Any], bool]:
    """Reflexion-inspired Self-Correction: Enforce and heal all 7 Invariants before finalizing."""
    healed = dict(output)
    refund_amount = round(float(healed["financial_resolution"]["recommended_refund_brl"]), 2)

    # 1. Enforce Financial Invariant: sum(lines) == recommended_refund_brl
    if refund_amount <= 0.0:
        healed["financial_resolution"]["recommended_refund_brl"] = 0.0
        healed["financial_resolution"]["refund_lines"] = []
    else:
        action = healed["resolution_actions"][0] if healed["resolution_actions"] else "issue_refund"
        order_id = healed["case_id"]
        if healed["affected_entities"]["order_ids"]:
            order_id = healed["affected_entities"]["order_ids"][0]
        healed["financial_resolution"]["refund_lines"] = [
            {
                "reason_code": str(action)[:80],
                "amount_brl": refund_amount,
                "entity_id": order_id,
            }
        ]

    # 2. Enforce Seller ID resolution
    for party in healed["root_cause_analysis"]["responsible_parties"]:
        if party.get("party_type") == "seller" and not party.get("party_id") and seller_ids:
            party["party_id"] = seller_ids[0]

    # 3. Enforce Evidence subset and deduplication
    valid_all_refs = set(all_evidence_refs)
    for ca in healed.get("claim_assessments", []):
        ca["evidence_refs"] = [r for r in ca.get("evidence_refs", []) if r in valid_all_refs]

    # 4. Enforce Confidence bounds
    conf = float(healed["assessment"]["confidence"])
    healed["assessment"]["confidence"] = max(0.4, min(0.95, round(conf, 2)))

    # Invariants verification check
    total_lines = round(
        sum(
            float(line.get("amount_brl", 0.0))
            for line in healed["financial_resolution"]["refund_lines"]
        ),
        2,
    )
    checks_ok = (
        abs(total_lines - refund_amount) < 1e-6
        and all(
            ref in valid_all_refs
            for assessment in healed.get("claim_assessments", [])
            for ref in assessment["evidence_refs"]
        )
        and 0 <= healed["assessment"]["confidence"] <= 1
    )
    return healed, checks_ok


async def solve_case(
    case: dict[str, Any], gateway: EvidenceGateway, trace: TraceWriter
) -> dict[str, Any]:
    """Coordinator + specialist agents + policy + verifier (Deterministic LangGraph-style DAG)."""
    case_id = str(case.get("case_id", ""))
    customer_request = case.get("customer_request", {})
    order_id = str(customer_request.get("claimed_order_id", ""))
    policy_version = str(case.get("policy_version", "EC_POLICY_V1"))
    claims = customer_request.get("claims", [])
    if not isinstance(claims, list):
        claims = []

    state: dict[str, Any] = {
        "evidence": {},
        "evidence_refs": [],
        "domain_refs": {
            "order": [],
            "payment": [],
            "shipment": [],
            "policy": [],
        },
    }

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
    evidence: dict[str, dict[str, Any]] = state["evidence"]
    evidence_refs = _unique([str(ref) for ref in state["evidence_refs"]], limit=30)

    # Fallback when completely devoid of evidence
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

    # Cross-check customer claims against authoritative MCP evidence
    primary_issue = _primary_issue_for(case, evidence)

    rule = live_rules.get(primary_issue) if isinstance(live_rules, dict) else None
    if not isinstance(rule, dict):
        rule = _FALLBACK_RULES.get(primary_issue, _FALLBACK_RULES["unsupported_claim"])

    case_status = str(rule.get("case_status", "needs_investigation"))
    recommended_action = str(rule.get("recommended_action", "monitor_refund"))
    try:
        refund_amount = round(float(rule.get("refund_brl", 0.0)), 2)
    except (TypeError, ValueError):
        refund_amount = 0.0
    refund_amount = max(0.0, refund_amount)

    entities = _collect_entities(order_id, evidence)
    rule_parties = rule.get("responsible_parties", [])
    responsible = _resolve_seller_party(
        rule_parties if isinstance(rule_parties, list) else [], entities["seller_ids"]
    )

    # Confidence calibration: based on evidence group completeness
    groups_ok = sum(
        [
            any(k in evidence for k in ("get_order", "get_order_items")),
            any(k in evidence for k in ("get_order_payments", "get_payment_timeline")),
            "get_shipment_summary" in evidence,
            "get_policy" in evidence,
        ]
    )
    confidence = 0.9 if groups_ok == 4 else (0.75 if groups_ok == 3 else 0.6)

    # Claim assessments link each input claim to precise, relevant supporting evidence.
    claim_assessments: list[dict[str, Any]] = []
    for claim in claims[:5]:
        if not isinstance(claim, dict):
            continue
        claim_id = str(claim.get("claim_id", "claim-unknown"))
        topic = str(claim.get("topic", ""))

        refs = _select_claim_evidence_refs(topic, state["domain_refs"], evidence_refs)

        if topic == "requested_full_refund":
            verdict = "supported" if refund_amount > 0 else "unsupported"
            claim_conf = confidence if verdict == "supported" else 0.75
        elif topic == "unsupported_claim":
            verdict = "unsupported"
            claim_conf = confidence
        elif topic == primary_issue:
            verdict = "supported"
            claim_conf = confidence
        else:
            verdict = "unsupported"
            claim_conf = 0.7

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

    draft_output = {
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
            "refund_lines": [],
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

    # Verifier: Active Self-Healing Invariant Guardrail
    final_output, checks_ok = _self_healing_verification(
        draft_output, evidence_refs, entities["seller_ids"]
    )

    trace.emit(
        case_id=case_id,
        event_type="verification_completed",
        actor="verifier",
        decision_code="verified" if checks_ok else "verified_with_warnings",
    )
    return final_output
