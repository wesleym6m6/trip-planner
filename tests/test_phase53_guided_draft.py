"""Phase 5.3 contracts for private guided future-trip drafts."""

from __future__ import annotations

import inspect
import json
import unittest
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone

import trip_planner
import trip_planner.guided_draft as guided_draft_module
from trip_planner.guided_draft import (
    BriefKnownState,
    BriefTextFact,
    DateSpanDraft,
    DestinationDraft,
    GuidedDraftStatus,
    GuidedQuestionCode,
    TripBriefDraft,
    assess_guided_draft,
)
from trip_planner.lodging import (
    LocationHint,
    LocationHintKind,
    LodgingIntentDraft,
    LodgingRequirement,
    ReportedDecisionClaim,
    TransportBoundaryDraft,
    TransportBoundaryKind,
    bind_lodging_candidate,
    bind_transport_boundary,
)
from trip_planner.models import DecisionState, EvidenceState


UTC = timezone.utc
START = date(2026, 10, 12)
END = date(2026, 10, 16)
PRIVATE = "private-address-url-price-token-place-id-never-serialize"


def _destination(
    value: str = "Ishigaki private destination sentinel",
    *,
    state: BriefKnownState = BriefKnownState.USER_STATED,
) -> DestinationDraft:
    return DestinationDraft(
        location=LocationHint(
            kind=LocationHintKind.AREA,
            label=value,
            input_text=value,
            country_code="jp",
        ),
        state=state,
    )


def _exact_dates(
    *,
    state: BriefKnownState = BriefKnownState.USER_STATED,
) -> DateSpanDraft:
    return DateSpanDraft(state=state, start=START, end=END)


def _transport_boundary() -> object:
    return bind_transport_boundary(
        TransportBoundaryDraft(
            kind=TransportBoundaryKind.ARRIVAL,
            location=LocationHint(
                kind=LocationHintKind.LOCATION_ID,
                label="Private arrival location",
                location_id="arrival-private-node",
                country_code="jp",
            ),
            exact_at=datetime(2026, 10, 12, 14, 30, tzinfo=UTC),
            reported_decision=ReportedDecisionClaim(
                decision_state=DecisionState.FIXED,
                source_ref="private-user-turn-transport",
            ),
        )
    )


def _lodging_candidate(
    *,
    decision: DecisionState = DecisionState.BOOKED,
    location_id: str = "private-stay-a",
) -> object:
    return bind_lodging_candidate(
        LodgingIntentDraft(
            kind=trip_planner.LodgingKind.HOTEL,
            label="Private hotel label URL=" + PRIVATE,
            location=LocationHint(
                kind=LocationHintKind.ADDRESS,
                label="Private hotel address",
                input_text=PRIVATE,
                country_code="jp",
            ),
            check_in=START,
            check_out=END,
            price_amount_minor=987_654,
            currency="JPY",
            price_basis=trip_planner.PriceBasis.TOTAL,
            reported_decision=ReportedDecisionClaim(
                decision_state=decision,
                source_ref="private-user-turn-lodging-" + location_id,
            ),
        )
    )


class GuidedDraftTests(unittest.TestCase):
    def test_public_api_is_pure_and_has_no_promotion_or_cli(self) -> None:
        self.assertTrue(hasattr(trip_planner, "TripBriefDraft"))
        self.assertTrue(hasattr(trip_planner, "assess_guided_draft"))
        self.assertIn("TripBriefDraft", trip_planner.__all__)
        self.assertIn("assess_guided_draft", trip_planner.__all__)
        for unsupported_name in (
            "parse_trip_brief",
            "create_trip_from_brief",
            "apply_guided_draft",
            "confirm_guided_draft",
            "guided_draft_cli",
        ):
            self.assertFalse(hasattr(trip_planner, unsupported_name))

        source = inspect.getsource(guided_draft_module)
        for forbidden_import in (
            "from .tripctl",
            "from .evidence_store",
            "from .facts",
            "import subprocess",
            "import pathlib",
        ):
            self.assertNotIn(forbidden_import, source)

    def test_review_cannot_be_replaced_with_private_output(self) -> None:
        review = assess_guided_draft(TripBriefDraft())

        with self.assertRaises(ValueError):
            replace(
                review,
                lodging_advice=("PRIVATE-RAW-SENTINEL",),
            )
        with self.assertRaises(ValueError):
            replace(
                review,
                status=GuidedDraftStatus.READY_FOR_PROPOSAL,
                next_question=None,
                next_action="prepare_candidate_proposal",
            )

    def test_empty_brief_asks_only_for_destination(self) -> None:
        review = assess_guided_draft(TripBriefDraft())
        safe = review.to_dict()

        self.assertEqual(GuidedDraftStatus.NEEDS_INPUT, review.status)
        self.assertEqual(GuidedQuestionCode.DESTINATION, review.next_question.code)
        self.assertEqual("capture_destination", safe["next_action"])
        self.assertEqual("deferred_until_exact_dates", safe["lodging"]["assessment_status"])
        self.assertEqual(
            {
                "process_local": True,
                "writes_to_trip": False,
                "provider_calls": False,
                "rendered": False,
                "deployed": False,
            },
            safe["side_effects"],
        )

    def test_destination_only_asks_one_dates_question(self) -> None:
        review = assess_guided_draft(TripBriefDraft(destination=_destination()))
        safe = review.to_dict()

        self.assertEqual(GuidedDraftStatus.NEEDS_INPUT, review.status)
        self.assertEqual(GuidedQuestionCode.DATES, review.next_question.code)
        self.assertEqual("capture_date_span", safe["next_action"])
        self.assertTrue(safe["brief"]["destination"]["provided"])
        self.assertEqual("user_stated", safe["brief"]["destination"]["state"])
        self.assertEqual("approximate", safe["brief"]["destination"]["precision"])

    def test_tentative_date_hint_is_not_promoted_to_dates(self) -> None:
        date_hint = "十月初 private date hint must not become a date"
        review = assess_guided_draft(
            TripBriefDraft(
                destination=_destination(),
                dates=DateSpanDraft(
                    state=BriefKnownState.TENTATIVE,
                    hint=date_hint,
                ),
            )
        )
        safe = review.to_dict()

        self.assertEqual(GuidedQuestionCode.DATES, review.next_question.code)
        self.assertFalse(safe["brief"]["dates"]["has_exact_span"])
        self.assertIsNone(safe["brief"]["dates"]["overnight_count"])
        self.assertEqual(["dates"], safe["tentative_fields"])
        self.assertNotIn(date_hint, json.dumps(safe, ensure_ascii=False))

    def test_exact_dates_are_ready_for_candidate_proposal_not_confirmation(self) -> None:
        review = assess_guided_draft(
            TripBriefDraft(
                destination=_destination(),
                dates=_exact_dates(),
                pace=BriefTextFact(
                    state=BriefKnownState.TENTATIVE,
                    value="慢遊，避免每天排太滿",
                ),
                must_do=(
                    BriefTextFact(
                        state=BriefKnownState.USER_STATED,
                        value="竹富島",
                    ),
                    BriefTextFact(
                        state=BriefKnownState.USER_STATED,
                        value="潛水",
                    ),
                ),
            )
        )
        safe = review.to_dict()

        self.assertEqual(GuidedDraftStatus.READY_FOR_PROPOSAL, review.status)
        self.assertIsNone(review.next_question)
        self.assertEqual("prepare_candidate_proposal", safe["next_action"])
        self.assertTrue(safe["proposal_requires_user_review"])
        self.assertEqual(4, safe["brief"]["dates"]["overnight_count"])
        self.assertEqual("missing", safe["lodging"]["assessment_status"])
        self.assertEqual(
            {"user_stated": 2, "tentative": 0},
            safe["brief"]["must_do"],
        )
        self.assertIn("pace", safe["tentative_fields"])

    def test_reported_fixed_transport_remains_candidate_and_unverified(self) -> None:
        boundary = _transport_boundary()
        review = assess_guided_draft(
            TripBriefDraft(
                destination=_destination(),
                dates=_exact_dates(),
                transport_boundaries=(boundary,),
            )
        )
        safe = review.to_dict()

        self.assertEqual(DecisionState.CANDIDATE, boundary.decision_state)
        self.assertEqual("candidate", safe["transport"]["decision_state"])
        self.assertEqual("unverified", safe["transport"]["evidence_state"])
        self.assertEqual(1, safe["transport"]["reported_decision_claim_count"])
        self.assertEqual(["destination_location", "transport_boundaries"], safe["needs_verification"])
        self.assertNotIn("2026-10-12T14:30", json.dumps(safe))
        self.assertNotIn("private-user-turn-transport", json.dumps(safe))

    def test_reported_booked_lodging_stays_candidate_unverified(self) -> None:
        candidate = _lodging_candidate()
        review = assess_guided_draft(
            TripBriefDraft(
                destination=_destination(),
                dates=_exact_dates(),
                lodging_requirement=LodgingRequirement.REQUIRED,
                lodging_candidates=(candidate,),
            )
        )
        safe = review.to_dict()

        self.assertEqual(DecisionState.CANDIDATE, candidate.decision_state)
        self.assertEqual(EvidenceState.UNVERIFIED, candidate.evidence_state)
        self.assertEqual("awaiting_confirmation", safe["lodging"]["assessment_status"])
        self.assertTrue(safe["lodging"]["needs_verification"])
        self.assertEqual(1, safe["lodging"]["reported_decision_claim_count"])
        self.assertIn("lodging", safe["needs_verification"])

    def test_conflicting_lodging_claims_are_preserved_without_auto_selection(self) -> None:
        first = _lodging_candidate(
            decision=DecisionState.SELECTED,
            location_id="private-stay-first",
        )
        second = _lodging_candidate(
            decision=DecisionState.SELECTED,
            location_id="private-stay-second",
        )
        review = assess_guided_draft(
            TripBriefDraft(
                destination=_destination(),
                dates=_exact_dates(),
                lodging_requirement=LodgingRequirement.REQUIRED,
                lodging_candidates=(second, first),
            )
        )
        safe = review.to_dict()

        self.assertEqual(GuidedDraftStatus.READY_FOR_PROPOSAL, review.status)
        self.assertEqual("conflicted", safe["lodging"]["assessment_status"])
        self.assertIn("LODGING_DECISION_CLAIM_CONFLICT", safe["lodging"]["issue_codes"])
        self.assertIn("lodging", safe["needs_verification"])
        self.assertEqual(2, safe["lodging"]["candidate_count"])
        self.assertEqual(DecisionState.CANDIDATE, first.decision_state)
        self.assertEqual(DecisionState.CANDIDATE, second.decision_state)

    def test_safe_review_and_repr_redact_all_private_values(self) -> None:
        private_date_hint = "private date hint when exact dates are unavailable"
        candidate = _lodging_candidate()
        boundary = _transport_boundary()
        private_preference = "private preference URL=https://example.test/token"
        complete = TripBriefDraft(
            destination=_destination(PRIVATE),
            dates=_exact_dates(),
            party=BriefTextFact(BriefKnownState.USER_STATED, "2 adults PNR=private"),
            budget=BriefTextFact(BriefKnownState.TENTATIVE, "JPY 987654"),
            pace=BriefTextFact(BriefKnownState.USER_STATED, private_preference),
            must_do=(BriefTextFact(BriefKnownState.USER_STATED, "Private dive"),),
            constraints=(BriefTextFact(BriefKnownState.TENTATIVE, "No stairs"),),
            transport_boundaries=(boundary,),
            lodging_requirement=LodgingRequirement.REQUIRED,
            lodging_candidates=(candidate,),
        )
        tentative = TripBriefDraft(
            destination=_destination(PRIVATE),
            dates=DateSpanDraft(
                state=BriefKnownState.TENTATIVE,
                hint=private_date_hint,
            ),
        )
        rendered = "\n".join(
            (
                repr(complete),
                repr(assess_guided_draft(complete)),
                json.dumps(assess_guided_draft(complete).to_dict()),
                repr(tentative),
                repr(assess_guided_draft(tentative)),
                json.dumps(assess_guided_draft(tentative).to_dict()),
            )
        )

        for private_value in (
            PRIVATE,
            private_date_hint,
            private_preference,
            "private-user-turn-transport",
            "private-user-turn-lodging",
            "2026-10-12",
            "14:30",
            "987654",
            "PNR=private",
        ):
            self.assertNotIn(private_value, rendered)

    def test_invalid_or_partial_date_spans_fail_closed(self) -> None:
        with self.assertRaises(ValueError):
            DateSpanDraft(
                state=BriefKnownState.USER_STATED,
                start=START,
            )
        with self.assertRaises(TypeError):
            DateSpanDraft(
                state=BriefKnownState.USER_STATED,
                start=datetime(2026, 10, 12, tzinfo=UTC),
                end=END,
            )
        with self.assertRaises(ValueError):
            DateSpanDraft(
                state=BriefKnownState.TENTATIVE,
                start=START,
                end=START + timedelta(days=367),
            )
        with self.assertRaises(ValueError):
            DateSpanDraft(
                state=BriefKnownState.UNKNOWN,
                hint="October",
            )

    def test_safe_projection_is_order_independent_and_duplicate_ids_fail(self) -> None:
        first = _lodging_candidate(
            decision=DecisionState.SELECTED,
            location_id="private-first",
        )
        second = _lodging_candidate(
            decision=DecisionState.SELECTED,
            location_id="private-second",
        )
        base = dict(
            destination=_destination(),
            dates=_exact_dates(),
            lodging_requirement=LodgingRequirement.REQUIRED,
        )
        forward = assess_guided_draft(
            TripBriefDraft(**base, lodging_candidates=(first, second))
        ).to_dict()
        reverse = assess_guided_draft(
            TripBriefDraft(**base, lodging_candidates=(second, first))
        ).to_dict()
        self.assertEqual(forward, reverse)

        boundary = _transport_boundary()
        with self.assertRaises(ValueError):
            TripBriefDraft(
                **base,
                transport_boundaries=(boundary, boundary),
            )
        with self.assertRaises(ValueError):
            TripBriefDraft(
                **base,
                lodging_candidates=(first, first),
            )
