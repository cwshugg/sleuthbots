"""Focused native installation checks using only isolated home directories."""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

# The installer is always invoked as a user would, outside the source checkout.
ROOT = Path(__file__).resolve().parents[1]
ROLES = ("coordinator", "skeptic", "theorist")
SKILLS = ("sleuth-workspace-cli", "sleuth-case-cli", "sleuth-theory-cli")


class TestInstall(unittest.TestCase):
    """Exercise discovery layout, execution, conflicts, and unsafe paths."""

    def setUp(self) -> None:
        """Allocate a private HOME for every installer subprocess."""
        temporary = tempfile.TemporaryDirectory(prefix="sleuth install ")
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name)
        self.destination = self.home / ".copilot"

    def run_install(
        self, *args: str, code: int = 0, root: Path = ROOT
    ) -> subprocess.CompletedProcess:
        """Run with no access to the actual user's installation directory."""
        result = subprocess.run(
            [sys.executable, str(root / "install.py"), *args],
            cwd=self.home,
            env={
                **os.environ,
                "HOME": str(self.home),
                "USERPROFILE": str(self.home),
            },
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        self.assertEqual(result.returncode, code, result.stdout + result.stderr)
        return result

    def test_native_discovery_and_installed_cli_workflow(self) -> None:
        """Discover profiles, resolve links, and run installed scripts."""
        self.assertIn("--overwrite", self.run_install("--help").stdout)
        self.run_install()
        agents = self.destination / "agents"
        self.assertEqual(
            {path.name for path in agents.iterdir()},
            {f"sleuthbots-{role}.agent.md" for role in ROLES},
        )
        for path in agents.iterdir():
            text = path.read_text(encoding="utf-8")
            frontmatter = text.split("---", 2)[1]
            self.assertIn('name: "[Sleuthbots::', frontmatter)
            self.assertIn("description:", frontmatter)
            self.assertNotIn("sleuthbots:", text)
            for link in re.findall(r"\]\(([^)]+)\)", text):
                self.assertTrue((path.parent / link).is_file(), link)
            for agent in re.findall(r"sleuthbots-[a-z]+", text):
                self.assertTrue((agents / f"{agent}.agent.md").is_file())
        skills = self.destination / "skills"
        for name in SKILLS:
            text = (skills / name / "SKILL.md").read_text(encoding="utf-8")
            self.assertIn(f"name: {name}\n", text)
            self.assertIn("description:", text.split("---", 2)[1])
            for link in re.findall(r"\]\(([^)]+)\)", text):
                self.assertTrue((skills / name / link).is_file(), link)
        for template in ("case", "theory"):
            self.assertTrue(
                (
                    skills
                    / f"sleuth-{template}-cli"
                    / f"template.{template}.md"
                ).is_file()
            )
        self.assertTrue((skills / "sleuth_common.py").is_file())

        def cli(kind: str, *args: str) -> dict:
            """Invoke an installed entry point from an unrelated working dir."""
            result = subprocess.run(
                [
                    sys.executable,
                    str(
                        skills / f"sleuth-{kind}-cli" / f"sleuth-{kind}-cli.py"
                    ),
                    "--json",
                    *args,
                ],
                cwd=self.home,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            self.assertEqual(
                result.returncode, 0, result.stdout + result.stderr
            )
            return json.loads(result.stdout)["data"]

        cli("workspace", "init")
        case = cli("case", "create", "--title", "Installed case")
        theory = cli(
            "theory",
            "create",
            "--case-id",
            case["id"],
            "--title",
            "Installed theory",
        )
        self.assertTrue(Path(case["path"]).is_file())
        self.assertTrue(Path(theory["path"]).is_file())

    def test_rerun_conflict_and_explicit_overwrite(self) -> None:
        """Leave identical and unrelated files alone; preflight conflicts."""
        self.run_install()
        target = self.destination / "agents/sleuthbots-coordinator.agent.md"
        original = target.read_bytes()
        before = target.stat().st_mtime_ns
        self.assertIn("0 copied, 12 unchanged", self.run_install().stdout)
        self.assertEqual(target.stat().st_mtime_ns, before)
        unrelated = self.destination / "skills/keep.txt"
        unrelated.write_text("keep", encoding="utf-8")
        target.write_text("custom", encoding="utf-8")
        missing = self.destination / "skills/sleuth_common.py"
        missing.unlink()
        self.assertIn("Conflict:", self.run_install(code=1).stderr)
        self.assertEqual(target.read_text(encoding="utf-8"), "custom")
        self.assertFalse(missing.exists())
        self.run_install("--overwrite")
        self.assertEqual(target.read_bytes(), original)
        self.assertEqual(unrelated.read_text(encoding="utf-8"), "keep")

    def test_unsafe_paths_and_source_links_are_rejected(self) -> None:
        """Reject traversal, destination links, hardlinks, and source links."""
        outside = self.home / "outside"
        outside.mkdir()
        self.destination.symlink_to(outside, target_is_directory=True)
        self.assertIn("Linked path", self.run_install(code=1).stderr)
        self.assertEqual(list(outside.iterdir()), [])
        self.destination.unlink()
        self.run_install("--destination", str(outside / "../escape"), code=1)
        self.assertFalse((self.home / "escape").exists())
        self.run_install()
        target = self.destination / "skills/sleuth_common.py"
        target.unlink()
        victim = outside / "keep.py"
        victim.write_text("keep", encoding="utf-8")
        target.symlink_to(victim)
        self.run_install("--overwrite", code=1)
        target.unlink()
        os.link(victim, target)
        self.run_install("--overwrite", code=1)
        self.assertEqual(victim.read_text(encoding="utf-8"), "keep")

        source = self.home / "checkout"
        source.mkdir()
        shutil.copy2(ROOT / "install.py", source)
        shutil.copytree(ROOT / "agents", source / "agents")
        shutil.copytree(ROOT / "skills", source / "skills")
        linked = source / "skills/sleuth_common.py"
        linked.unlink()
        linked.symlink_to(victim)
        destination = self.home / "fresh"
        self.run_install("--destination", str(destination), root=source, code=1)
        self.assertFalse(destination.exists())
        self.run_install(
            "--destination", str(source / "nested"), root=source, code=1
        )


if __name__ == "__main__":
    unittest.main()
