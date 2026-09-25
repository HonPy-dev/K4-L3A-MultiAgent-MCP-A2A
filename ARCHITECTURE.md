# L3A Architecture Record

Team phải cập nhật tài liệu này cùng source. Mục tiêu là mô tả quyết định có thể kiểm chứng, không ghi prompt bí mật hoặc chain-of-thought.

## 1. System overview

Luồng từ `inputs/<case_id>.json` đến MCP calls, specialist agents, verifier, output và trace.
Triển khai tại `src/student_agent/workflow.py:solve_case()` — async state-machine
theo phong cách LangGraph (nodes + edges định sẵn, không thêm dependency).

```text
Input → Coordinator → Specialists → Policy → Verifier → Output
              │            │            │          │
              │            └── MCP ─────┴── Trace ─┘
              └── task_assigned / handoff / case_received / case_finalized
```

`src/student_agent/cli.py:_run()` lo `case_received` / `case_finalized` và validate
output theo `l3a-output-v2.schema.json`. `solve_case()` lo phần còn lại:
`task_assigned` → `tool_result_consumed` → `handoff` → `policy_decided` →
`verification_completed`.

## 2. Agent ownership

| Actor | Input | Trách nhiệm | Output/handoff |
| --- | --- | --- | --- |
| Coordinator | case, claimed_order_id | Emit 3 `task_assigned`, điều phối tuần tự, emit `handoff` → policy-agent | Handoff sang policy-agent |
| Order/item (order-agent) | order_id | Gọi `get_order`, `get_order_items`, `get_sellers`, `get_product_context`; thu item_ids, seller_ids | Evidence order/item + `tool_result_consumed` |
| Payment (payment-agent) | order_id | Gọi `get_order_payments`, `get_payment_timeline`, `get_refund_timeline` (optional); thu payment_references | Evidence payment/refund + `tool_result_consumed` |
| Shipment (shipment-agent) | order_id | Gọi `get_shipment_summary`; thu shipment_ids | Evidence shipment + `tool_result_consumed` |
| Policy (policy-agent) | policy_version + kết quả specialists | Gọi `get_policy`; chọn `primary_issue` từ claim topic, đọc rule `{case_status, refund_brl, responsible_parties, recommended_action}`; emit `policy_decided` | Handoff sang verifier |
| Verifier (verifier) | output dự thảo + evidence_refs | Kiểm invariants (mục 6), emit `verification_completed` | Output `day09-l3a-output-v2` |

`get_customer_history` cố ý không gọi: cần `customer_unique_id` mà `get_order`
chỉ trả `customer_id`, gọi thử luôn lỗi `Error executing tool`. Tuân thủ
least-privilege và tránh call invalid.

## 3. A2A protocol

- Correlation: mọi message và trace event mang `case_id` (`cases.py` enforce 1-1
  giữa `case-set.json` và `inputs/*.json`).
- Envelope handoff (logic, không trace payload): `{case_id, from_actor, to_actor,
  handoff_reason}` → trace `event_type=handoff, actor=from, target=to`.
- Thứ tự edges: `task_assigned x3` → `tool_result_consumed` (specialists) →
  `handoff coordinator→policy-agent` → `tool_result_consumed get_policy` →
  `policy_decided` → `handoff policy-agent→verifier` → `verification_completed`.
- DAG một chiều, không vòng lặp. Specialists chạy tuần tự (concurrency 1/case)
  để trace deterministic.
- Timeout: dựa vào `mcp_gateway.py:connect_gateway()` (`connect/write/pool 30s`,
  tổng 300s). Mỗi tool retry tối đa 2 lần (mục 5).
- Chỉ trace sự kiện/decision code quan sát được (`trace-event-v1.schema.json` 7
  loại); không trace prompt hay chain-of-thought.

## 4. Evidence lifecycle

1. `EvidenceGateway.call()` (`mcp_gateway.py`) validate response theo
   `mcp-evidence-response-v1.schema.json`, trả `{evidence_ref, result_hash,
   domain, data}`. Fix tương thích `is_error`/`isError` cho `mcp-types 2.2.0`.
2. Mỗi agent lưu envelope vào `state["evidence"][tool_name]`, gom `evidence_ref`
   vào `state["evidence_refs"]` (scope theo case, không reuse cross-case).
3. Ngay sau mỗi call thành công, emit `tool_result_consumed` với đúng
   `actor/tool_name/evidence_refs` — đảm bảo evidence-to-trace linkage cho điểm
   `workflow` (mean lifecycle coverage + ordering + collaboration + linkage).
4. Map vào output: `output.evidence_refs` = unique tất cả refs (max 30);
   `claim_assessments[].evidence_refs` = subset liên quan (order-claim →
   order/item/seller/product refs; payment-claim → payment refs). Mọi ref trong
   output đều đã xuất hiện trong trace.
5. `primary_issue` = claim topic đầu tiên khác `requested_full_refund`
   (mỗi case có 1 topic chuyên biệt + 1 `requested_full_refund`); map 1-1 với
   `primaryIssue` enum trong `l3a-output-v2.schema.json`.

## 5. Failure policy

| Failure | Retry? | Fallback | Trace event/code |
| --- | --- | --- | --- |
| MCP timeout / transport error | Có, tối đa 2 attempts, idempotent (cùng `case_id` + args) | Bỏ tool đó, tiếp tục tool khác; không bịa data | Không emit `tool_result_consumed` cho call thất bại |
| Tool not found / invalid args (`get_refund_timeline`, `get_customer_history` hay lỗi) | Có, 2 attempts rồi bỏ | Coi như evidence nhóm đó thiếu; giảm confidence | Verifier ghi `verified_with_warnings` nếu thiếu |
| Source conflict (items trùng `order_item_id` khác `freight_value`/`shipping_limit_date`) | Không | Giữ cả 2 evidence, emit 1 `data_conflicts[]` `use_first_observed` | Conflict nằm trong output, không thêm trace |
| Invalid specialist result / rỗng toàn bộ | Không | Output `insufficient_evidence`, `needs_investigation`, confidence 0.4, refund 0 | `policy_decided=insufficient_evidence`, `verification_completed=fallback_insufficient_evidence` |
| `get_policy` thất bại | Có, 2 attempts | Dùng `_FALLBACK_RULES` (snapshot `EC_POLICY_V1`) | Như bình thường, vẫn emit linkage cho các ref còn lại |

Không chuyển missing evidence thành dữ liệu phỏng đoán.

## 6. Verification invariants

Trước finalize, verifier kiểm (observable, không đoán):
- Schema: `case_id` khớp input; `assessment.primary_issue` thuộc enum;
  `confidence` 0–1 (không dùng 1.0 tuyệt đối).
- Entity scope: `order_ids=[claimed_order_id]`, `item_ids`/`seller_ids` trích từ
  evidence item/seller/product, `payment_references={order_id}:{seq}:{type}`,
  `shipment_ids=[order_id]`.
- Evidence ownership: mọi ref khớp `^ev_...$`, thuộc case hiện tại, đã có trong
  `tool_result_consumed`.
- Claim linkage: mỗi `claim_assessments[].evidence_refs` là subset của
  `output.evidence_refs`; claim `requested_full_refund` → `supported` khi
  refund > 0, ngược lại `unsupported`.
- Money totals: `sum(refund_lines.amount_brl) == recommended_refund_brl`;
  `refund_lines` rỗng khi refund = 0; `currency const BRL`.
- Responsibility/action consistency: `responsible_parties` từ policy rule
  (party seller thay bằng seller_id thực tế từ evidence);
  `resolution_actions=[recommended_action]`; `cause_code=PRIMARY_ISSUE.upper()`.
- Confidence bounds: 0.9 khi đủ 4 nhóm (order/payment/shipment/policy), 0.75 khi
  thiếu 1 nhóm, 0.6 khi thiếu nhiều hơn.

## 7. Reproducibility

- Thiết kế: async state-machine phong cách LangGraph, không thêm dependency ngoài
  `pyproject.toml` (`httpx2`, `jsonschema`, `mcp`, `python-dotenv`, `pytest`,
  `ruff`). Tương thích `mcp-types 2.2.0` (`is_error`/`structured_content`).
- Concurrency: 1 case tại một thời điểm (`cli.py` loop tuần tự), specialists
  trong case chạy tuần tự; không random seed (logic deterministic từ MCP +
  policy rules).
- Lệnh chạy: `day09 run` → `day09 validate` → `day09 package --output
  dist/submission.zip`. Output tại `outputs/<case_id>.json`,
  trace tại `traces/trace.jsonl`.
- Giới hạn: mỗi case tối đa 9 MCP calls (4 order + 3 payment + 1 shipment +
  1 policy); `get_refund_timeline` có thể thất bại tùy case là bình thường.
- Không ghi API key, prompt bí mật hay chain-of-thought vào output/trace
  (`submission.py` quét `sk-team-` trước khi package).
