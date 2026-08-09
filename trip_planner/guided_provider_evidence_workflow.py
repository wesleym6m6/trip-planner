"""Provider-specific Phase 5.32 quarantine assessment and evidence routing.

The bounded executor deliberately leaves every HTTP response private and
untrusted.  This module is the only guided seam that revalidates that exact
quarantine, dispatches it to the matching provider adapter, and then routes
authorized results to the persistence boundary selected by host policy.

Assessment is pure.  Place identity still requires the existing trusted
review finalizer before it can reach the durable EvidenceStore.  Place Details
and Routes remain memory-only and can only enter an EvidenceSession.  SerpAPI
hotel normalization remains a candidate-only, non-provenance DTO and is never
accepted by either evidence boundary.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import fields, is_dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Any, Mapping

from .evidence_session import EvidenceSession, EvidenceSessionLoad, EvidenceSessionMerge
from .evidence_store import EvidenceStore, EvidenceStoreResult
from .facts import AuthorizedProviderResult, EvidenceSnapshot, FactContractError
from .guided_provider_execution import (
    GuidedProviderExecution,
    GuidedProviderQuarantinedResponse,
    revalidate_guided_provider_quarantined_response,
)
from .guided_provider_execution_target_bindings import (
    GuidedProviderExecutionTargetPreimage,
)
from .guided_provider_pre_execution import (
    GuidedProviderPreExecution,
    GuidedProviderPreExecutionContext,
    _SealedSlots,
)
from .guided_provider_request_materialization_review import (
    GuidedProviderRequestMaterializationKind,
)
from .guided_provider_request_send_preparation import (
    GuidedProviderRequestTransportProfile,
)
from .lodging_discovery import (
    LodgingDiscoveryRequest,
    LodgingDiscoveryResult,
    normalize_serpapi_hotel_discovery,
)
from .place_details import (
    GooglePlaceDetailsHttpResponse,
    GooglePlaceDetailsRequest,
    authorize_google_place_details_http_response,
)
from .places_identity import (
    PlaceIdentityRequest,
    PlaceIdentityReview,
    PlaceIdentityReviewAuthority,
    PlaceIdentityReviewGrant,
    PlaceIdentityReviewStatus,
    evaluate_google_place_identity_candidates,
    finalize_google_place_identity_review,
)
from .routes import (
    GoogleRouteRequest,
    GoogleRouteResponseAuthorization,
    GoogleRoutesHttpResponse,
    authorize_google_route_http_response,
)


GUIDED_PROVIDER_EVIDENCE_WORKFLOW_VERSION = (
    "guided-provider-evidence-workflow/v1"
)
_ASSESSMENT_TOKEN = object()
_ITEM_TOKEN = object()
_ROUTING_TOKEN = object()
_DIGEST_RE = re.compile(r"[0-9a-f]{64}")
_IDENTITY_RESPONSE_BYTES = 65_536
_HOTEL_RESPONSE_BYTES = 1_048_576


class GuidedProviderEvidenceItemStatus(str, Enum):
    """Provider-specific result state after quarantine assessment."""

    AUTHORIZED_RESULT = "authorized_result"
    IDENTITY_REVIEW = "identity_review"
    LODGING_CANDIDATES = "lodging_candidates"
    REJECTED = "rejected"


class GuidedProviderEvidenceAssessmentStatus(str, Enum):
    """Aggregate assessment state without implying canonical authority."""

    READY_FOR_EVIDENCE = "ready_for_evidence"
    REVIEW_REQUIRED = "review_required"
    PARTIAL = "partial"
    NO_USABLE_RESULTS = "no_usable_results"


class GuidedProviderEvidenceAssessmentItem(_SealedSlots):
    """One sealed provider-specific output with private normalized content."""

    __slots__ = (
        "request_index",
        "transport_profile",
        "materialization_kind",
        "status",
        "problem_code",
        "_authorized_result",
        "_identity_review",
        "_lodging_result",
        "_route_warnings",
        "_source_response_fingerprint",
        "_item_fingerprint",
    )

    def __init__(
        self,
        *,
        request_index: int,
        transport_profile: GuidedProviderRequestTransportProfile,
        materialization_kind: GuidedProviderRequestMaterializationKind,
        status: GuidedProviderEvidenceItemStatus,
        problem_code: str | None,
        authorized_result: AuthorizedProviderResult | None,
        identity_review: PlaceIdentityReview | None,
        lodging_result: LodgingDiscoveryResult | None,
        route_warnings: tuple[str, ...],
        source_response_fingerprint: str,
        _token: object | None = None,
    ) -> None:
        if _token is not _ITEM_TOKEN:
            raise ValueError("Evidence assessment items require the adapter gate")
        if (
            type(request_index) is not int
            or request_index < 0
            or type(transport_profile)
            is not GuidedProviderRequestTransportProfile
            or type(materialization_kind)
            is not GuidedProviderRequestMaterializationKind
            or type(status) is not GuidedProviderEvidenceItemStatus
            or problem_code is not None
            and (type(problem_code) is not str or not problem_code)
            or authorized_result is not None
            and type(authorized_result) is not AuthorizedProviderResult
            or identity_review is not None
            and type(identity_review) is not PlaceIdentityReview
            or lodging_result is not None
            and type(lodging_result) is not LodgingDiscoveryResult
            or not isinstance(route_warnings, tuple)
            or any(type(item) is not str or not item for item in route_warnings)
            or tuple(sorted(set(route_warnings))) != route_warnings
            or not _DIGEST_RE.fullmatch(source_response_fingerprint)
        ):
            raise ValueError("Evidence assessment item is invalid")
        present = sum(
            value is not None
            for value in (authorized_result, identity_review, lodging_result)
        )
        expected_present = 0 if status is GuidedProviderEvidenceItemStatus.REJECTED else 1
        if present != expected_present:
            raise ValueError("Evidence assessment item content differs from status")
        if status is GuidedProviderEvidenceItemStatus.REJECTED:
            if problem_code is None or route_warnings:
                raise ValueError("Rejected evidence item requires one safe problem")
        elif problem_code is not None:
            raise ValueError("Usable evidence item cannot carry a rejection problem")
        if (
            status is GuidedProviderEvidenceItemStatus.AUTHORIZED_RESULT
        ) is not (authorized_result is not None):
            raise ValueError("Authorized result differs from item status")
        if (
            status is GuidedProviderEvidenceItemStatus.IDENTITY_REVIEW
        ) is not (identity_review is not None):
            raise ValueError("Identity review differs from item status")
        if (
            status is GuidedProviderEvidenceItemStatus.LODGING_CANDIDATES
        ) is not (lodging_result is not None):
            raise ValueError("Lodging candidates differ from item status")
        self.request_index = request_index
        self.transport_profile = transport_profile
        self.materialization_kind = materialization_kind
        self.status = status
        self.problem_code = problem_code
        self._authorized_result = authorized_result
        self._identity_review = identity_review
        self._lodging_result = lodging_result
        self._route_warnings = route_warnings
        self._source_response_fingerprint = source_response_fingerprint
        self._item_fingerprint = _assessment_item_fingerprint(self)
        self._seal()

    def __repr__(self) -> str:
        return (
            "GuidedProviderEvidenceAssessmentItem("
            f"request_index={self.request_index!r}, "
            f"materialization_kind={self.materialization_kind.value!r}, "
            f"status={self.status.value!r})"
        )

    @property
    def requires_user_review(self) -> bool:
        if self._lodging_result is not None:
            return bool(self._lodging_result.candidates)
        return bool(
            self._identity_review is not None
            and self._identity_review.status
            is PlaceIdentityReviewStatus.REVIEW_REQUIRED
        )

    @property
    def supports_evidence_routing(self) -> bool:
        if self.status is GuidedProviderEvidenceItemStatus.AUTHORIZED_RESULT:
            return True
        return bool(
            self._identity_review is not None
            and self._identity_review.status
            in {
                PlaceIdentityReviewStatus.READY,
                PlaceIdentityReviewStatus.REVIEW_REQUIRED,
            }
        )

    def to_safe_dict(self) -> dict[str, object]:
        _validate_assessment_item(self)
        result: dict[str, object] = {
            "request_index": self.request_index,
            "transport_profile": self.transport_profile.value,
            "materialization_kind": self.materialization_kind.value,
            "status": self.status.value,
            "problem_code": self.problem_code,
            "requires_user_review": self.requires_user_review,
            "supports_evidence_routing": self.supports_evidence_routing,
            "authorized_result_private": self._authorized_result is not None,
            "identity_review_private": self._identity_review is not None,
            "lodging_candidates_private": self._lodging_result is not None,
            "route_warning_count": len(self._route_warnings),
            "raw_provider_values_exposed": False,
            "source_fingerprint_exposed": False,
            "canonical_authority": False,
        }
        if self._identity_review is not None:
            result["identity_review_status"] = self._identity_review.status.value
            result["identity_candidate_count"] = len(
                self._identity_review.assessments
            )
        if self._lodging_result is not None:
            result["lodging_status"] = self._lodging_result.status.value
            result["lodging_candidate_count"] = len(
                self._lodging_result.candidates
            )
        return result

    def __reduce_ex__(self, protocol: int) -> object:
        del protocol
        raise TypeError("Evidence assessment items are non-serializable")


class GuidedProviderEvidenceAssessment(_SealedSlots):
    """Pure provider-specific assessment over one exact execution."""

    __slots__ = (
        "status",
        "next_action",
        "_items",
        "_assessment_fingerprint",
    )

    def __init__(
        self,
        *,
        status: GuidedProviderEvidenceAssessmentStatus,
        next_action: str,
        items: tuple[GuidedProviderEvidenceAssessmentItem, ...],
        _token: object | None = None,
    ) -> None:
        if _token is not _ASSESSMENT_TOKEN:
            raise ValueError("Evidence assessments require provider adapters")
        if (
            type(status) is not GuidedProviderEvidenceAssessmentStatus
            or type(next_action) is not str
            or not next_action
            or not isinstance(items, tuple)
            or not items
            or any(
                type(item) is not GuidedProviderEvidenceAssessmentItem
                for item in items
            )
            or tuple(item.request_index for item in items)
            != tuple(range(len(items)))
        ):
            raise ValueError("Evidence assessment aggregate is invalid")
        self.status = status
        self.next_action = next_action
        self._items = items
        if _assessment_branch(items) != (status, next_action):
            raise ValueError("Evidence assessment aggregate branch is invalid")
        self._assessment_fingerprint = _assessment_fingerprint(
            status,
            next_action,
            items,
        )
        self._seal()

    @property
    def item_count(self) -> int:
        return len(self._items)

    @property
    def requires_user_review(self) -> bool:
        return any(item.requires_user_review for item in self._items)

    def identity_review(self, request_index: int) -> PlaceIdentityReview:
        """Return exact private review content for an explicit host review UI."""

        item = self._validated_item(request_index)
        if item._identity_review is None:
            raise ValueError("Request does not contain a place identity review")
        return item._identity_review

    def identity_review_payload(self, request_index: int) -> dict[str, Any]:
        return self.identity_review(request_index).to_review_payload()

    def lodging_review_payload(self, request_index: int) -> dict[str, Any]:
        """Return candidate-only hotel content for an explicit selection UI."""

        item = self._validated_item(request_index)
        if item._lodging_result is None:
            raise ValueError("Request does not contain lodging candidates")
        return item._lodging_result.to_dict()

    def route_warnings(self, request_index: int) -> tuple[str, ...]:
        return self._validated_item(request_index)._route_warnings

    def _validated_item(
        self,
        request_index: int,
    ) -> GuidedProviderEvidenceAssessmentItem:
        _validate_assessment(self)
        if type(request_index) is not int or not 0 <= request_index < len(
            self._items
        ):
            raise ValueError("Evidence assessment request index is invalid")
        return self._items[request_index]

    def to_dict(self) -> dict[str, Any]:
        _validate_assessment(self)
        counts = {
            status.value: sum(item.status is status for item in self._items)
            for status in GuidedProviderEvidenceItemStatus
        }
        return {
            "contract_version": GUIDED_PROVIDER_EVIDENCE_WORKFLOW_VERSION,
            "status": self.status.value,
            "next_action": self.next_action,
            "requires_user_review": self.requires_user_review,
            "item_count": self.item_count,
            "item_counts": counts,
            "items": [item.to_safe_dict() for item in self._items],
            "raw_provider_values_exposed": False,
            "evidence_merged": False,
            "canonical_mutation_created": False,
            "writes_to_trip": False,
            "supports_direct_canonical_use": False,
        }

    def __repr__(self) -> str:
        return (
            "GuidedProviderEvidenceAssessment("
            f"status={self.status.value!r}, item_count={self.item_count!r})"
        )

    def __reduce_ex__(self, protocol: int) -> object:
        del protocol
        raise TypeError("Evidence assessments are non-serializable")


class GuidedProviderMemoryEvidenceRouting(_SealedSlots):
    """Typed memory-only merge outcome; never a canonical mutation grant."""

    __slots__ = ("current", "_merges")

    def __init__(
        self,
        *,
        current: EvidenceSessionLoad,
        merges: tuple[EvidenceSessionMerge, ...],
        _token: object | None = None,
    ) -> None:
        if _token is not _ROUTING_TOKEN:
            raise ValueError("Memory evidence routing requires the workflow gate")
        if (
            type(current) is not EvidenceSessionLoad
            or not isinstance(merges, tuple)
            or any(type(item) is not EvidenceSessionMerge for item in merges)
            or merges
            and merges[-1].current != current
        ):
            raise ValueError("Memory evidence routing result is invalid")
        self.current = current
        self._merges = merges
        self._seal()

    @property
    def merge_count(self) -> int:
        return len(self._merges)

    def to_dict(self) -> dict[str, Any]:
        return {
            "contract_version": GUIDED_PROVIDER_EVIDENCE_WORKFLOW_VERSION,
            "status": "memory_evidence_routed",
            "merge_count": self.merge_count,
            "current": self.current.to_dict(),
            "writes_to_disk": False,
            "writes_to_trip": False,
            "canonical_authority": False,
        }

    def __reduce_ex__(self, protocol: int) -> object:
        del protocol
        raise TypeError("Memory evidence routing is non-serializable")


def assess_guided_provider_quarantined_responses(
    context: GuidedProviderPreExecutionContext,
    pre_execution: GuidedProviderPreExecution,
    execution: GuidedProviderExecution,
    *,
    preimages: tuple[GuidedProviderExecutionTargetPreimage, ...],
    evaluation_at: datetime,
) -> GuidedProviderEvidenceAssessment:
    """Revalidate and assess every exact quarantined response without writes."""

    if (
        type(context) is not GuidedProviderPreExecutionContext
        or type(pre_execution) is not GuidedProviderPreExecution
        or type(execution) is not GuidedProviderExecution
    ):
        raise TypeError("Evidence workflow sources must be exact")
    evaluated = _aware_utc(evaluation_at, "evaluation_at")
    if evaluated < execution._completed_at:
        raise ValueError("Evidence assessment clock rolled back")
    items: list[GuidedProviderEvidenceAssessmentItem] = []
    for index, outcome in enumerate(execution._outcomes):
        quarantine = outcome._quarantine
        if quarantine is None:
            items.append(
                _rejected_item(
                    request_index=index,
                    transport_profile=outcome.transport_profile,
                    materialization_kind=(
                        pre_execution._requests[index].materialization_kind
                    ),
                    problem_code="provider_response_not_quarantined",
                    source_response_fingerprint=("0" * 64),
                )
            )
            continue
        revalidate_guided_provider_quarantined_response(
            context,
            pre_execution,
            execution,
            quarantine,
            preimages=preimages,
            evaluation_at=evaluated,
        )
        items.append(_assess_quarantine(quarantine))
    status, next_action = _assessment_branch(tuple(items))
    return GuidedProviderEvidenceAssessment(
        status=status,
        next_action=next_action,
        items=tuple(items),
        _token=_ASSESSMENT_TOKEN,
    )


def finalize_guided_provider_identity_evidence(
    assessment: GuidedProviderEvidenceAssessment,
    request_index: int,
    current_snapshot: EvidenceSnapshot,
    authority: PlaceIdentityReviewAuthority,
    grant: PlaceIdentityReviewGrant | None = None,
) -> AuthorizedProviderResult:
    """Apply the existing exact identity review gate; this performs no write."""

    if (
        type(assessment) is not GuidedProviderEvidenceAssessment
        or type(current_snapshot) is not EvidenceSnapshot
        or type(authority) is not PlaceIdentityReviewAuthority
        or grant is not None
        and type(grant) is not PlaceIdentityReviewGrant
    ):
        raise TypeError("Identity evidence finalization sources must be exact")
    item = assessment._validated_item(request_index)
    review = item._identity_review
    if review is None:
        raise FactContractError(
            "PENDING_REVIEW",
            "Selected assessment item is not a place identity review.",
        )
    return finalize_google_place_identity_review(
        review,
        current_snapshot,
        authority,
        grant,
    )


def merge_guided_provider_identity_evidence(
    assessment: GuidedProviderEvidenceAssessment,
    request_index: int,
    store: EvidenceStore,
    current_snapshot: EvidenceSnapshot,
    authority: PlaceIdentityReviewAuthority,
    grant: PlaceIdentityReviewGrant | None = None,
) -> EvidenceStoreResult:
    """Finalize one exact identity review and route it to durable evidence."""

    if type(store) is not EvidenceStore:
        raise TypeError("Identity evidence routing requires an EvidenceStore")
    authorized = finalize_guided_provider_identity_evidence(
        assessment,
        request_index,
        current_snapshot,
        authority,
        grant,
    )
    return store.merge(
        authorized,
        expected_revision=current_snapshot.store_revision,
    )


def route_guided_provider_memory_evidence(
    assessment: GuidedProviderEvidenceAssessment,
    session: EvidenceSession,
) -> GuidedProviderMemoryEvidenceRouting:
    """Merge only dedicated Details/Routes results into one run session."""

    if (
        type(assessment) is not GuidedProviderEvidenceAssessment
        or type(session) is not EvidenceSession
    ):
        raise TypeError("Memory evidence routing sources must be exact")
    _validate_assessment(assessment)
    merges: list[EvidenceSessionMerge] = []
    for item in assessment._items:
        if item.materialization_kind not in {
            GuidedProviderRequestMaterializationKind.GOOGLE_PLACE_DETAILS,
            GuidedProviderRequestMaterializationKind.GOOGLE_ROUTES_COMPUTE_ROUTES,
        }:
            continue
        if item._authorized_result is None:
            continue
        merges.append(session.merge(item._authorized_result))
    current = merges[-1].current if merges else session.load()
    return GuidedProviderMemoryEvidenceRouting(
        current=current,
        merges=tuple(merges),
        _token=_ROUTING_TOKEN,
    )


def _assess_quarantine(
    quarantine: GuidedProviderQuarantinedResponse,
) -> GuidedProviderEvidenceAssessmentItem:
    kind = quarantine.materialization_kind
    common = {
        "request_index": quarantine.request_index,
        "transport_profile": quarantine.transport_profile,
        "materialization_kind": kind,
        "source_response_fingerprint": quarantine._response_fingerprint,
    }
    target = quarantine._normalization_target
    try:
        if kind is GuidedProviderRequestMaterializationKind.GOOGLE_PLACES_TEXT_SEARCH:
            if type(target) is not PlaceIdentityRequest:
                raise ValueError
            if not 200 <= quarantine.status_code <= 299:
                return _rejected_item(
                    **common,
                    problem_code="provider_http_status_rejected",
                )
            raw = _strict_json_object(
                quarantine._body,
                maximum_bytes=_IDENTITY_RESPONSE_BYTES,
                maximum_nodes=512,
                maximum_object_fields=64,
                maximum_list_items=128,
                maximum_text=4_096,
            )
            review = evaluate_google_place_identity_candidates(
                target,
                raw,
                completed_at=quarantine.retrieved_at,
                attempts_used=quarantine.attempt_number,
            )
            return GuidedProviderEvidenceAssessmentItem(
                **common,
                status=GuidedProviderEvidenceItemStatus.IDENTITY_REVIEW,
                problem_code=None,
                authorized_result=None,
                identity_review=review,
                lodging_result=None,
                route_warnings=(),
                _token=_ITEM_TOKEN,
            )
        if kind is GuidedProviderRequestMaterializationKind.GOOGLE_PLACE_DETAILS:
            if type(target) is not GooglePlaceDetailsRequest:
                raise ValueError
            adapted = authorize_google_place_details_http_response(
                target,
                GooglePlaceDetailsHttpResponse(
                    status_code=quarantine.status_code,
                    body=quarantine._body,
                    headers=quarantine._headers,
                ),
                sent_at=quarantine._sent_at,
                completed_at=quarantine.retrieved_at,
                attempts_used=quarantine.attempt_number,
            )
            return GuidedProviderEvidenceAssessmentItem(
                **common,
                status=GuidedProviderEvidenceItemStatus.AUTHORIZED_RESULT,
                problem_code=None,
                authorized_result=adapted,
                identity_review=None,
                lodging_result=None,
                route_warnings=(),
                _token=_ITEM_TOKEN,
            )
        if kind is (
            GuidedProviderRequestMaterializationKind
            .GOOGLE_ROUTES_COMPUTE_ROUTES
        ):
            if type(target) is not GoogleRouteRequest:
                raise ValueError
            adapted_route = authorize_google_route_http_response(
                target,
                GoogleRoutesHttpResponse(
                    status_code=quarantine.status_code,
                    body=quarantine._body,
                    headers=quarantine._headers,
                ),
                sent_at=quarantine._sent_at,
                completed_at=quarantine.retrieved_at,
                attempts_used=quarantine.attempt_number,
            )
            return GuidedProviderEvidenceAssessmentItem(
                **common,
                status=GuidedProviderEvidenceItemStatus.AUTHORIZED_RESULT,
                problem_code=None,
                authorized_result=adapted_route.result,
                identity_review=None,
                lodging_result=None,
                route_warnings=adapted_route.warnings,
                _token=_ITEM_TOKEN,
            )
        if kind is GuidedProviderRequestMaterializationKind.SERPAPI_GOOGLE_HOTELS:
            if type(target) is not LodgingDiscoveryRequest:
                raise ValueError
            if not 200 <= quarantine.status_code <= 299:
                return _rejected_item(
                    **common,
                    problem_code="provider_http_status_rejected",
                )
            raw = _strict_json_object(
                quarantine._body,
                maximum_bytes=_HOTEL_RESPONSE_BYTES,
                maximum_nodes=32_768,
                maximum_object_fields=128,
                maximum_list_items=256,
                maximum_text=2_048,
            )
            lodging = normalize_serpapi_hotel_discovery(
                target,
                raw,
                completed_at=quarantine.retrieved_at,
            )
            return GuidedProviderEvidenceAssessmentItem(
                **common,
                status=GuidedProviderEvidenceItemStatus.LODGING_CANDIDATES,
                problem_code=None,
                authorized_result=None,
                identity_review=None,
                lodging_result=lodging,
                route_warnings=(),
                _token=_ITEM_TOKEN,
            )
    except (FactContractError, TypeError, ValueError):
        return _rejected_item(
            **common,
            problem_code="provider_adapter_rejected_response",
        )
    return _rejected_item(
        **common,
        problem_code="unsupported_provider_materialization",
    )


def _rejected_item(
    *,
    request_index: int,
    transport_profile: GuidedProviderRequestTransportProfile,
    materialization_kind: GuidedProviderRequestMaterializationKind,
    problem_code: str,
    source_response_fingerprint: str,
) -> GuidedProviderEvidenceAssessmentItem:
    return GuidedProviderEvidenceAssessmentItem(
        request_index=request_index,
        transport_profile=transport_profile,
        materialization_kind=materialization_kind,
        status=GuidedProviderEvidenceItemStatus.REJECTED,
        problem_code=problem_code,
        authorized_result=None,
        identity_review=None,
        lodging_result=None,
        route_warnings=(),
        source_response_fingerprint=source_response_fingerprint,
        _token=_ITEM_TOKEN,
    )


def _assessment_branch(
    items: tuple[GuidedProviderEvidenceAssessmentItem, ...],
) -> tuple[GuidedProviderEvidenceAssessmentStatus, str]:
    rejected = any(
        item.status is GuidedProviderEvidenceItemStatus.REJECTED
        for item in items
    )
    usable = any(
        item.supports_evidence_routing
        or item._lodging_result is not None
        and bool(item._lodging_result.candidates)
        for item in items
    )
    review = any(item.requires_user_review for item in items)
    if rejected and usable:
        return (
            GuidedProviderEvidenceAssessmentStatus.PARTIAL,
            "review_partial_provider_specific_results",
        )
    if not usable:
        return (
            GuidedProviderEvidenceAssessmentStatus.NO_USABLE_RESULTS,
            "rebuild_or_stop_provider_workflow",
        )
    if review:
        return (
            GuidedProviderEvidenceAssessmentStatus.REVIEW_REQUIRED,
            "review_provider_specific_candidates",
        )
    return (
        GuidedProviderEvidenceAssessmentStatus.READY_FOR_EVIDENCE,
        "route_source_backed_evidence",
    )


def _strict_json_object(
    body: bytes,
    *,
    maximum_bytes: int,
    maximum_nodes: int,
    maximum_object_fields: int,
    maximum_list_items: int,
    maximum_text: int,
) -> dict[str, Any]:
    if type(body) is not bytes or len(body) > maximum_bytes or not body.strip():
        raise ValueError("Provider response body is outside adapter bounds")

    def pairs(values: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in values:
            if key in result:
                raise ValueError("Provider response contains duplicate keys")
            result[key] = value
        return result

    try:
        decoded = json.loads(
            body.decode("utf-8"),
            object_pairs_hook=pairs,
            parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()),
        )
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        RecursionError,
        ValueError,
    ):
        raise ValueError("Provider response is not strict JSON") from None
    if not isinstance(decoded, dict):
        raise ValueError("Provider response root must be an object")
    nodes = [0]
    _validate_json_tree(
        decoded,
        depth=0,
        nodes=nodes,
        maximum_nodes=maximum_nodes,
        maximum_object_fields=maximum_object_fields,
        maximum_list_items=maximum_list_items,
        maximum_text=maximum_text,
    )
    return decoded


def _validate_json_tree(
    value: object,
    *,
    depth: int,
    nodes: list[int],
    maximum_nodes: int,
    maximum_object_fields: int,
    maximum_list_items: int,
    maximum_text: int,
) -> None:
    nodes[0] += 1
    if depth > 12 or nodes[0] > maximum_nodes:
        raise ValueError("Provider response JSON exceeds structural bounds")
    if isinstance(value, dict):
        if len(value) > maximum_object_fields:
            raise ValueError("Provider response object exceeds its field bound")
        for key, item in value.items():
            if type(key) is not str or len(key) > 256:
                raise ValueError("Provider response object key is invalid")
            _validate_json_tree(
                item,
                depth=depth + 1,
                nodes=nodes,
                maximum_nodes=maximum_nodes,
                maximum_object_fields=maximum_object_fields,
                maximum_list_items=maximum_list_items,
                maximum_text=maximum_text,
            )
        return
    if isinstance(value, list):
        if len(value) > maximum_list_items:
            raise ValueError("Provider response list exceeds its item bound")
        for item in value:
            _validate_json_tree(
                item,
                depth=depth + 1,
                nodes=nodes,
                maximum_nodes=maximum_nodes,
                maximum_object_fields=maximum_object_fields,
                maximum_list_items=maximum_list_items,
                maximum_text=maximum_text,
            )
        return
    if type(value) is str:
        if len(value) > maximum_text:
            raise ValueError("Provider response text exceeds its bound")
        return
    if type(value) is float and not math.isfinite(value):
        raise ValueError("Provider response number must be finite")
    if value is not None and type(value) not in {bool, int, float}:
        raise ValueError("Provider response JSON contains an invalid value")


def _aware_utc(value: datetime, name: str) -> datetime:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise ValueError(f"{name} must be a timezone-aware datetime")
    return value.astimezone(timezone.utc)


def _validate_assessment(
    assessment: GuidedProviderEvidenceAssessment,
) -> None:
    if (
        type(assessment) is not GuidedProviderEvidenceAssessment
        or type(assessment.status) is not GuidedProviderEvidenceAssessmentStatus
        or type(assessment.next_action) is not str
        or _assessment_branch(assessment._items)
        != (assessment.status, assessment.next_action)
        or _assessment_fingerprint(
            assessment.status,
            assessment.next_action,
            assessment._items,
        )
        != assessment._assessment_fingerprint
    ):
        raise ValueError("Evidence assessment no longer matches its sources")


def _assessment_fingerprint(
    status: GuidedProviderEvidenceAssessmentStatus,
    next_action: str,
    items: tuple[GuidedProviderEvidenceAssessmentItem, ...],
) -> str:
    return _sha256(
        {
            "contract_version": GUIDED_PROVIDER_EVIDENCE_WORKFLOW_VERSION,
            "domain": "guided-provider-evidence-assessment",
            "status": status,
            "next_action": next_action,
            "items": [_assessment_item_fingerprint(item) for item in items],
        }
    )


def _validate_assessment_item(
    item: GuidedProviderEvidenceAssessmentItem,
) -> None:
    if (
        type(item) is not GuidedProviderEvidenceAssessmentItem
        or _assessment_item_fingerprint(item) != item._item_fingerprint
    ):
        raise ValueError("Evidence assessment item no longer matches its source")


def _assessment_item_fingerprint(
    item: GuidedProviderEvidenceAssessmentItem,
) -> str:
    return _sha256(
        {
            "contract_version": GUIDED_PROVIDER_EVIDENCE_WORKFLOW_VERSION,
            "domain": "guided-provider-evidence-assessment-item",
            "request_index": item.request_index,
            "transport_profile": item.transport_profile,
            "materialization_kind": item.materialization_kind,
            "status": item.status,
            "problem_code": item.problem_code,
            "authorized_result": _private_value(item._authorized_result),
            "identity_review": _private_value(item._identity_review),
            "lodging_result": _lodging_private_value(item._lodging_result),
            "route_warnings": item._route_warnings,
            "source_response_fingerprint": item._source_response_fingerprint,
        }
    )


def _lodging_private_value(value: LodgingDiscoveryResult | None) -> object:
    if value is None:
        return None
    return {
        "request_query": value.request.query,
        "projection": value.to_dict(),
    }


def _private_value(value: object) -> object:
    if value is None or type(value) in {str, int, bool}:
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError("Private binding contains a non-finite number")
        return value
    if type(value) is Decimal:
        return {"decimal": str(value)}
    if type(value) is bytes:
        return {
            "bytes_length": len(value),
            "bytes_sha256": hashlib.sha256(value).hexdigest(),
        }
    if isinstance(value, Enum):
        return {
            "enum": f"{type(value).__module__}.{type(value).__qualname__}",
            "value": _private_value(value.value),
        }
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Private binding datetime must be timezone-aware")
        return value.astimezone(timezone.utc).isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {
            str(key): _private_value(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (tuple, list)):
        return [_private_value(item) for item in value]
    if is_dataclass(value) and not isinstance(value, type):
        return {
            "dataclass": f"{type(value).__module__}.{type(value).__qualname__}",
            "fields": {
                item.name: _private_value(getattr(value, item.name))
                for item in fields(value)
            },
        }
    raise TypeError("Private evidence binding contains an unsupported value")


def _sha256(value: object) -> str:
    encoded = json.dumps(
        _private_value(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


__all__ = [
    "GUIDED_PROVIDER_EVIDENCE_WORKFLOW_VERSION",
    "GuidedProviderEvidenceAssessment",
    "GuidedProviderEvidenceAssessmentItem",
    "GuidedProviderEvidenceAssessmentStatus",
    "GuidedProviderEvidenceItemStatus",
    "GuidedProviderMemoryEvidenceRouting",
    "assess_guided_provider_quarantined_responses",
    "finalize_guided_provider_identity_evidence",
    "merge_guided_provider_identity_evidence",
    "route_guided_provider_memory_evidence",
]
