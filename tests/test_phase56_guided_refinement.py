"""Phase 5.6 contracts for source-preserving private direction refinement."""

from __future__ import annotations

import ast
import inspect
import json
import unittest
from dataclasses import replace
from datetime import date

import trip_planner
import trip_planner.guided_refinement as guided_refinement_module
from trip_planner.guided_draft import (
    BriefKnownState,
    BriefTextFact,
    DateSpanDraft,
    DestinationDraft,
    TripBriefDraft,
)
from trip_planner.guided_proposal import (
    GuidedDirectionCard,
    GuidedDirectionPreferenceKind,
    GuidedOutlineLine,
    ProposalLineSource,
    capture_guided_direction_preference,
)
from trip_planner.guided_refinement import (
    GuidedRefinementCandidate,
    GuidedRefinementProblemCode,
    GuidedRefinementStatus,
    GuidedSourceLineRef,
    assess_guided_refinement,
)
from trip_planner.lodging import LocationHint, LocationHintKind


PRIVATE = "private-phase56-user-place-and-card-text-never-serialize"
START = date(2026, 10, 12)
END = date(2026, 10, 16)


def _brief() -> TripBriefDraft:
    return TripBriefDraft(
        destination=DestinationDraft(
            location=LocationHint(
                kind=LocationHintKind.AREA,
                label="Private island " + PRIVATE,
                input_text="Private island " + PRIVATE,
                country_code="jp",
            ),
            state=BriefKnownState.USER_STATED,
        ),
        dates=DateSpanDraft(
            state=BriefKnownState.USER_STATED,
            start=START,
            end=END,
        ),
        pace=BriefTextFact(
            state=BriefKnownState.TENTATIVE,
            value="Private relaxed pace " + PRIVATE,
        ),
        must_do=(
            BriefTextFact(
                state=BriefKnownState.USER_STATED,
                value="Private required stop " + PRIVATE,
            ),
            BriefTextFact(
                state=BriefKnownState.TENTATIVE,
                value="Private optional stop " + PRIVATE,
            ),
        ),
    )


def _line(
    title: str,
    *,
    slot: int,
    source: ProposalLineSource,
    state: BriefKnownState,
    must_do_indexes: tuple[int, ...] = (),
) -> GuidedOutlineLine:
    return GuidedOutlineLine(
        outline_slot=slot,
        source=source,
        known_state=state,
        title=title + " " + PRIVATE,
        rationale=title + " private rationale " + PRIVATE,
        must_do_indexes=must_do_indexes,
    )


def _cards() -> tuple[GuidedDirectionCard, GuidedDirectionCard]:
    return (
        GuidedDirectionCard(
            card_ref="card-a",
            title="Private direction A " + PRIVATE,
            rationale="Private A rationale " + PRIVATE,
            lines=(
                _line(
                    "A required",
                    slot=1,
                    source=ProposalLineSource.USER_STATED,
                    state=BriefKnownState.USER_STATED,
                    must_do_indexes=(0,),
                ),
                _line(
                    "A candidate",
                    slot=2,
                    source=ProposalLineSource.AI_SUGGESTED,
                    state=BriefKnownState.UNKNOWN,
                ),
            ),
        ),
        GuidedDirectionCard(
            card_ref="card-b",
            title="Private direction B " + PRIVATE,
            rationale="Private B rationale " + PRIVATE,
            lines=(
                _line(
                    "B required",
                    slot=1,
                    source=ProposalLineSource.USER_STATED,
                    state=BriefKnownState.USER_STATED,
                    must_do_indexes=(0,),
                ),
                _line(
                    "B tentative",
                    slot=2,
                    source=ProposalLineSource.USER_STATED,
                    state=BriefKnownState.TENTATIVE,
                    must_do_indexes=(1,),
                ),
                _line(
                    "B candidate",
                    slot=3,
                    source=ProposalLineSource.AI_SUGGESTED,
                    state=BriefKnownState.UNKNOWN,
                ),
            ),
        ),
    )


def _preference(
    brief: TripBriefDraft,
    cards: tuple[GuidedDirectionCard, ...],
    kind: GuidedDirectionPreferenceKind,
    refs: tuple[str, ...] = (),
):
    return capture_guided_direction_preference(
        brief,
        cards,
        kind=kind,
        card_refs=refs,
    )


def _source_ref(card_ref: str, line_index: int) -> GuidedSourceLineRef:
    return GuidedSourceLineRef(card_ref=card_ref, line_index=line_index)


def _candidate_from_sources(
    cards: tuple[GuidedDirectionCard, ...],
    sources: tuple[tuple[str, int], ...],
    *,
    output_lines: tuple[GuidedOutlineLine, ...] | None = None,
    card_ref: str = "card-refined",
) -> GuidedRefinementCandidate:
    cards_by_ref = {card.card_ref: card for card in cards}
    refs = tuple(_source_ref(card_ref_, index) for card_ref_, index in sources)
    if output_lines is None:
        output_lines = tuple(
            replace(
                cards_by_ref[source.card_ref].lines[source.line_index],
                outline_slot=slot,
            )
            for slot, source in enumerate(refs, start=1)
        )
    return GuidedRefinementCandidate(
        direction=GuidedDirectionCard(
            card_ref=card_ref,
            title="Private refined direction " + PRIVATE,
            rationale="Private refined rationale " + PRIVATE,
            lines=output_lines,
        ),
        retained_source_lines=refs,
    )


class GuidedRefinementTests(unittest.TestCase):
    def test_prefer_one_walkthrough_reaches_one_redacted_review(self) -> None:
        brief = _brief()
        cards = _cards()
        preference = _preference(
            brief,
            cards,
            GuidedDirectionPreferenceKind.PREFER_ONE,
            ("card-a",),
        )
        review = assess_guided_refinement(
            brief,
            cards,
            preference,
            _candidate_from_sources(cards, (("card-a", 0), ("card-a", 1))),
        )
        safe = review.to_dict()

        self.assertEqual(GuidedRefinementStatus.REVIEW_REQUIRED, review.status)
        self.assertEqual("review_refined_direction", safe["next_action"])
        self.assertTrue(safe["requires_user_response"])
        self.assertTrue(safe["requires_user_review"])
        self.assertTrue(safe["may_present_refined_direction"])
        self.assertEqual([], safe["problems"])
        self.assertEqual(
            {
                "preference_kind": "prefer_one",
                "selected_source_card_count": 1,
                "retained_source_card_count": 1,
                "retained_source_line_count": 2,
                "refined_outline_line_count": 2,
                "required_must_do_count": 1,
                "minimum_declared_must_do_coverage": 1,
                "uncovered_required_must_do_count": 0,
                "decision_state": "candidate",
                "evidence_state": "unverified",
                "supports_authoritative_use": False,
            },
            safe["refinement"],
        )
        self.assertEqual(
            {
                "process_local": True,
                "writes_to_trip": False,
                "provider_calls": False,
                "scheduled": False,
                "rendered": False,
                "deployed": False,
            },
            safe["side_effects"],
        )

    def test_prefer_one_cannot_silently_drop_a_selected_line(self) -> None:
        brief = _brief()
        cards = _cards()
        preference = _preference(
            brief,
            cards,
            GuidedDirectionPreferenceKind.PREFER_ONE,
            ("card-a",),
        )
        review = assess_guided_refinement(
            brief,
            cards,
            preference,
            _candidate_from_sources(cards, (("card-a", 0),)),
        )

        self.assertEqual(GuidedRefinementStatus.NEEDS_REFINEMENT, review.status)
        self.assertEqual("refine_private_direction", review.next_action)
        self.assertFalse(review.may_present_refined_direction)
        self.assertIn(
            GuidedRefinementProblemCode.PREFERRED_LINE_NOT_RETAINED,
            review.problem_codes,
        )

    def test_mix_retains_each_selected_card_and_every_user_line(self) -> None:
        brief = _brief()
        cards = _cards()
        preference = _preference(
            brief,
            cards,
            GuidedDirectionPreferenceKind.MIX,
            ("card-b", "card-a"),
        )
        sources = (
            ("card-a", 0),
            ("card-a", 1),
            ("card-b", 0),
            ("card-b", 1),
            ("card-b", 2),
        )
        forward = assess_guided_refinement(
            brief,
            cards,
            preference,
            _candidate_from_sources(cards, sources),
        ).to_dict()
        reverse = assess_guided_refinement(
            brief,
            tuple(reversed(cards)),
            preference,
            _candidate_from_sources(cards, sources),
        ).to_dict()

        self.assertEqual("review_required", forward["status"])
        self.assertEqual(forward, reverse)
        self.assertEqual(2, forward["refinement"]["selected_source_card_count"])

    def test_mix_cannot_use_shared_user_lines_as_direction_carryover(self) -> None:
        brief = _brief()
        cards = _cards()
        preference = _preference(
            brief,
            cards,
            GuidedDirectionPreferenceKind.MIX,
            ("card-a", "card-b"),
        )
        review = assess_guided_refinement(
            brief,
            cards,
            preference,
            _candidate_from_sources(
                cards,
                (("card-a", 0), ("card-b", 0), ("card-b", 1)),
            ),
        )

        self.assertEqual(GuidedRefinementStatus.NEEDS_REFINEMENT, review.status)
        self.assertEqual(
            (GuidedRefinementProblemCode.PREFERRED_CARD_NOT_RETAINED,),
            review.problem_codes,
        )

    def test_mix_missing_one_card_and_user_line_stays_private_for_repair(self) -> None:
        brief = _brief()
        cards = _cards()
        preference = _preference(
            brief,
            cards,
            GuidedDirectionPreferenceKind.MIX,
            ("card-a", "card-b"),
        )
        safe = assess_guided_refinement(
            brief,
            cards,
            preference,
            _candidate_from_sources(cards, (("card-a", 0),)),
        ).to_dict()

        self.assertEqual("needs_refinement", safe["status"])
        self.assertFalse(safe["may_present_refined_direction"])
        self.assertIsNone(safe["review_prompt"])
        self.assertIn("preferred_card_not_retained", safe["problems"])
        self.assertIn("user_stated_line_not_retained", safe["problems"])

    def test_request_refinement_still_preserves_all_user_stated_lines(self) -> None:
        brief = _brief()
        cards = _cards()
        preference = _preference(
            brief,
            cards,
            GuidedDirectionPreferenceKind.REQUEST_REFINEMENT,
        )
        new_required_line = _line(
            "Entirely new required",
            slot=1,
            source=ProposalLineSource.USER_STATED,
            state=BriefKnownState.USER_STATED,
            must_do_indexes=(0,),
        )
        candidate = _candidate_from_sources(
            cards,
            (),
            output_lines=(new_required_line,),
        )
        review = assess_guided_refinement(
            brief,
            cards,
            preference,
            candidate,
        )

        self.assertEqual(GuidedRefinementStatus.NEEDS_REFINEMENT, review.status)
        self.assertIn(
            GuidedRefinementProblemCode.USER_STATED_LINE_NOT_RETAINED,
            review.problem_codes,
        )
        self.assertNotIn(
            GuidedRefinementProblemCode.REQUIRED_MUST_DO_COVERAGE_INCOMPLETE,
            review.problem_codes,
        )

    def test_retained_line_content_must_be_carried_even_if_slots_change(self) -> None:
        brief = _brief()
        cards = _cards()
        preference = _preference(
            brief,
            cards,
            GuidedDirectionPreferenceKind.PREFER_ONE,
            ("card-a",),
        )
        changed_ai_line = replace(
            cards[0].lines[1],
            outline_slot=1,
            title="Changed private AI line " + PRIVATE,
        )
        carried_user_line = replace(cards[0].lines[0], outline_slot=2)
        review = assess_guided_refinement(
            brief,
            cards,
            preference,
            _candidate_from_sources(
                cards,
                (("card-a", 0), ("card-a", 1)),
                output_lines=(changed_ai_line, carried_user_line),
            ),
        )

        self.assertEqual(GuidedRefinementStatus.NEEDS_REFINEMENT, review.status)
        self.assertEqual(
            (
                GuidedRefinementProblemCode.RETAINED_SOURCE_LINE_NOT_CARRIED,
            ),
            review.problem_codes,
        )

    def test_prefer_one_cannot_reintroduce_an_unpreferred_source(self) -> None:
        brief = _brief()
        cards = _cards()
        preference = _preference(
            brief,
            cards,
            GuidedDirectionPreferenceKind.PREFER_ONE,
            ("card-a",),
        )
        review = assess_guided_refinement(
            brief,
            cards,
            preference,
            _candidate_from_sources(
                cards,
                (("card-a", 0), ("card-a", 1), ("card-b", 0)),
            ),
        )

        self.assertEqual(GuidedRefinementStatus.NEEDS_REFINEMENT, review.status)
        self.assertIn(
            GuidedRefinementProblemCode.UNPREFERRED_SOURCE_INCLUDED,
            review.problem_codes,
        )

    def test_unpreferred_source_content_cannot_bypass_an_omitted_ref(self) -> None:
        brief = _brief()
        cards = _cards()
        preference = _preference(
            brief,
            cards,
            GuidedDirectionPreferenceKind.PREFER_ONE,
            ("card-a",),
        )
        output_lines = (
            replace(cards[0].lines[0], outline_slot=1),
            replace(cards[0].lines[1], outline_slot=2),
            replace(cards[1].lines[2], outline_slot=3),
        )
        review = assess_guided_refinement(
            brief,
            cards,
            preference,
            _candidate_from_sources(
                cards,
                (("card-a", 0), ("card-a", 1)),
                output_lines=output_lines,
            ),
        )

        self.assertEqual(GuidedRefinementStatus.NEEDS_REFINEMENT, review.status)
        self.assertEqual(
            (GuidedRefinementProblemCode.UNPREFERRED_SOURCE_INCLUDED,),
            review.problem_codes,
        )

    def test_duplicate_source_content_requires_duplicate_output_carryover(self) -> None:
        brief = _brief()
        original_cards = _cards()
        duplicated_ai_line = replace(
            original_cards[0].lines[1],
            outline_slot=3,
        )
        cards = (
            replace(
                original_cards[0],
                lines=(*original_cards[0].lines, duplicated_ai_line),
            ),
            original_cards[1],
        )
        preference = _preference(
            brief,
            cards,
            GuidedDirectionPreferenceKind.PREFER_ONE,
            ("card-a",),
        )
        output_lines = (
            replace(cards[0].lines[0], outline_slot=1),
            replace(cards[0].lines[1], outline_slot=2),
        )
        review = assess_guided_refinement(
            brief,
            cards,
            preference,
            _candidate_from_sources(
                cards,
                (("card-a", 0), ("card-a", 1), ("card-a", 2)),
                output_lines=output_lines,
            ),
        )

        self.assertEqual(GuidedRefinementStatus.NEEDS_REFINEMENT, review.status)
        self.assertEqual(
            (
                GuidedRefinementProblemCode.RETAINED_SOURCE_LINE_NOT_CARRIED,
            ),
            review.problem_codes,
        )

    def test_unknown_duplicate_out_of_range_and_colliding_refs_fail_closed(self) -> None:
        brief = _brief()
        cards = _cards()
        preference = _preference(
            brief,
            cards,
            GuidedDirectionPreferenceKind.PREFER_ONE,
            ("card-a",),
        )

        with self.assertRaises(ValueError):
            GuidedSourceLineRef(card_ref="private bad ref", line_index=0)
        with self.assertRaises(ValueError):
            GuidedRefinementCandidate(
                direction=_candidate_from_sources(cards, (("card-a", 0),)).direction,
                retained_source_lines=(
                    _source_ref("card-a", 0),
                    _source_ref("card-a", 0),
                ),
            )
        with self.assertRaises(ValueError):
            assess_guided_refinement(
                brief,
                cards,
                preference,
                _candidate_from_sources(
                    cards,
                    (("card-missing", 0),),
                    output_lines=(cards[0].lines[0],),
                ),
            )
        with self.assertRaises(ValueError):
            assess_guided_refinement(
                brief,
                cards,
                preference,
                _candidate_from_sources(
                    cards,
                    (("card-a", 31),),
                    output_lines=(cards[0].lines[0],),
                ),
            )
        with self.assertRaises(ValueError):
            assess_guided_refinement(
                brief,
                cards,
                preference,
                _candidate_from_sources(
                    cards,
                    (("card-a", 0), ("card-a", 1)),
                    card_ref="card-a",
                ),
            )

    def test_stale_brief_or_card_context_rejects_the_old_preference(self) -> None:
        brief = _brief()
        cards = _cards()
        preference = _preference(
            brief,
            cards,
            GuidedDirectionPreferenceKind.PREFER_ONE,
            ("card-a",),
        )
        candidate = _candidate_from_sources(
            cards,
            (("card-a", 0), ("card-a", 1)),
        )
        changed_brief = replace(
            brief,
            pace=BriefTextFact(
                state=BriefKnownState.TENTATIVE,
                value="Changed private pace " + PRIVATE,
            ),
        )
        changed_cards = (
            replace(cards[0], title="Changed private card " + PRIVATE),
            cards[1],
        )

        with self.assertRaises(ValueError):
            assess_guided_refinement(
                changed_brief,
                cards,
                preference,
                candidate,
            )
        with self.assertRaises(ValueError):
            assess_guided_refinement(
                brief,
                changed_cards,
                preference,
                candidate,
            )

    def test_safe_views_and_repr_never_include_private_text_or_refs(self) -> None:
        brief = _brief()
        cards = _cards()
        preference = _preference(
            brief,
            cards,
            GuidedDirectionPreferenceKind.PREFER_ONE,
            ("card-a",),
        )
        candidate = _candidate_from_sources(
            cards,
            (("card-a", 0), ("card-a", 1)),
        )
        review = assess_guided_refinement(
            brief,
            cards,
            preference,
            candidate,
        )
        rendered = "\n".join(
            (
                repr(candidate.retained_source_lines[0]),
                repr(candidate),
                repr(review),
                json.dumps(review.to_dict(), ensure_ascii=False),
            )
        )

        for private_value in (
            PRIVATE,
            "card-a",
            "card-refined",
            "Private direction A",
            "2026-10-12",
        ):
            self.assertNotIn(private_value, rendered)
        with self.assertRaises(ValueError):
            replace(review, tentative_fields=(PRIVATE,))

    def test_public_contract_has_no_apply_provider_cli_or_io_path(self) -> None:
        for name in (
            "GUIDED_REFINEMENT_VERSION",
            "GuidedRefinementCandidate",
            "GuidedRefinementProblemCode",
            "GuidedRefinementReview",
            "GuidedRefinementStatus",
            "GuidedSourceLineRef",
            "assess_guided_refinement",
        ):
            self.assertTrue(hasattr(trip_planner, name))
            self.assertIn(name, trip_planner.__all__)
        for unsupported_name in (
            "apply_guided_refinement",
            "confirm_guided_refinement",
            "guided_refinement_cli",
            "create_trip_from_refinement",
        ):
            self.assertFalse(hasattr(trip_planner, unsupported_name))

        source = inspect.getsource(guided_refinement_module)
        tree = ast.parse(source)
        allowed_imports = {"re", "unicodedata"}
        allowed_from_imports = {
            (0, "__future__"),
            (0, "collections"),
            (0, "dataclasses"),
            (0, "enum"),
            (0, "typing"),
            (1, "guided_draft"),
            (1, "guided_proposal"),
            (1, "models"),
        }
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for imported in node.names:
                    self.assertIn(imported.name, allowed_imports)
            elif isinstance(node, ast.ImportFrom):
                self.assertIn((node.level, node.module), allowed_from_imports)
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                self.assertNotIn(
                    node.func.id,
                    {"open", "exec", "eval", "compile", "__import__"},
                )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
