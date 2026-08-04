"""Offline contracts for the fail-closed public-release boundary."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

from trip_planner.public_release import (
    PUBLIC_ARTIFACT_SCHEMA_VERSION,
    PublicReleaseError,
    build_public_site,
    prepare_public_release,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_DIR = REPO_ROOT / "template"
DEPLOY_SCRIPT = REPO_ROOT / "scripts" / "deploy.sh"
PRIVATE = "private-address-place-id-token-price-37.5000-127.0000"


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )


def _tree_bytes(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _public_trip(slug: str, *, title: str | None = None) -> dict[str, object]:
    return {
        "schema_version": "trip-planner.public-trip/v1",
        "slug": slug,
        "title": title or f"{slug} public itinerary",
        "date_label": "2026-10-01 to 2026-10-02",
        "cities": ["Fixture City"],
        "days": [
            {
                "day": 1,
                "label": "Arrival",
                "items": [
                    {"time": "10:00", "title": "Public first stop"},
                    {"time": None, "title": "Public flexible time"},
                ],
            }
        ],
    }


class Phase60PublicReleaseTests(unittest.TestCase):
    def _template_copy(self, root: Path) -> Path:
        copied = root / "template"
        copied.mkdir(parents=True)
        for name in ("public_index.html", "public_trip.html"):
            shutil.copyfile(TEMPLATE_DIR / name, copied / name)
        return copied

    def _public_root(self, root: Path) -> Path:
        source_root = root / "public"
        (source_root / "trips").mkdir(parents=True)
        return source_root

    def _prepare_manifest(
        self,
        source_root: Path,
        template_dir: Path,
        slugs: list[str],
    ) -> dict[str, object]:
        manifest = prepare_public_release(
            slugs,
            source_root=source_root,
            template_dir=template_dir,
        )
        _write_json(source_root / "release.json", manifest)
        return manifest

    def _build(self, root: Path, source_root: Path, template_dir: Path) -> dict[str, object]:
        return build_public_site(
            source_root / "release.json",
            root / "output",
            source_root=source_root,
            template_dir=template_dir,
        )

    def test_valid_public_release_is_exact_deterministic_and_excludes_private_data(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source_root = self._public_root(root)
            template_dir = self._template_copy(root)
            _write_json(source_root / "trips" / "alpha-2026.json", _public_trip("alpha-2026"))
            private_source = root / "trips" / "private" / "data" / "trip.json"
            private_source.parent.mkdir(parents=True)
            private_source.write_text(PRIVATE, encoding="utf-8")
            self._prepare_manifest(source_root, template_dir, ["alpha-2026"])

            result = self._build(root, source_root, template_dir)

            self.assertEqual(1, result["trip_count"])
            self.assertEqual(2, result["artifact_count"])
            output = root / "output"
            self.assertEqual(
                {
                    "alpha-2026/index.html",
                    "index.html",
                    "release-manifest.json",
                },
                set(_tree_bytes(output)),
            )
            rendered = b"\n".join(_tree_bytes(output).values()).decode("utf-8")
            self.assertNotIn(PRIVATE, rendered)
            self.assertNotIn("calendar.ics", rendered)
            self.assertNotIn("localStorage", rendered)
            self.assertNotIn("leaflet", rendered.lower())
            self.assertNotIn("maps.google", rendered.lower())
            artifact_manifest = json.loads(
                (output / "release-manifest.json").read_text(encoding="utf-8")
            )
            self.assertEqual(PUBLIC_ARTIFACT_SCHEMA_VERSION, artifact_manifest["schema_version"])
            self.assertEqual(
                ["index.html", "alpha-2026/index.html"],
                [record["path"] for record in artifact_manifest["artifacts"]],
            )

            copied_output = root / "second-output"
            build_public_site(
                source_root / "release.json",
                copied_output,
                source_root=source_root,
                template_dir=template_dir,
            )
            self.assertEqual(_tree_bytes(output), _tree_bytes(copied_output))

    def test_public_text_is_escaped_and_private_schema_fields_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source_root = self._public_root(root)
            template_dir = self._template_copy(root)
            public_path = source_root / "trips" / "escape-2026.json"
            _write_json(
                public_path,
                _public_trip("escape-2026", title="Public <script>alert(1)</script>"),
            )
            self._prepare_manifest(source_root, template_dir, ["escape-2026"])
            self._build(root, source_root, template_dir)
            rendered = (root / "output" / "escape-2026" / "index.html").read_text(
                encoding="utf-8"
            )
            self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", rendered)
            self.assertNotIn("<script>", rendered)

            invalid = _public_trip("escape-2026")
            invalid["address"] = PRIVATE
            _write_json(public_path, invalid)
            with self.assertRaises(PublicReleaseError) as raised:
                prepare_public_release(
                    ["escape-2026"], source_root=source_root, template_dir=template_dir
                )
            self.assertEqual("PUBLIC_SCHEMA_FIELDS_INVALID", raised.exception.code)

    def test_invalid_json_and_lone_surrogate_are_bounded_refusals(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source_root = self._public_root(root)
            template_dir = self._template_copy(root)
            public_path = source_root / "trips" / "invalid-2026.json"
            public_path.write_text(
                '{"schema_version":"trip-planner.public-trip/v1","schema_version":"x"}',
                encoding="utf-8",
            )
            with self.assertRaises(PublicReleaseError) as duplicate:
                prepare_public_release(
                    ["invalid-2026"], source_root=source_root, template_dir=template_dir
                )
            self.assertEqual("PUBLIC_JSON_INVALID", duplicate.exception.code)

            public_path.write_text(
                json.dumps(
                    _public_trip("invalid-2026", title="lone-surrogate-\ud800"),
                    ensure_ascii=True,
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
            with self.assertRaises(PublicReleaseError) as surrogate:
                prepare_public_release(
                    ["invalid-2026"], source_root=source_root, template_dir=template_dir
                )
            self.assertEqual("PUBLIC_TEXT_INVALID", surrogate.exception.code)

    def test_digest_and_index_binding_reject_source_template_and_order_drift_before_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source_root = self._public_root(root)
            template_dir = self._template_copy(root)
            _write_json(source_root / "trips" / "alpha-2026.json", _public_trip("alpha-2026"))
            _write_json(source_root / "trips" / "bravo-2026.json", _public_trip("bravo-2026"))
            manifest = self._prepare_manifest(
                source_root, template_dir, ["alpha-2026", "bravo-2026"]
            )

            changed_source = _public_trip("alpha-2026", title="Changed after review")
            _write_json(source_root / "trips" / "alpha-2026.json", changed_source)
            with self.assertRaises(PublicReleaseError) as source_error:
                self._build(root, source_root, template_dir)
            self.assertEqual("PUBLIC_SOURCE_DIGEST_MISMATCH", source_error.exception.code)
            self.assertFalse((root / "output").exists())

            _write_json(source_root / "trips" / "alpha-2026.json", _public_trip("alpha-2026"))
            (template_dir / "public_trip.html").write_text(
                (template_dir / "public_trip.html").read_text(encoding="utf-8") + "\n<!-- drift -->\n",
                encoding="utf-8",
            )
            with self.assertRaises(PublicReleaseError) as trip_template_error:
                self._build(root, source_root, template_dir)
            self.assertEqual("PUBLIC_HTML_DIGEST_MISMATCH", trip_template_error.exception.code)
            self.assertFalse((root / "output").exists())

            template_dir = self._template_copy(root / "replacement")
            (template_dir / "public_index.html").write_text(
                (template_dir / "public_index.html").read_text(encoding="utf-8") + "\n<!-- drift -->\n",
                encoding="utf-8",
            )
            with self.assertRaises(PublicReleaseError) as index_template_error:
                self._build(root, source_root, template_dir)
            self.assertEqual("PUBLIC_INDEX_HTML_DIGEST_MISMATCH", index_template_error.exception.code)
            self.assertFalse((root / "output").exists())

            template_dir = self._template_copy(root / "ordered")
            manifest["trips"].reverse()  # type: ignore[index]
            _write_json(source_root / "release.json", manifest)
            with self.assertRaises(PublicReleaseError) as order_error:
                self._build(root, source_root, template_dir)
            self.assertEqual("PUBLIC_INDEX_HTML_DIGEST_MISMATCH", order_error.exception.code)
            self.assertFalse((root / "output").exists())

    def test_symlink_and_hardlink_sources_are_refused_without_public_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source_root = root / "public"
            source_root.mkdir()
            template_dir = self._template_copy(root)
            private_trips = root / "trips"
            private_trips.mkdir()
            _write_json(private_trips / "alpha-2026.json", _public_trip("alpha-2026"))
            (source_root / "trips").symlink_to(private_trips, target_is_directory=True)
            with self.assertRaises(PublicReleaseError) as directory_link:
                prepare_public_release(
                    ["alpha-2026"], source_root=source_root, template_dir=template_dir
                )
            self.assertEqual("PUBLIC_SOURCE_UNSAFE", directory_link.exception.code)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source_root = self._public_root(root)
            template_dir = self._template_copy(root)
            private_file = root / "private.json"
            _write_json(private_file, _public_trip("alpha-2026"))
            public_file = source_root / "trips" / "alpha-2026.json"
            public_file.symlink_to(private_file)
            with self.assertRaises(PublicReleaseError) as file_link:
                prepare_public_release(
                    ["alpha-2026"], source_root=source_root, template_dir=template_dir
                )
            self.assertEqual("PUBLIC_SOURCE_UNSAFE", file_link.exception.code)

            public_file.unlink()
            try:
                os.link(private_file, public_file)
            except OSError as error:
                self.skipTest(f"hard links unavailable: {error}")
            with self.assertRaises(PublicReleaseError) as hard_link:
                prepare_public_release(
                    ["alpha-2026"], source_root=source_root, template_dir=template_dir
                )
            self.assertEqual("PUBLIC_SOURCE_UNSAFE", hard_link.exception.code)

    def test_template_symlinks_are_refused_without_private_content_or_output(self) -> None:
        for template_name in ("public_trip.html", "public_index.html"):
            with self.subTest(template_name=template_name), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                source_root = self._public_root(root)
                template_dir = self._template_copy(root)
                _write_json(source_root / "trips" / "alpha-2026.json", _public_trip("alpha-2026"))
                self._prepare_manifest(source_root, template_dir, ["alpha-2026"])
                private_template = root / "private-template.txt"
                private_template.write_text(PRIVATE, encoding="utf-8")
                target = template_dir / template_name
                target.unlink()
                target.symlink_to(private_template)

                with self.assertRaises(PublicReleaseError) as raised:
                    self._build(root, source_root, template_dir)
                self.assertEqual("PUBLIC_TEMPLATE_UNSAFE", raised.exception.code)
                self.assertNotIn(PRIVATE, str(raised.exception))
                self.assertFalse((root / "output").exists())

    def test_intermediate_public_trips_swap_is_pinned_and_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source_root = self._public_root(root)
            template_dir = self._template_copy(root)
            _write_json(source_root / "trips" / "alpha-2026.json", _public_trip("alpha-2026"))
            private_trips = root / "private-trips"
            private_trips.mkdir()
            (private_trips / "alpha-2026.json").write_text(PRIVATE, encoding="utf-8")
            original_open = os.open
            swapped = False

            def swap_before_leaf_open(path: object, flags: int, *args: object, **kwargs: object) -> int:
                nonlocal swapped
                if path == "alpha-2026.json" and not swapped:
                    swapped = True
                    (source_root / "trips").rename(root / "public-trips-before-swap")
                    (source_root / "trips").symlink_to(
                        private_trips, target_is_directory=True
                    )
                return original_open(path, flags, *args, **kwargs)

            with patch(
                "trip_planner.public_release.os.open",
                side_effect=swap_before_leaf_open,
            ):
                with self.assertRaises(PublicReleaseError) as raised:
                    prepare_public_release(
                        ["alpha-2026"],
                        source_root=source_root,
                        template_dir=template_dir,
                    )

            self.assertTrue(swapped)
            self.assertEqual("PUBLIC_SOURCE_CHANGED", raised.exception.code)
            self.assertNotIn(PRIVATE, str(raised.exception))

    def test_intermediate_template_directory_swap_is_pinned_and_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source_root = self._public_root(root)
            template_dir = self._template_copy(root)
            _write_json(source_root / "trips" / "alpha-2026.json", _public_trip("alpha-2026"))
            private_templates = root / "private-templates"
            private_templates.mkdir()
            (private_templates / "public_trip.html").write_text(PRIVATE, encoding="utf-8")
            original_open = os.open
            swapped = False

            def swap_before_template_open(
                path: object, flags: int, *args: object, **kwargs: object
            ) -> int:
                nonlocal swapped
                if path == "public_trip.html" and not swapped:
                    swapped = True
                    template_dir.rename(root / "template-before-swap")
                    template_dir.symlink_to(private_templates, target_is_directory=True)
                return original_open(path, flags, *args, **kwargs)

            with patch(
                "trip_planner.public_release.os.open",
                side_effect=swap_before_template_open,
            ):
                with self.assertRaises(PublicReleaseError) as raised:
                    prepare_public_release(
                        ["alpha-2026"],
                        source_root=source_root,
                        template_dir=template_dir,
                    )

            self.assertTrue(swapped)
            self.assertEqual("PUBLIC_SOURCE_CHANGED", raised.exception.code)
            self.assertNotIn(PRIVATE, str(raised.exception))

    def test_deploy_refuses_missing_manifest_before_builder_or_git_network_work(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fake_repo = root / "repo"
            (fake_repo / "scripts").mkdir(parents=True)
            copied_deploy = fake_repo / "scripts" / "deploy.sh"
            shutil.copyfile(DEPLOY_SCRIPT, copied_deploy)
            fake_bin = root / "bin"
            fake_bin.mkdir()
            git_log = root / "git.log"
            fake_git = fake_bin / "git"
            fake_git.write_text(
                "#!/usr/bin/env bash\n"
                "printf '%s\\n' \"$*\" >> \"$FAKE_GIT_LOG\"\n"
                "if [ \"$1\" = \"rev-parse\" ]; then printf '%s\\n' \"$FAKE_REPO\"; exit 0; fi\n"
                "exit 99\n",
                encoding="utf-8",
            )
            fake_git.chmod(0o755)
            environment = {
                **os.environ,
                "PATH": f"{fake_bin}:{os.environ['PATH']}",
                "FAKE_REPO": str(fake_repo),
                "FAKE_GIT_LOG": str(git_log),
            }

            completed = subprocess.run(
                ["bash", str(copied_deploy)],
                cwd=fake_repo,
                text=True,
                capture_output=True,
                check=False,
                env=environment,
            )

            self.assertEqual(2, completed.returncode)
            self.assertIn("public/release.json is required", completed.stderr)
            self.assertNotIn("Building explicit public release", completed.stdout)
            self.assertFalse(git_log.exists())


if __name__ == "__main__":
    unittest.main()
