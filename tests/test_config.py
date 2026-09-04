from __future__ import annotations

import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from video_analysis_mvp.config import config_path, load_runtime_config, mask_secret, save_runtime_config


class RuntimeConfigSecurityTest(unittest.TestCase):
    def test_audio_adapter_requires_absolute_path_and_bounded_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            configured = save_runtime_config(
                root,
                {
                    "audio_adapter_executable": str(root / "adapter"),
                    "audio_adapter_timeout_seconds": "90",
                },
            )
            self.assertEqual(str(root / "adapter"), configured.audio_adapter_executable)
            self.assertEqual(90, configured.audio_adapter_timeout_seconds)
            for updates in (
                {"audio_adapter_executable": "relative-adapter"},
                {"audio_adapter_timeout_seconds": "0"},
                {"audio_adapter_timeout_seconds": "601"},
                {"audio_adapter_timeout_seconds": "1.5"},
            ):
                with self.subTest(updates=updates), self.assertRaises(ValueError):
                    save_runtime_config(root, updates)

    def test_secret_status_never_discloses_key_fragments(self) -> None:
        secret = "sk-sensitive-first-and-last"

        masked = mask_secret(secret)

        self.assertEqual("Configured", masked)
        self.assertNotIn(secret[:5], masked)
        self.assertNotIn(secret[-4:], masked)

    def test_save_uses_private_permissions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            save_runtime_config(root, {"openai_api_key": "secret-value"})

            self.assertEqual(0o600, stat.S_IMODE(config_path(root).stat().st_mode))
            self.assertEqual(0o700, stat.S_IMODE(config_path(root).parent.stat().st_mode))

    def test_changing_endpoint_requires_key_reentry(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            save_runtime_config(root, {"openai_api_key": "secret-value"})
            updated = save_runtime_config(root, {"openai_base_url": "https://example.com/v1"})

            self.assertEqual("https://example.com/v1", updated.openai_base_url)
            self.assertEqual("", updated.openai_api_key)

    def test_load_repairs_legacy_posix_permissions(self) -> None:
        if os.name != "posix":
            self.skipTest("POSIX permission bits are not available")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            save_runtime_config(root, {"openai_api_key": "secret-value"})
            path = config_path(root)
            path.parent.chmod(0o755)
            path.chmod(0o644)

            loaded = load_runtime_config(root)

            self.assertEqual("secret-value", loaded.openai_api_key)
            self.assertEqual(0o600, stat.S_IMODE(path.stat().st_mode))
            self.assertEqual(0o700, stat.S_IMODE(path.parent.stat().st_mode))

    def test_rejects_unsafe_endpoints(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            invalid = (
                "http://example.com/v1",
                "https://user:pass@example.com/v1",
                "https://example.com/v1?forward=1",
                "file:///tmp/provider",
            )
            for endpoint in invalid:
                with self.subTest(endpoint=endpoint), self.assertRaises(ValueError):
                    save_runtime_config(root, {"openai_base_url": endpoint})

    def test_refuses_symlinked_config(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = config_path(root).parent
            settings.mkdir()
            target = root / "outside.json"
            target.write_text("{}", encoding="utf-8")
            config_path(root).symlink_to(target)

            with self.assertRaises(ValueError):
                load_runtime_config(root)
            with self.assertRaises(ValueError):
                save_runtime_config(root, {})

    def test_malformed_config_is_fail_closed_and_save_does_not_overwrite_it(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = config_path(root)
            path.parent.mkdir(mode=0o700)
            malformed = b'{"openai_api_key":"must-survive"'
            path.write_bytes(malformed)
            path.chmod(0o600)

            with self.assertRaisesRegex(ValueError, "malformed JSON"):
                load_runtime_config(root)
            with self.assertRaisesRegex(ValueError, "malformed JSON"):
                save_runtime_config(root, {"openai_model": "replacement"})

            self.assertEqual(malformed, path.read_bytes())

    def test_unknown_provider_is_rejected_on_save_and_load(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaisesRegex(ValueError, "Unsupported vision provider"):
                save_runtime_config(root, {"vision_provider": "mystery"})
            path = config_path(root)
            path.write_text('{"vision_provider":"mystery"}', encoding="utf-8")
            path.chmod(0o600)
            with self.assertRaisesRegex(ValueError, "Unsupported vision provider"):
                load_runtime_config(root)

    def test_concurrent_final_file_change_is_detected_before_replace(self) -> None:
        if os.name != "posix":
            self.skipTest("dirfd race check is POSIX-specific")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            save_runtime_config(root, {"openai_model": "original"})
            path = config_path(root)
            from video_analysis_mvp import config

            actual_write = config._write_all

            def write_then_race(descriptor: int, payload: bytes) -> None:
                actual_write(descriptor, payload)
                path.write_text('{"vision_provider":"openai","openai_model":"raced"}', encoding="utf-8")

            with (
                patch("video_analysis_mvp.config._write_all", side_effect=write_then_race),
                self.assertRaisesRegex(ValueError, "changed while"),
            ):
                save_runtime_config(root, {"openai_model": "new"})
            self.assertIn("raced", path.read_text(encoding="utf-8"))

    def test_final_symlink_swap_cannot_overwrite_outside_file(self) -> None:
        if os.name != "posix":
            self.skipTest("dirfd race check is POSIX-specific")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            save_runtime_config(root, {"openai_model": "original"})
            path = config_path(root)
            outside = root / "outside.json"
            outside.write_text("outside-must-survive", encoding="utf-8")
            from video_analysis_mvp import config

            actual_write = config._write_all

            def write_then_swap(descriptor: int, payload: bytes) -> None:
                actual_write(descriptor, payload)
                path.unlink()
                path.symlink_to(outside)

            with (
                patch("video_analysis_mvp.config._write_all", side_effect=write_then_swap),
                self.assertRaisesRegex(ValueError, "unsafe"),
            ):
                save_runtime_config(root, {"openai_model": "new"})
            self.assertEqual("outside-must-survive", outside.read_text(encoding="utf-8"))

    def test_symlinked_settings_parent_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            outside = root / "outside-settings"
            outside.mkdir()
            config_path(root).parent.symlink_to(outside, target_is_directory=True)
            with self.assertRaises(ValueError):
                load_runtime_config(root)
            with self.assertRaises(ValueError):
                save_runtime_config(root, {})

    def test_symlinked_transaction_lock_is_rejected_without_touching_target(self) -> None:
        if os.name != "posix":
            self.skipTest("no-follow lock checks are POSIX-specific")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = config_path(root).parent
            settings.mkdir(mode=0o700)
            outside = root / "outside.lock"
            outside.write_bytes(b"outside-must-survive")
            (settings / ".runtime_config.lock").symlink_to(outside)

            with self.assertRaisesRegex(ValueError, "lock"):
                save_runtime_config(root, {"openai_model": "new"})

            self.assertEqual(b"outside-must-survive", outside.read_bytes())
            self.assertFalse(config_path(root).exists())

    def test_symlinked_workspace_parent_component_is_rejected(self) -> None:
        if os.name != "posix":
            self.skipTest("component no-follow checks are POSIX-specific")
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            outside = base / "outside"
            outside.mkdir()
            (base / "linked").symlink_to(outside, target_is_directory=True)
            workspace = base / "linked" / "workspace"
            with self.assertRaises(ValueError):
                load_runtime_config(workspace)
            with self.assertRaises(ValueError):
                save_runtime_config(workspace, {})
            self.assertFalse((outside / "workspace").exists())


if __name__ == "__main__":
    unittest.main()
