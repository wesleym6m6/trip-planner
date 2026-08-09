"""Phase 5.5 contracts for private guided direction preferences."""

from __future__ import annotations

import ast
import inspect
import json
import unittest
from dataclasses import replace
from datetime import date

import trip_planner
import trip_planner.guided_proposal as guided_proposal_module
from trip_planner.guided_draft import (
    BriefKnownState,
    BriefTextFact,
    DateSpanDraft,
    DestinationDraft,
    TripBriefDraft,
)
from trip_planner.guided_proposal import (
    GuidedDirectionCard,
    GuidedDirectionPreference,
    GuidedDirectionPreferenceKind,
    GuidedDirectionPreferenceStatus,
    GuidedOutlineLine,
    GuidedProposalStatus,
    ProposalLineSource,
    assess_guided_direction_preference,
    assess_guided_proposal,
    capture_guided_direction_preference,
)
from trip_planner.lodging import LocationHint, LocationHintKind


START = date(2026, 10, 12)
END = date(2026, 10, 16)
PRIVATE = "private-direction-card-user-response-place-token-never-serialize"


def _brief(
    *,
    destination_state: BriefKnownState = BriefKnownState.USER_STATED,
    date_state: BriefKnownState = BriefKnownState.USER_STATED,
) -> TripBriefDraft:
    return TripBriefDraft(
        destination=DestinationDraft(
            location=LocationHint(
                kind=LocationHintKind.AREA,
                label="Synthetic private destination " + PRIVATE,
                input_text="Synthetic private destination " + PRIVATE,
                country_code="jp",
            ),
            state=destination_state,
        ),
        dates=DateSpanDraft(state=date_state, start=START, end=END),
        pace=BriefTextFact(
            state=BriefKnownState.TENTATIVE,
            value="Private slow pace " + PRIVATE,
        ),
        must_do=(
            BriefTextFact(
                state=BriefKnownState.USER_STATED,
                value="Private required place " + PRIVATE,
            ),
            BriefTextFact(
                state=BriefKnownState.TENTATIVE,
                value="Private tentative place " + PRIVATE,
            ),
        ),
    )


def _line(
    *,
    slot: int,
    source: ProposalLineSource = ProposalLineSource.AI_SUGGESTED,
    state: BriefKnownState = BriefKnownState.UNKNOWN,
    must_do_indexes: tuple[int, ...] = (),
) -> GuidedOutlineLine:
    return GuidedOutlineLine(
        outline_slot=slot,
        source=source,
        known_state=state,
        title="Private outline " + PRIVATE,
        rationale="Private outline rationale " + PRIVATE,
        must_do_indexes=must_do_indexes,
    )


def _cards(
    *,
    second_covers_required: bool = True,
) -> tuple[GuidedDirectionCard, GuidedDirectionCard]:
    return (
        GuidedDirectionCard(
            card_ref="card-a",
            title="Private direction A " + PRIVATE,
            rationale="Private direction A rationale " + PRIVATE,
            lines=(
                _line(
                    slot=1,
                    source=ProposalLineSource.USER_STATED,
                    state=BriefKnownState.USER_STATED,
                    must_do_indexes=(0,),
                ),
                _line(slot=2),
            ),
        ),
        GuidedDirectionCard(
            card_ref="card-b",
            title="Private direction B " + PRIVATE,
            rationale="Private direction B rationale " + PRIVATE,
            lines=(
                _line(
                    slot=1,
                    source=ProposalLineSource.USER_STATED,
                    state=BriefKnownState.USER_STATED,
                    must_do_indexes=(0,) if second_covers_required else (),
                ),
                _line(
                    slot=2,
                    source=ProposalLineSource.USER_STATED,
                    state=BriefKnownState.TENTATIVE,
                    must_do_indexes=(1,),
                ),
            ),
        ),
    )


def _capture(
    brief: TripBriefDraft,
    cards: tuple[GuidedDirectionCard, ...],
    *,
    kind: GuidedDirectionPreferenceKind,
    card_refs: tuple[str, ...] = (),
) -> GuidedDirectionPreference:
    return capture_guided_direction_preference(
        brief,
        cards,
        kind=kind,
        card_refs=card_refs,
    )


class GuidedDirectionPreferenceTests(unittest.TestCase):
    def test_private_future_trip_walkthrough_reaches_refinement_without_side_effects(self) -> None:
        empty = assess_guided_proposal(TripBriefDraft(), _cards())
        destination_only = assess_guided_proposal(
            TripBriefDraft(destination=_brief().destination),
            _cards(),
        )
        cards = _cards()
        brief = _brief()
        directions = assess_guided_proposal(brief, cards)
        handoff = assess_guided_direction_preference(
            brief,
            cards,
            _capture(
                brief,
                cards,
                kind=GuidedDirectionPreferenceKind.PREFER_ONE,
                card_refs=("card-b",),
            ),
        )

        self.assertEqual(GuidedProposalStatus.NEEDS_INPUT, empty.status)
        self.assertEqual(0, empty.direction_card_count)
        self.assertEqual(GuidedProposalStatus.NEEDS_INPUT, destination_only.status)
        self.assertEqual(0, destination_only.direction_card_count)
        self.assertEqual(GuidedProposalStatus.REVIEW_REQUIRED, directions.status)
        self.assertTrue(directions.may_present_direction_cards)
        self.assertEqual(
            GuidedDirectionPreferenceStatus.READY_FOR_PRIVATE_REFINEMENT,
            handoff.status,
        )
        self.assertFalse(handoff.to_dict()["requires_user_response"])

    def test_public_contract_is_process_local_without_apply_or_provider_path(self) -> None:
        for name in (
            "GuidedDirectionPreference",
            "GuidedDirectionPreferenceKind",
            "GuidedDirectionPreferenceReview",
            "GuidedDirectionPreferenceStatus",
            "capture_guided_direction_preference",
            "assess_guided_direction_preference",
        ):
            self.assertTrue(hasattr(trip_planner, name))
            self.assertIn(name, trip_planner.__all__)
        for unsupported_name in (
            "select_guided_direction",
            "apply_guided_direction_preference",
            "guided_direction_preference_cli",
            "create_trip_from_direction_preference",
        ):
            self.assertFalse(hasattr(trip_planner, unsupported_name))

        source = inspect.getsource(guided_proposal_module)
        tree = ast.parse(source)
        allowed_imports = {"hashlib", "json", "re", "unicodedata"}
        allowed_from_imports = {
            (0, "__future__"),
            (0, "dataclasses"),
            (0, "datetime"),
            (0, "enum"),
            (0, "typing"),
            (1, "guided_draft"),
            (1, "models"),
        }
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for imported in node.names:
                    self.assertIn(imported.name, allowed_imports)
            elif isinstance(node, ast.ImportFrom):
                self.assertIn((node.level, node.module), allowed_from_imports)
            elif (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
            ):
                self.assertNotIn(
                    node.func.id,
                    {"open", "exec", "eval", "compile", "__import__"},
                )

    def test_one_direction_preference_is_redacted_and_non_authoritative(self) -> None:
        brief = _brief()
        cards = _cards()
        preference = _capture(
            brief,
            cards,
            kind=GuidedDirectionPreferenceKind.PREFER_ONE,
            card_refs=("card-b",),
        )
        review = assess_guided_direction_preference(
            brief,
            cards,
            preference,
        )
        safe = review.to_dict()

        self.assertEqual(
            GuidedDirectionPreferenceStatus.READY_FOR_PRIVATE_REFINEMENT,
            review.status,
        )
        self.assertEqual("refine_private_direction", safe["next_action"])
        self.assertFalse(safe["requires_user_response"])
        self.assertFalse(safe["requires_user_review"])
        self.assertFalse(safe["requires_user_decision"])
        self.assertEqual(
            {
                "kind": "prefer_one",
                "preferred_card_count": 1,
                "has_preferred_direction": True,
                "decision_state": "candidate",
                "evidence_state": "unverified",
            },
            safe["direction_preference"],
        )
        self.assertEqual(["pace", "must_do"], safe["tentative_fields"])
        self.assertEqual(
            ["destination_location", "proposal_candidates"],
            safe["needs_verification"],
        )
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

    def test_mixed_preference_is_order_independent(self) -> None:
        brief = _brief()
        cards = _cards()
        preference = _capture(
            brief,
            cards,
            kind=GuidedDirectionPreferenceKind.MIX,
            card_refs=("card-b", "card-a"),
        )

        forward = assess_guided_direction_preference(
            brief,
            cards,
            preference,
        ).to_dict()
        reverse = assess_guided_direction_preference(
            brief,
            tuple(reversed(cards)),
            preference,
        ).to_dict()

        self.assertEqual(("card-a", "card-b"), preference.card_refs)
        self.assertEqual(forward, reverse)
        self.assertEqual("mix", forward["direction_preference"]["kind"])
        self.assertEqual(
            2,
            forward["direction_preference"]["preferred_card_count"],
        )

    def test_request_refinement_names_no_card_and_needs_no_new_confirmation(self) -> None:
        brief = _brief()
        cards = _cards()
        review = assess_guided_direction_preference(
            brief,
            cards,
            _capture(
                brief,
                cards,
                kind=GuidedDirectionPreferenceKind.REQUEST_REFINEMENT,
            ),
        )
        safe = review.to_dict()

        self.assertEqual(
            "request_refinement",
            safe["direction_preference"]["kind"],
        )
        self.assertEqual(
            0,
            safe["direction_preference"]["preferred_card_count"],
        )
        self.assertFalse(safe["direction_preference"]["has_preferred_direction"])
        self.assertIsNone(safe["direction_preference"]["decision_state"])
        self.assertIsNone(safe["direction_preference"]["evidence_state"])
        self.assertFalse(safe["requires_user_response"])
        self.assertFalse(safe["requires_user_review"])
        self.assertFalse(safe["requires_user_decision"])

    def test_invalid_card_reference_shapes_and_unknown_cards_fail_closed(self) -> None:
        with self.assertRaises(ValueError):
            _capture(
                _brief(),
                _cards(),
                kind=GuidedDirectionPreferenceKind.PREFER_ONE,
            )
        with self.assertRaises(ValueError):
            _capture(
                _brief(),
                _cards(),
                kind=GuidedDirectionPreferenceKind.MIX,
                card_refs=("card-a",),
            )
        with self.assertRaises(ValueError):
            _capture(
                _brief(),
                _cards(),
                kind=GuidedDirectionPreferenceKind.MIX,
                card_refs=("card-a", "card-a"),
            )
        with self.assertRaises(ValueError):
            _capture(
                _brief(),
                _cards(),
                kind=GuidedDirectionPreferenceKind.REQUEST_REFINEMENT,
                card_refs=("card-a",),
            )

        with self.assertRaises(ValueError):
            _capture(
                _brief(),
                _cards(),
                kind=GuidedDirectionPreferenceKind.PREFER_ONE,
                card_refs=("card-missing",),
            )
        with self.assertRaises(ValueError):
            GuidedDirectionPreference(
                kind=GuidedDirectionPreferenceKind.PREFER_ONE,
                card_refs=("card-a",),
            )

    def test_unready_or_incomplete_cards_cannot_consume_a_preference(self) -> None:
        source_brief = _brief()
        source_cards = _cards()
        preference = _capture(
            source_brief,
            source_cards,
            kind=GuidedDirectionPreferenceKind.PREFER_ONE,
            card_refs=("card-a",),
        )

        with self.assertRaises(ValueError):
            assess_guided_direction_preference(TripBriefDraft(), _cards(), preference)
        with self.assertRaises(ValueError):
            assess_guided_direction_preference(
                _brief(),
                _cards(second_covers_required=False),
                preference,
            )

    def test_preference_is_bound_to_exact_brief_and_card_contents(self) -> None:
        brief = _brief()
        cards = _cards()
        preference = _capture(
            brief,
            cards,
            kind=GuidedDirectionPreferenceKind.PREFER_ONE,
            card_refs=("card-a",),
        )
        changed_cards = (
            replace(cards[0], title="Private changed direction " + PRIVATE),
            cards[1],
        )
        changed_brief = replace(
            brief,
            must_do=(
                BriefTextFact(
                    state=BriefKnownState.USER_STATED,
                    value="Private changed required place " + PRIVATE,
                ),
                brief.must_do[1],
            ),
        )

        self.assertEqual(
            GuidedProposalStatus.REVIEW_REQUIRED,
            assess_guided_proposal(changed_brief, cards).status,
        )
        with self.assertRaises(ValueError):
            assess_guided_direction_preference(changed_brief, cards, preference)
        with self.assertRaises(ValueError):
            assess_guided_direction_preference(brief, changed_cards, preference)

    def test_safe_transcript_and_repr_never_include_card_or_user_text(self) -> None:
        brief = _brief()
        cards = _cards()
        preference = _capture(
            brief,
            cards,
            kind=GuidedDirectionPreferenceKind.PREFER_ONE,
            card_refs=("card-a",),
        )
        review = assess_guided_direction_preference(
            brief,
            cards,
            preference,
        )
        rendered = "\n".join(
            (
                repr(preference),
                repr(review),
                json.dumps(review.to_dict(), ensure_ascii=False),
            )
        )

        for private_value in (
            PRIVATE,
            "card-a",
            "Private direction A",
            "Private required place",
            "2026-10-12",
        ):
            self.assertNotIn(private_value, rendered)

    def test_ordinary_review_replacement_cannot_inject_private_transcript_text(self) -> None:
        brief = _brief()
        cards = _cards()
        review = assess_guided_direction_preference(
            brief,
            cards,
            _capture(
                brief,
                cards,
                kind=GuidedDirectionPreferenceKind.PREFER_ONE,
                card_refs=("card-a",),
            ),
        )

        with self.assertRaises(ValueError):
            replace(review, tentative_fields=(PRIVATE,))
        with self.assertRaises(ValueError):
            replace(
                review,
                preference_kind=GuidedDirectionPreferenceKind.MIX,
                preferred_card_count=1,
            )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
