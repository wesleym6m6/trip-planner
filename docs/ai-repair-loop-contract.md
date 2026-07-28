# AI Repair Loop Contract

Status: Phase 2 implementation contract
Contract versions: `planner-snapshot/v1`, `repair-proposal/v1`

## Purpose

The repair loop lets an AI improve a canonical itinerary without giving the
model direct storage access or authority to approve its own changes.

The boundary is intentionally small:

1. the trusted host creates a receipt-free `PlannerSnapshot`;
2. the model returns semantic operations, a registered repair option, and a
   reason tied to current issue IDs;
3. the trusted binder supplies trip ID, base revision, deterministic audit
   operation IDs, AI provenance, and idempotency;
4. the controller previews the exact patch and proves bounded progress;
5. the controller commits only the reviewed effect, after any external
   approval has been supplied.

No database, queue, workflow engine, or provider framework is part of Phase 2.
The controller is one bounded in-memory state machine over a small repository
protocol.

## Model-facing snapshot

`PlannerSnapshot.to_dict()` exposes:

- canonical semantic state;
- fixed UTC `evaluation_at`;
- deterministic `CheckReport`;
- stable repair issue IDs;
- explicit owner and typed repair options;
- remaining iteration, provider-call, and persistent-change budgets.

It does not expose:

- receipts or history;
- generation counters;
- filesystem paths or locks;
- approval grants;
- claimed validation or commit status.

The semantic state digest hashes `plan["state"]`, not revision or generation.
The snapshot ID additionally binds revision, evaluation time, issues, status,
and remaining budgets, so a response cannot silently reuse an older budget
view.

Issue identity excludes prose, severity, fix ordering, and volatile diagnostic
metrics. Possible and definite forms of the same structural issue share one
family and identity. Unknown issue codes or repair options fail closed to
manual review.

## Model-facing proposal

A proposal contains only:

- one to 128 typed `PlanPatch` operations;
- exactly one `OperationReason` per operation;
- current issue IDs;
- one repair `option_key` actually offered by every cited issue;
- human-readable reason and optional summary.

Strict decoding rejects duplicate JSON keys, unknown fields, trusted storage
fields, unsupported operations, malformed reasons, oversized payloads, and
model-authored constraint provenance.

The binder:

- rejects stale or unknown issue references;
- rejects provider, human, system, or blocking options as automatic plan
  patches;
- rewrites model operation IDs to deterministic `ai-op-NNN` audit IDs;
- stamps new constraint provenance as `ai`;
- injects trip ID and base revision;
- derives an exact idempotency key from the bound operations and stored intent.

The semantic effect digest excludes model-chosen operation IDs and rationale.
It is used only to detect repeated attempts; storage replay remains bound to
the exact patch request.

## Preview and commit

`RepairController.submit()` consumes one iteration even when the model response
is malformed or stale. It then:

1. reloads canonical state and refuses implicit rebasing;
2. rejects a repeated semantic attempt on the same state;
3. previews with the run's fixed `evaluation_at`;
4. counts explicit and derived `ChangeRecord` values;
5. requires every cited target issue to disappear;
6. requires a strict lexicographic improvement in:
   status, error count, verification warnings, planner-owned issues, then total
   issues;
7. rejects no-op and previously visited semantic states;
8. stages one exact review.

`RepairController.commit()` reloads and re-previews the same patch. The
diff, risk policy, revision, and budgets must still match the review. For a
normal preview, the candidate semantic state must match too. A Phase 1
protected preview cannot be kernel-evaluated until its exact store grant is
present, so its checkpoint binds the semantic effect, complete diff, reasons,
and approval scope; commit then evaluates the candidate after the grant and
before any write. Only these store outcomes confirm a commit:

- `applied` with `changed=true`;
- exact `replayed` after an outcome-unknown retry.

`no_op`, `replayed_rolled_back`, generic success flags, and an unresolved
`commit_outcome_unknown` never advance the loop or consume the persistent
change budget.

Both `ProposalReview` and `RepairResult` carry the exact readable diff and the
per-operation reasons. This is the Phase 2 audit handoff; durable product-level
audit presentation belongs to the later CLI/interface phase.

## Human checkpoints

Approvals are separate trusted objects and can never appear in model JSON.

The controller requires a `HumanCheckpointGrant` bound to the exact review for
effects outside the conservative automatic policy, including:

- removal of an activity;
- creation or downgrade of fixed/booked decisions;
- creation of fixed-day/fixed-time authority;
- hard-constraint creation or constraint mutation;
- day date, timezone, or availability-bound changes;
- an option/operation or option/field mismatch;
- a patch larger than the automatic per-patch budget.

Phase 1 protected changes independently require an `ApprovalGrant` bound to
the store's exact protected-change scope. When either checkpoint is required,
commit re-previews after receiving the grant; approval never bypasses kernel
validation.

## Budgets and convergence

One run owns monotonic counters for:

- model iterations;
- unique provider-call fingerprints;
- committed `ChangeRecord` values, including derived invalidations;
- automatic changes per patch.

A provider fingerprint is first `reserved`; it becomes a reusable cache hit
only after the host calls `complete_provider_call()`. A second in-flight
reservation is denied, and completed cache hits do not consume provider
budget. Budget values may be zero for inspection-only runs.

The controller remembers semantic attempts and committed states. A repeated
effect is rejected before preview, and a candidate or externally restored
state already visited by the run stops the loop as an oscillation.

## Deferred deliberately

Phase 2 does not:

- call an AI model;
- execute provider queries;
- choose or optimize an itinerary;
- persist a workflow graph;
- automatically pay, book, cancel, or publish;
- replace Phase 1 revision, approval, validation, and atomic-write rules.

Candidate generation and scoring are Phase 3. Provider normalization and
evidence ingestion are Phase 4.
