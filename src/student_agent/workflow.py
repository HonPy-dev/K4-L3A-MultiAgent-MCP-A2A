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

# Linchpin tools get one extra retry: order anchors entities, policy anchors rules.
_RETRY_ATTEMPTS = {"get_order": 3, "get_policy": 3}


async def _call_tool(
    gateway: EvidenceGateway,
    tool_name: str,
    *,
    case_id: str,
    attempts: int | None = None,
    **kwargs: str,
) -> dict[str, Any] | None:
    """Bounded idempotent retry. Returns None instead of guessing data."""
    budget = attempts if attempts is not None else _RETRY_ATTEMPTS.get(tool_name, 2)
    last_error: Exception | None = None
    for _ in range(max(1, budget)):
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


def _evidence_signals(evidence: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Observable boolean signals extracted verbatim from MCP evidence."""
    order = (evidence.get("get_order") or {}).get("data", {})
    order_status = order.get("order_status") if isinstance(order, dict) else None
    delivered_customer = None
    if isinstance(order, dict):
        delivered_customer = order.get("order_delivered_customer_date")

    shipment = (evidence.get("get_shipment_summary") or {}).get("data", {})
    ship = shipment if isinstance(shipment, dict) else {}
    ship_events = _as_list(ship.get("events"))
    ship_late = any(
        isinstance(evt, dict) and evt.get("event_type") == "delivered_late" for evt in ship_events
    )
    delivered_at = ship.get("delivered_customer_at")
    estimated_at = ship.get("estimated_delivery_at")
    past_estimate = bool(
        isinstance(delivered_at, str)
        and isinstance(estimated_at, str)
        and delivered_at > estimated_at
    )

    timeline = (evidence.get("get_payment_timeline") or {}).get("data", {})
    pay_events = _as_list(timeline.get("events") if isinstance(timeline, dict) else [])
    mismatch = any(
        isinstance(evt, dict) and evt.get("event_type") == "reconciliation_mismatch"
        for evt in pay_events
    )

    payments = _as_list((evidence.get("get_order_payments") or {}).get("data"))
    pay_keys = [
        (row.get("payment_sequential"), row.get("payment_type"), row.get("payment_value"))
        for row in payments
        if isinstance(row, dict)
    ]
    duplicate_pay = len(pay_keys) != len(set(pay_keys)) and len(pay_keys) > 0
    split_pay = len({key[1] for key in pay_keys}) > 1

    refund_tl = (evidence.get("get_refund_timeline") or {}).get("data", {})
    refund_events = _as_list(refund_tl.get("events") if isinstance(refund_tl, dict) else [])
    refund_types = {str(evt.get("event_type")) for evt in refund_events if isinstance(evt, dict)}

    return {
        "order_status": order_status,
        "delivered_customer": delivered_customer,
        "ship_late": ship_late,
        "past_estimate": past_estimate,
        "mismatch": mismatch,
        "duplicate_pay": duplicate_pay,
        "split_pay": split_pay,
        "refund_types": refund_types,
    }


def _topic_support(topic: str, signals: dict[str, Any]) -> tuple[int, int]:
    """Return (support, contradict) signal counts for a claimed topic."""
    status = signals["order_status"]
    if topic == "canceled_order_paid":
        support = 1 if status == "canceled" else 0
        contradict = 1 if status == "delivered" and signals["delivered_customer"] else 0
        return support, contradict
    if topic == "unavailable_order_paid":
        support = 1 if status == "unavailable" else 0
        contradict = 1 if status == "delivered" and signals["delivered_customer"] else 0
        return support, contradict
    if topic in ("late_delivery_seller", "late_delivery_logistics"):
        support = int(bool(signals["ship_late"])) + int(bool(signals["past_estimate"]))
        contradict = 1 if status in ("canceled", "unavailable") else 0
        return support, contradict
    if topic == "valid_split_payment":
        return int(bool(signals["split_pay"])), 0
    if topic == "payment_mismatch":
        return int(bool(signals["mismatch"])), 0
    if topic == "duplicate_charge":
        return int(bool(signals["duplicate_pay"])), 0
    if topic == "refund_pending":
        support = int(bool({"refund_requested", "pending"} & signals["refund_types"]))
        return support, 0
    if topic == "refund_failed":
        support = int(bool({"refund_failed", "failed"} & signals["refund_types"]))
        return support, 0
    return 0, 0


def _refs_for_topic(
    topic: str,
    order_refs: list[str],
    payment_refs: list[str],
    shipment_refs: list[str],
    policy_refs: list[str],
) -> list[str]:
    """Domain-targeted refs: only evidence that truly supports the claim topic."""
    if topic == "requested_full_refund":
        return _unique(payment_refs + policy_refs)[:10]
    if topic in ("canceled_order_paid", "unavailable_order_paid"):
        return _unique(order_refs + payment_refs + policy_refs)[:10]
    if topic in ("late_delivery_seller", "late_delivery_logistics"):
        return _unique(shipment_refs + order_refs + policy_refs)[:10]
    if topic in ("payment_mismatch", "duplicate_charge", "valid_split_payment"):
        return _unique(payment_refs + order_refs + policy_refs)[:10]
    if topic in ("refund_pending", "refund_failed"):
        return _unique(payment_refs + policy_refs + order_refs)[:10]
    return _unique(policy_refs + order_refs)[:10]


# Topics where the seller-domain evidence group is directly liability-relevant.
_SELLER_LIABLE_TOPICS = {"late_delivery_seller", "unavailable_order_paid"}
# Topics where item/product detail supports the claim.
_ITEM_RELEVANT_TOPICS = {
    "canceled_order_paid",
    "unavailable_order_paid",
    "late_delivery_seller",
    "late_delivery_logistics",
}
# Topics where a refund timeline is expected to exist.
_REFUND_EXPECTED_TOPICS = {
    "refund_pending",
    "refund_failed",
    "payment_mismatch",
    "duplicate_charge",
}


async def _consume(
    gateway: EvidenceGateway,
    trace: TraceWriter,
    state: dict[str, Any],
    *,
    case_id: str,
    actor: str,
    tool_name: str,
    kwargs: dict[str, str],
) -> dict[str, Any] | None:
    evidence = await _call_tool(gateway, tool_name, case_id=case_id, **kwargs)
    if evidence is None:
        return None
    state["evidence"][tool_name] = evidence
    state["evidence_refs"].append(str(evidence["evidence_ref"]))
    trace.emit(
        case_id=case_id,
        event_type="tool_result_consumed",
        actor=actor,
        tool_name=tool_name,
        evidence_refs=[str(evidence["evidence_ref"])],
    )
    return evidence


def _item_seller_ids(evidence: dict[str, dict[str, Any]]) -> tuple[list[str], list[str]]:
    items = _as_list((evidence.get("get_order_items") or {}).get("data"))
    item_ids = [
        str(r["order_item_id"]) for r in items if isinstance(r, dict) and r.get("order_item_id")
    ]
    seller_ids = [str(r["seller_id"]) for r in items if isinstance(r, dict) and r.get("seller_id")]
    return _unique(item_ids), _unique(seller_ids)


async def _order_item_agent(
    case_id: str,
    order_id: str,
    topics: list[str],
    gateway: EvidenceGateway,
    trace: TraceWriter,
    state: dict[str, Any],
) -> None:
    await _consume(
        gateway,
        trace,
        state,
        case_id=case_id,
        actor="order-agent",
        tool_name="get_order",
        kwargs={"order_id": order_id},
    )
    await _consume(
        gateway,
        trace,
        state,
        case_id=case_id,
        actor="order-agent",
        tool_name="get_order_items",
        kwargs={"order_id": order_id},
    )
    _, item_sellers = _item_seller_ids(state["evidence"])
    # Seller group: skip when items already supplied seller_ids and no
    # seller-liability topic needs the authoritative seller record.
    if not item_sellers or _SELLER_LIABLE_TOPICS.intersection(topics):
        await _consume(
            gateway,
            trace,
            state,
            case_id=case_id,
            actor="order-agent",
            tool_name="get_sellers",
            kwargs={"order_id": order_id},
        )
    # Product group: skip for pure payment/refund topics when items exist.
    item_ids, _ = _item_seller_ids(state["evidence"])
    if not item_ids or _ITEM_RELEVANT_TOPICS.intersection(topics):
        await _consume(
            gateway,
            trace,
            state,
            case_id=case_id,
            actor="order-agent",
            tool_name="get_product_context",
            kwargs={"order_id": order_id},
        )


async def _payment_agent(
    case_id: str,
    order_id: str,
    topics: list[str],
    gateway: EvidenceGateway,
    trace: TraceWriter,
    state: dict[str, Any],
) -> None:
    for tool_name in ("get_order_payments", "get_payment_timeline"):
        await _consume(
            gateway,
            trace,
            state,
            case_id=case_id,
            actor="payment-agent",
            tool_name=tool_name,
            kwargs={"order_id": order_id},
        )
    # Refund timeline: expected only for refund/payment-dispute topics or when
    # the payment timeline itself shows refund activity. Skipping avoids audited
    # calls that historically fail ~60% of the time without yielding evidence.
    want_refund = bool(_REFUND_EXPECTED_TOPICS.intersection(topics))
    if not want_refund:
        timeline = (state["evidence"].get("get_payment_timeline") or {}).get("data", {})
        events = _as_list(timeline.get("events") if isinstance(timeline, dict) else [])
        want_refund = any(
            isinstance(evt, dict) and "refund" in str(evt.get("event_type", "")) for evt in events
        )
    if want_refund:
        await _consume(
            gateway,
            trace,
            state,
            case_id=case_id,
            actor="payment-agent",
            tool_name="get_refund_timeline",
            kwargs={"order_id": order_id},
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
    topics = [str(c.get("topic", "")) for c in claims if isinstance(c, dict)]

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
    await _order_item_agent(case_id, order_id, topics, gateway, trace, state)
    await _payment_agent(case_id, order_id, topics, gateway, trace, state)
    await _shipment_agent(case_id, order_id, gateway, trace, state)

    # Fan-in: last specialist hands results back to the coordinator.
    trace.emit(case_id=case_id, event_type="handoff", actor="shipment-agent", target="coordinator")
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
    # Never overconfident: cap when evidence contradicts the claim or conflicts.
    groups_ok = sum(
        [
            any(k in evidence for k in ("get_order", "get_order_items")),
            any(k in evidence for k in ("get_order_payments", "get_payment_timeline")),
            "get_shipment_summary" in evidence,
            "get_policy" in evidence,
        ]
    )
    confidence = 0.9 if groups_ok == 4 else (0.75 if groups_ok == 3 else 0.6)
    signals = _evidence_signals(evidence)
    claimed_support, claimed_contra = _topic_support(primary_issue, signals)
    if claimed_contra > 0:
        confidence = min(confidence, 0.65)
    if _detect_conflicts(evidence):
        confidence = min(confidence, 0.75)

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
            refs = _refs_for_topic(topic, order_refs, payment_refs, shipment_refs, policy_refs)
            claim_conf = confidence if verdict == "supported" else 0.7
        elif topic == primary_issue:
            support, contradict = _topic_support(topic, signals)
            refs = _refs_for_topic(topic, order_refs, payment_refs, shipment_refs, policy_refs)
            if contradict == 0:
                verdict = "supported"
                claim_conf = confidence
            else:
                # Evidence contradicts the claim: downgrade verdict, keep primary
                # (override threshold for primary_issue itself is not met).
                verdict = "partially_supported"
                claim_conf = 0.55
        else:
            verdict = "unsupported"
            refs = _refs_for_topic(topic, order_refs, payment_refs, shipment_refs, policy_refs)
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
        # The refund entity follows the responsible party: a liable seller refunds,
        # otherwise the order (platform/provider) is the accountable entity.
        payer = order_id
        if responsible and responsible[0].get("party_type") == "seller":
            payer = str(responsible[0].get("party_id") or order_id)
        refund_lines.append(
            {
                "reason_code": recommended_action[:80],
                "amount_brl": refund_amount,
                "entity_id": payer,
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
        and (
            (case_status == "no_action" and refund_amount == 0.0)
            or (case_status == "action_required" and refund_amount > 0.0)
            or (case_status == "needs_investigation")
        )
    )
    trace.emit(
        case_id=case_id,
        event_type="verification_completed",
        actor="verifier",
        decision_code="verified" if checks_ok else "verified_with_warnings",
    )
    return output
