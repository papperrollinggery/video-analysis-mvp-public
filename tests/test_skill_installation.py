from __future__ import annotations

import importlib.util
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import venv
import zipfile


REPO_ROOT = Path(__file__).resolve().parents[1]
SKILL_ROOT = REPO_ROOT / ".agents" / "skills" / "video-evidence-workbench"


def load_script(name: str):
    path = SKILL_ROOT / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"test_{name}", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def make_wheel(path: Path, *, distribution: str = "video-analysis-mvp", version: str = "0.3.0") -> None:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            f"{distribution.replace('-', '_')}-{version}.dist-info/METADATA",
            f"Metadata-Version: 2.1\nName: {distribution}\nVersion: {version}\n",
        )


class SkillInstallationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.installer = load_script("install")
        self.wrapper = load_script("workbench")
        self.temp = tempfile.TemporaryDirectory(prefix="vew-skill-install-")
        self.root = Path(self.temp.name)
        self.wheel = self.root / "video_analysis_mvp-0.3.0-py3-none-any.whl"
        make_wheel(self.wheel)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _fake_venv(self, target: str) -> None:
        runtime = Path(target)
        bin_dir = runtime / "bin"
        bin_dir.mkdir(parents=True)
        for name in ("python", "analyze-video"):
            executable = bin_dir / name
            executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            executable.chmod(0o755)

    def _fake_run(self, command, **kwargs):
        if command[-1] == "--version":
            return subprocess.CompletedProcess(command, 0, "pip 26.2.1 from /runtime/site-packages/pip (python 3.13)\n", "")
        if "pip" in command:
            return subprocess.CompletedProcess(command, 0, "", "")
        return subprocess.CompletedProcess(command, 0, json.dumps({"version": "0.3.0", "module": "/runtime/package.py", "extras": {}}), "")

    def test_local_wheel_installs_isolated_runtime_and_skill_without_runtime_residue(self) -> None:
        skills_dir = self.root / "codex" / "skills"
        runtimes = self.root / "runtimes"
        with patch.object(self.installer.venv.EnvBuilder, "create", side_effect=self._fake_venv), patch.object(
            self.installer.subprocess, "run", side_effect=self._fake_run
        ):
            code = self.installer.main(["--wheel", str(self.wheel), "--skills-dir", str(skills_dir), "--runtime-home", str(runtimes)])
        self.assertEqual(code, 0)
        installed = skills_dir / "video-evidence-workbench"
        runtime = json.loads((installed / "runtime.json").read_text(encoding="utf-8"))
        self.assertEqual(runtime["version"], "0.3.0")
        self.assertTrue(Path(runtime["executable"]).is_file())
        self.assertTrue(Path(runtime["python"]).is_file())
        self.assertFalse((SKILL_ROOT / "runtime.json").exists())
        self.assertFalse(any("__pycache__" in str(path) for path in installed.rglob("*")))

    def test_replacement_keeps_prior_skill_outside_discovery_directory(self) -> None:
        skills_dir = self.root / "codex" / "skills"
        old = skills_dir / "video-evidence-workbench"
        old.mkdir(parents=True)
        (old / "SKILL.md").write_text("old skill\n", encoding="utf-8")
        executable = self.root / "runtime" / "bin" / "analyze-video"
        executable.parent.mkdir(parents=True)
        executable.write_text("#!/bin/sh\n", encoding="utf-8")
        executable.chmod(0o755)
        backup = self.installer.install_skill(skills_dir, executable, "0.3.0", "a" * 64)
        self.assertIsNotNone(backup)
        assert backup is not None
        self.assertEqual(backup.parent, skills_dir.parent / ".video-evidence-workbench-backups")
        self.assertEqual((backup / "SKILL.md").read_text(encoding="utf-8"), "old skill\n")
        self.assertTrue((skills_dir / "video-evidence-workbench" / "runtime.json").is_file())

    def test_failed_activation_restores_prior_skill(self) -> None:
        skills_dir = self.root / "codex" / "skills"
        old = skills_dir / "video-evidence-workbench"
        old.mkdir(parents=True)
        (old / "SKILL.md").write_text("old skill\n", encoding="utf-8")
        executable = self.root / "runtime" / "bin" / "analyze-video"
        executable.parent.mkdir(parents=True)
        executable.write_text("#!/bin/sh\n", encoding="utf-8")
        executable.chmod(0o755)
        real_replace = self.installer.os.replace
        calls = 0

        def fail_on_activation(source, destination):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("simulated activation failure")
            return real_replace(source, destination)

        with patch.object(self.installer.os, "replace", side_effect=fail_on_activation):
            with self.assertRaises(OSError):
                self.installer.install_skill(skills_dir, executable, "0.3.0", "b" * 64)
        self.assertEqual((old / "SKILL.md").read_text(encoding="utf-8"), "old skill\n")

    def test_rejects_non_workbench_wheel_and_symlink(self) -> None:
        other = self.root / "other.whl"
        make_wheel(other, distribution="unrelated")
        with self.assertRaisesRegex(ValueError, "distribution"):
            self.installer.wheel_metadata(other)
        link = self.root / "linked.whl"
        link.symlink_to(self.wheel)
        with self.assertRaisesRegex(ValueError, "regular file"):
            self.installer.wheel_metadata(link)

    def test_reinstall_repairs_modified_skill_even_when_runtime_binding_matches(self) -> None:
        skills_dir = self.root / "codex" / "skills"
        executable = self.root / "runtime" / "bin" / "analyze-video"
        executable.parent.mkdir(parents=True)
        executable.write_text("#!/bin/sh\n", encoding="utf-8")
        executable.chmod(0o755)
        self.installer.install_skill(skills_dir, executable, "0.3.0", "e" * 64)
        installed = skills_dir / "video-evidence-workbench" / "SKILL.md"
        installed.write_text("modified local instructions", encoding="utf-8")
        backup = self.installer.install_skill(skills_dir, executable, "0.3.0", "e" * 64)
        self.assertIsNotNone(backup)
        self.assertEqual(installed.read_bytes(), (SKILL_ROOT / "SKILL.md").read_bytes())
        self.assertEqual((backup / "SKILL.md").read_text(), "modified local instructions")

    def test_wrapper_preserves_callers_working_directory(self) -> None:
        executable = self.root / "analyze-video"
        executable.write_text("#!/bin/sh\n", encoding="utf-8")
        executable.chmod(0o755)
        caller = self.root / "caller"
        caller.mkdir()
        with patch.object(self.wrapper, "_executable", return_value=(executable, "installed")), patch.object(
            self.wrapper.subprocess, "call", return_value=0
        ) as call, patch("os.getcwd", return_value=str(caller)):
            self.assertEqual(self.wrapper.main(["--workspace", "relative-projects", "doctor"]), 0)
        self.assertEqual(call.call_args.kwargs["cwd"], str(caller))
        self.assertEqual(call.call_args.args[0][0], str(executable))
        self.assertEqual(call.call_args.args[0][1:4], ["-I", "-m", "video_analysis_mvp.cli"])

    def test_runtime_binding_uses_venv_python_not_console_script(self) -> None:
        console_script = self.root / "bin" / "analyze-video"
        interpreter = self.root / "bin" / "python"
        console_script.parent.mkdir(parents=True)
        for executable in (console_script, interpreter):
            executable.write_text("#!/bin/sh\n", encoding="utf-8")
            executable.chmod(0o755)
        runtime_file = self.root / "runtime.json"
        runtime_file.write_text(
            json.dumps({"distribution": "video-analysis-mvp", "executable": str(console_script), "python": str(interpreter), "version": "0.3.0"}),
            encoding="utf-8",
        )
        with patch.object(self.wrapper, "RUNTIME_FILE", runtime_file):
            bound, source = self.wrapper._executable()
        self.assertEqual(source, "installed")
        self.assertEqual(bound, interpreter)

    def test_invalid_runtime_binding_does_not_fallback_to_development(self) -> None:
        runtime_file = self.root / "runtime.json"
        runtime_file.write_text("{broken", encoding="utf-8")
        with patch.object(self.wrapper, "RUNTIME_FILE", runtime_file), patch.object(
            self.wrapper, "_development_python", return_value=Path("/unexpected/development/python")
        ):
            bound, source = self.wrapper._executable()
        self.assertIsNone(bound)
        self.assertEqual(source, "invalid-runtime")

    def test_runtime_info_reports_binding_separately_from_actual_runtime(self) -> None:
        console_script = self.root / "bin" / "analyze-video"
        interpreter = self.root / "bin" / "python"
        console_script.parent.mkdir(parents=True)
        for executable in (console_script, interpreter):
            executable.write_text("#!/bin/sh\n", encoding="utf-8")
            executable.chmod(0o755)
        runtime_file = self.root / "runtime.json"
        runtime_file.write_text(
            json.dumps({"distribution": "video-analysis-mvp", "executable": str(console_script), "python": str(interpreter), "version": "0.3.0"}),
            encoding="utf-8",
        )
        output = io.StringIO()
        with patch.object(self.wrapper, "RUNTIME_FILE", runtime_file), patch.object(
            self.wrapper, "_runtime_probe", return_value={"version": "0.3.0", "module": "/runtime/site-packages/video_analysis_mvp/__init__.py"}
        ), contextlib.redirect_stdout(output):
            self.assertEqual(self.wrapper.main(["--runtime-info"]), 0)
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["binding"]["python"], str(interpreter))
        self.assertEqual(payload["actual"]["version"], "0.3.0")

    def test_runtime_info_fails_closed_when_actual_probe_fails(self) -> None:
        console_script = self.root / "bin" / "analyze-video"
        interpreter = self.root / "bin" / "python"
        console_script.parent.mkdir(parents=True)
        for executable in (console_script, interpreter):
            executable.write_text("#!/bin/sh\n", encoding="utf-8")
            executable.chmod(0o755)
        runtime_file = self.root / "runtime.json"
        runtime_file.write_text(
            json.dumps({"distribution": "video-analysis-mvp", "executable": str(console_script), "python": str(interpreter), "version": "0.3.0"}),
            encoding="utf-8",
        )
        output = io.StringIO()
        with patch.object(self.wrapper, "RUNTIME_FILE", runtime_file), patch.object(
            self.wrapper, "_runtime_probe", return_value=None
        ), contextlib.redirect_stdout(output):
            self.assertEqual(self.wrapper.main(["--runtime-info"]), 2)
        self.assertEqual(json.loads(output.getvalue())["status"], "invalid-runtime")

    def test_rejects_old_and_path_like_wheel_versions(self) -> None:
        for version in ("0.2.2", "../0.3.0", "/tmp/0.3.0"):
            wheel = self.root / f"bad-{len(version)}.whl"
            make_wheel(wheel, version=version)
            with self.assertRaises(ValueError):
                self.installer.wheel_metadata(wheel)

    def test_rejects_dangling_skill_symlink(self) -> None:
        skills_dir = self.root / "codex" / "skills"
        skills_dir.mkdir(parents=True)
        (skills_dir / "video-evidence-workbench").symlink_to(self.root / "missing")
        executable = self.root / "runtime" / "bin" / "analyze-video"
        executable.parent.mkdir(parents=True)
        executable.write_text("#!/bin/sh\n", encoding="utf-8")
        executable.chmod(0o755)
        with self.assertRaisesRegex(ValueError, "symlink"):
            self.installer.install_skill(skills_dir, executable, "0.3.0", "c" * 64)

    def test_extras_are_part_of_runtime_identity_and_pip_requirement(self) -> None:
        runtimes = self.root / "runtimes"
        calls = []

        def record_run(command, **kwargs):
            calls.append(command)
            return self._fake_run(command, **kwargs)

        with patch.object(self.installer.venv.EnvBuilder, "create", side_effect=self._fake_venv), patch.object(
            self.installer.subprocess, "run", side_effect=record_run
        ):
            executable, _ = self.installer.ensure_runtime(self.wheel, "0.3.0", runtimes, ("pdf", "api"))
        self.assertIn("api-pdf", str(executable))
        self.assertEqual(calls[1][-1], f"{self.wheel}[api,pdf]")
        self.assertEqual(calls[1][1:4], ["-I", "-m", "pip"])

    def test_old_pip_is_upgraded_before_wheel_install(self) -> None:
        calls = []
        version_checks = 0

        def old_then_safe_pip(command, **kwargs):
            nonlocal version_checks
            calls.append(command)
            if command[-1] == "--version":
                version_checks += 1
                version = "26.0.1" if version_checks == 1 else "26.2.1"
                return subprocess.CompletedProcess(command, 0, f"pip {version} from /runtime/pip (python 3.13)\n", "")
            if "pip" in command:
                return subprocess.CompletedProcess(command, 0, "", "")
            return self._fake_run(command, **kwargs)

        with patch.object(self.installer.venv.EnvBuilder, "create", side_effect=self._fake_venv), patch.object(
            self.installer.subprocess, "run", side_effect=old_then_safe_pip
        ):
            self.installer.ensure_runtime(self.wheel, "0.3.0", self.root / "runtimes")
        upgrade_index = next(index for index, call in enumerate(calls) if call[-1] == "pip==26.2.1")
        wheel_index = next(index for index, call in enumerate(calls) if call[-1] == str(self.wheel))
        self.assertLess(upgrade_index, wheel_index)

    def test_safe_pip_reused_runtime_is_not_upgraded(self) -> None:
        runtimes = self.root / "runtimes"
        digest = self.installer._sha256(self.wheel)[:16]
        runtime = runtimes / f"0.3.0-{digest}-base"
        self._fake_venv(str(runtime))
        calls = []

        def record(command, **kwargs):
            calls.append(command)
            return self._fake_run(command, **kwargs)

        with patch.object(self.installer.subprocess, "run", side_effect=record):
            self.installer.ensure_runtime(self.wheel, "0.3.0", runtimes)
        self.assertFalse(any(call[-1] == "pip==26.2.1" for call in calls))

    def test_two_component_pip_versions_upgrade_or_reuse_by_numeric_version(self) -> None:
        for initial, should_upgrade in (("25.2", True), ("26.2", True), ("26.3", False)):
            with self.subTest(version=initial):
                upgraded = False

                def pip_response(command, **kwargs):
                    nonlocal upgraded
                    if command[-1] == "--version":
                        version = "26.2.1" if upgraded else initial
                        return subprocess.CompletedProcess(command, 0, f"pip {version} from /runtime/pip (python 3.13)\n", "")
                    self.assertEqual(command[-1], "pip==26.2.1")
                    upgraded = True
                    return subprocess.CompletedProcess(command, 0, "", "")

                with patch.object(self.installer.subprocess, "run", side_effect=pip_response):
                    self.installer._ensure_safe_pip(self.root / "python")
                self.assertEqual(upgraded, should_upgrade)

    def test_pip_upgrade_failure_does_not_activate_skill(self) -> None:
        skills_dir = self.root / "codex" / "skills"
        checks = 0

        def failed_upgrade(command, **kwargs):
            nonlocal checks
            if command[-1] == "--version":
                checks += 1
                return subprocess.CompletedProcess(command, 0, "pip 26.0.1 from /runtime/pip (python 3.13)\n", "")
            if command[-1] == "pip==26.2.1":
                raise subprocess.CalledProcessError(1, command)
            return self._fake_run(command, **kwargs)

        with patch.object(self.installer.venv.EnvBuilder, "create", side_effect=self._fake_venv), patch.object(
            self.installer.subprocess, "run", side_effect=failed_upgrade
        ):
            code = self.installer.main(["--wheel", str(self.wheel), "--skills-dir", str(skills_dir), "--runtime-home", str(self.root / "runtimes")])
        self.assertEqual(code, 2)
        self.assertFalse((skills_dir / "video-evidence-workbench").exists())

    def test_changed_skill_source_digest_prevents_stale_skill_reuse(self) -> None:
        skills_dir = self.root / "codex" / "skills"
        target = skills_dir / "video-evidence-workbench"
        target.mkdir(parents=True)
        executable = self.root / "runtime" / "bin" / "analyze-video"
        executable.parent.mkdir(parents=True)
        executable.write_text("#!/bin/sh\n", encoding="utf-8")
        executable.chmod(0o755)
        runtime = {
            "schema": "video-evidence-workbench-runtime/v1", "distribution": "video-analysis-mvp", "version": "0.3.0",
            "wheel_sha256": "d" * 64, "extras": [], "executable": str(executable), "python": str(executable.parent / "python"),
            "skill_sha256": "old-source",
        }
        (target / "runtime.json").write_text(json.dumps(runtime), encoding="utf-8")
        with patch.object(self.installer, "_skill_source_digest", return_value="new-source"):
            backup = self.installer.install_skill(skills_dir, executable, "0.3.0", "d" * 64)
        self.assertIsNotNone(backup)
        self.assertEqual(json.loads((target / "runtime.json").read_text())["skill_sha256"], "new-source")

    def test_relative_install_paths_are_made_absolute_before_binding(self) -> None:
        caller = self.root / "caller"
        caller.mkdir()
        with patch.object(self.installer.venv.EnvBuilder, "create", side_effect=self._fake_venv), patch.object(
            self.installer.subprocess, "run", side_effect=self._fake_run
        ), patch("os.getcwd", return_value=str(caller)):
            code = self.installer.main(["--wheel", str(self.wheel), "--skills-dir", "relative-skills", "--runtime-home", "relative-runtimes"])
        self.assertEqual(code, 0)
        runtime = json.loads((caller / "relative-skills" / "video-evidence-workbench" / "runtime.json").read_text())
        self.assertTrue(Path(runtime["python"]).is_absolute())

    def test_symlinked_venv_is_runnable_on_this_host(self) -> None:
        root = self.root / "small-venv"
        venv.EnvBuilder(with_pip=False, symlinks=True).create(root)
        result = subprocess.run([str(root / "bin" / "python"), "-I", "-c", "print('ok')"], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "ok")

    def test_installer_bootstraps_without_third_party_imports(self) -> None:
        result = subprocess.run([sys.executable, "-I", str(SKILL_ROOT / "scripts" / "install.py"), "--help"], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_isolated_interpreter_rejects_callers_pythonpath_decoy(self) -> None:
        caller = self.root / "caller"
        decoy = caller / "video_analysis_mvp"
        decoy.mkdir(parents=True)
        (decoy / "__init__.py").write_text("", encoding="utf-8")
        (decoy / "cli.py").write_text("raise SystemExit('decoy was imported')\n", encoding="utf-8")
        environment = dict(os.environ, PYTHONPATH=str(caller))
        result = subprocess.run(
            [sys.executable, "-I", "-c", "import video_analysis_mvp; print(video_analysis_mvp.__file__)"],
            cwd=caller,
            env=environment,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(result.stdout.strip().startswith(str(caller)))


if __name__ == "__main__":
    unittest.main()
