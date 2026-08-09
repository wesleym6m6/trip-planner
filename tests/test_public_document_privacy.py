"""Structural privacy checks for public tracked documentation."""

from __future__ import annotations

import re
import subprocess
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
RAW_DIGEST = re.compile(r"(?<![0-9A-Fa-f])[0-9A-Fa-f]{64}(?![0-9A-Fa-f])")
PLACE_ID_SHAPED_TOKEN = re.compile(
    r"(?<![A-Za-z0-9_-])ChIJ[A-Za-z0-9_-]{8,}(?![A-Za-z0-9_-])"
)
CONCRETE_TRIP_PATH = re.compile(
    r"(?<!public/)\btrips/(?![<{*])[^/\s`\"')\]}]+(?:/|\b)"
)
RUNNABLE_PRIVATE_LIVE_COMMAND = re.compile(
    r"(?:provider_exit_gate\.py|(?:^|\s)--live(?:\s|$))"
)
EXACT_PRIVATE_CHOICE_ARGUMENT = re.compile(
    r"--(?:origin-choice|origin-selection-binding(?:-v\d+)?)"
    r"\s*(?:=|\s)\s*(?![<{])(?:[\"']\s*)?[A-Za-z0-9]"
)


def _tracked_names(*pathspecs: str) -> tuple[str, ...]:
    completed = subprocess.run(
        ["git", "ls-files", "-z", "--", *pathspecs],
        cwd=REPO_ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )
    return tuple(
        raw.decode("utf-8") for raw in completed.stdout.split(b"\0") if raw
    )


def _document_violations(path: Path) -> tuple[str, ...]:
    rules = (
        ("raw-private-digest", RAW_DIGEST),
        ("provider-place-id-shaped-token", PLACE_ID_SHAPED_TOKEN),
        ("concrete-private-trip-path", CONCRETE_TRIP_PATH),
        ("runnable-private-live-command", RUNNABLE_PRIVATE_LIVE_COMMAND),
        ("exact-private-choice-argument", EXACT_PRIVATE_CHOICE_ARGUMENT),
    )
    violations: list[str] = []
    for line_number, line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        for rule, pattern in rules:
            if pattern.search(line):
                violations.append(
                    f"{path.relative_to(REPO_ROOT)}:{line_number}:{rule}"
                )
    return tuple(violations)


class PublicDocumentPrivacyTests(unittest.TestCase):
    def test_detectors_use_synthetic_shapes(self) -> None:
        self.assertIsNotNone(RAW_DIGEST.search("a" * 64))
        self.assertIsNotNone(PLACE_ID_SHAPED_TOKEN.search("ChIJ" + "a" * 8))
        self.assertIsNone(PLACE_ID_SHAPED_TOKEN.search("<synthetic-place-id>"))
        self.assertIsNotNone(
            CONCRETE_TRIP_PATH.search("trips/example-city-2099-12/data")
        )
        self.assertIsNotNone(CONCRETE_TRIP_PATH.search("trips/private_slug/data"))
        self.assertIsNotNone(CONCRETE_TRIP_PATH.search("trips/private-slug"))
        self.assertIsNone(CONCRETE_TRIP_PATH.search("trips/{slug}/data"))
        self.assertIsNone(CONCRETE_TRIP_PATH.search("trips/<slug>/data"))
        self.assertIsNone(CONCRETE_TRIP_PATH.search("trips/*/data"))
        self.assertIsNone(
            CONCRETE_TRIP_PATH.search("public/trips/{slug}.json")
        )
        self.assertIsNotNone(
            EXACT_PRIVATE_CHOICE_ARGUMENT.search("--origin-choice=B")
        )
        self.assertIsNotNone(
            EXACT_PRIVATE_CHOICE_ARGUMENT.search(
                "--origin-selection-binding-v3 privatevalue"
            )
        )
        self.assertIsNotNone(
            EXACT_PRIVATE_CHOICE_ARGUMENT.search('--origin-choice "B"')
        )
        self.assertIsNone(
            EXACT_PRIVATE_CHOICE_ARGUMENT.search("--origin-choice {choice}")
        )

    def test_public_tracked_markdown_has_no_structural_private_values(
        self,
    ) -> None:
        names = _tracked_names("*.md")
        self.assertTrue(names)
        violations: list[str] = []
        for name in names:
            path = REPO_ROOT / name
            if path.is_symlink() or not path.is_file():
                violations.append(f"{name}:0:tracked-markdown-not-regular-file")
                continue
            violations.extend(_document_violations(path))
        self.assertEqual([], violations)

    def test_private_trip_tree_is_not_tracked(self) -> None:
        tracked = _tracked_names("trips/**")
        if tracked:
            self.fail(f"{len(tracked)} prohibited private-trip path(s) tracked")

    def test_destination_bound_provider_exit_gates_are_not_tracked(self) -> None:
        tracked = _tracked_names(
            "scripts/*_provider_exit_gate.py",
            "tests/test_*provider_exit_gate.py",
        )
        if tracked:
            self.fail(f"{len(tracked)} prohibited destination-gate path(s) tracked")


if __name__ == "__main__":
    unittest.main()
