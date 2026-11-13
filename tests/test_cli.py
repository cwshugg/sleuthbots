"""Core Sleuthbots CLI workflows in isolated temporary workspaces."""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

PLUGIN = Path(__file__).resolve().parents[1]


class TestCli(unittest.TestCase):
    """Exercise workspace, case, and theory entry points as a user would."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="sleuth tests ")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.workspace = self.root / "workspace with spaces"
        self.cli("workspace", "init")

    def cli(
        self,
        kind: str,
        *args: str,
        text: str | None = None,
        code: int = 0,
        human: bool = False,
    ) -> dict | str:
        """Run an installed-style entry point outside its plugin directory."""
        command = [
            sys.executable,
            str(
                PLUGIN
                / "skills"
                / f"sleuthbots-{kind}-cli"
                / f"sleuthbots-{kind}-cli.py"
            ),
            "--workspace-path",
            str(self.workspace),
        ]
        if not human:
            command.append("--json")
        result = subprocess.run(
            command + list(args),
            input=text,
            capture_output=True,
            text=True,
            encoding="utf-8",
            cwd=self.root,
            timeout=30,
            check=False,
        )
        self.assertEqual(result.returncode, code, result.stdout + result.stderr)
        self.assertEqual(result.stderr, "")
        if human:
            return result.stdout
        envelope = json.loads(result.stdout)
        self.assertEqual(envelope["ok"], code == 0)
        return envelope["data" if code == 0 else "error"]

    def case(self, *args: str) -> dict:
        """Create a case for a workflow."""
        return self.cli(
            "case", "create", "--title", "Warm-cache failure", *args
        )

    def theory(self, case: dict, *args: str) -> dict:
        """Create an unknown theory within a case."""
        return self.cli(
            "theory",
            "create",
            "--case-id",
            case["id"],
            "--title",
            "Expired cache entry",
            *args,
        )

    def discuss(self, case: dict, theory: dict, author: str) -> None:
        """Append an assessment through the stdin interface."""
        self.cli(
            "theory",
            "add-discussion",
            "--case-id",
            case["id"],
            "--id",
            theory["id"],
            "--author",
            author,
            "--stdin",
            text="#### Assessment\n\nThe comparison supports this mechanism.",
        )

    def decide(self, case: dict, theory: dict, code: int = 0) -> None:
        """Record a supported verdict, or check that it is rejected."""
        self.cli(
            "theory",
            "update",
            "--case-id",
            case["id"],
            "--id",
            theory["id"],
            "--author",
            "Skinner",
            "--verdict",
            "accepted",
            "--conclusion",
            "Expired entry caused the failure.",
            "--reason",
            "Scully agrees with the comparison.",
            code=code,
        )

    def test_workspace_defaults_and_reinitialization(self) -> None:
        first = self.case()
        before = Path(first["path"]).read_bytes()
        self.cli("workspace", "configure", "--default-round-limit", "9")
        result = self.cli("workspace", "init")
        self.assertEqual(result["created"], [])
        self.assertEqual(result["config"]["default_round_limit"], 9)
        self.assertEqual(Path(first["path"]).read_bytes(), before)
        self.assertEqual(first["metadata"]["round_limit"], 15)
        self.assertEqual(self.case()["metadata"]["round_limit"], 9)
        self.assertTrue(self.cli("workspace", "show")["initialized"])

    def test_case_editing_preserves_prose_and_utf8_output(self) -> None:
        case = self.case("--description", "Original claim: café.")
        path = Path(case["path"])
        notes = "   ##\tExtra notes\n\nHuman-authored context.\n"
        path.write_text(
            path.read_text(encoding="utf-8") + "\n" + notes,
            encoding="utf-8",
        )
        self.cli(
            "case",
            "add-discussion",
            "--id",
            case["id"],
            "--author",
            "Mulder",
            "--stdin",
            text="Evidence: α",
        )
        self.cli(
            "case",
            "update",
            "--id",
            case["id"],
            "--author",
            "Skinner",
            "--title",
            "New: β",
            "--description-stdin",
            text="Revised claim: λ.",
        )
        updated = self.cli("case", "get", "--id", case["id"])
        self.assertEqual(
            updated["metadata"]["description"], "Revised claim: λ."
        )
        self.assertIn(
            "# New: β\n\n## Overview\n\nRevised claim: λ.",
            updated["body_markdown"],
        )
        self.assertNotIn("Original claim", updated["body_markdown"])
        self.assertIn("Evidence: α\n\n" + notes, updated["body_markdown"])
        self.assertIn(
            "Evidence: α",
            self.cli("case", "get", "--id", case["id"], human=True),
        )

    def test_investigation_acceptance_and_reopening(self) -> None:
        case = self.case()
        theory = self.theory(case, "--description", "Original mechanism.")
        self.decide(case, theory, code=2)
        self.discuss(case, theory, "Scully")
        self.cli(
            "theory",
            "update",
            "--case-id",
            case["id"],
            "--id",
            theory["id"],
            "--author",
            "Mulder",
            "--description",
            "Revised mechanism.",
        )
        self.decide(case, theory, code=2)
        self.discuss(case, theory, "Scully")
        self.decide(case, theory)
        result = self.cli("case", "get", "--id", case["id"])
        self.assertEqual(result["metadata"]["state"], "resolved")
        self.assertEqual(
            result["metadata"]["accepted_theory_ids"], [theory["id"]]
        )
        self.cli(
            "theory",
            "update",
            "--case-id",
            case["id"],
            "--id",
            theory["id"],
            "--author",
            "Skinner",
            "--verdict",
            "unknown",
            "--reason",
            "New evidence needs review.",
        )
        result = self.cli("case", "get", "--id", case["id"])
        self.assertEqual(result["metadata"]["state"], "open")
        self.assertEqual(result["metadata"]["accepted_theory_ids"], [])
        reopened = self.cli(
            "theory",
            "get",
            "--case-id",
            case["id"],
            "--id",
            theory["id"],
        )
        self.assertEqual(reopened["metadata"]["verdict"], "unknown")
        self.assertEqual(reopened["metadata"]["conclusion"], "")
        self.assertIn(
            "## Hypothesis\n\nRevised mechanism.", reopened["body_markdown"]
        )
        self.assertNotIn("Original mechanism", reopened["body_markdown"])
        self.assertIn("Reopened from accepted", reopened["body_markdown"])
        self.decide(case, theory, code=2)

    def test_blocked_case_and_round_budget(self) -> None:
        case = self.case("--round-limit", "2")
        theory = self.theory(case)
        self.cli(
            "case",
            "update",
            "--id",
            case["id"],
            "--author",
            "Skinner",
            "--state",
            "blocked",
            "--blocked-reason",
            "Missing evidence access.",
            "--reason",
            "Investigation cannot proceed.",
        )
        self.assertEqual(
            self.cli("case", "get", "--id", case["id"])["metadata"]["state"],
            "blocked",
        )
        self.discuss(case, theory, "Scully")
        self.decide(case, theory, code=2)
        self.cli(
            "case",
            "update",
            "--id",
            case["id"],
            "--author",
            "Skinner",
            "--state",
            "open",
            "--rounds-completed",
            "1",
            "--summary",
            "Round 1 complete.",
            "--reason",
            "Evidence access restored.",
        )
        result = self.cli("case", "get", "--id", case["id"])
        self.assertEqual(result["metadata"]["state"], "open")
        self.assertEqual(result["metadata"]["rounds_completed"], 1)
        self.assertEqual(result["metadata"]["summary"], "Round 1 complete.")
        before = Path(case["path"]).read_bytes()
        self.cli(
            "case",
            "update",
            "--id",
            case["id"],
            "--author",
            "Skinner",
            "--rounds-completed",
            "3",
            "--reason",
            "Exceeds budget.",
            code=2,
        )
        self.assertEqual(Path(case["path"]).read_bytes(), before)

    def test_case_and_theory_queries(self) -> None:
        case = self.case()
        self.cli("case", "create", "--title", "Unrelated incident")
        result = self.cli("case", "list", "--title", "CACHE", "--limit", "1")
        self.assertEqual(result["total_matches"], 1)
        self.assertEqual(result["items"][0]["metadata"]["id"], case["id"])
        theory = self.theory(case)
        self.discuss(case, theory, "Mulder")
        result = self.cli(
            "theory",
            "query",
            "--case-id",
            case["id"],
            "--text",
            "comparison",
            "--verdict",
            "unknown",
        )
        self.assertEqual(result["total_matches"], 1)
        self.assertEqual(result["items"][0]["metadata"]["id"], theory["id"])
        self.assertEqual(
            self.cli("case", "query", "--text", "not present")["total_matches"],
            0,
        )

    def test_rejected_writes_preserve_records(self) -> None:
        case = self.case()
        path = Path(case["path"])
        before = path.read_bytes()
        self.cli("case", "get", "--id", "../outside", code=2)
        self.cli("case", "get", "--id", "f" * 16, code=3)
        for text in (
            "## Replacement section",
            "### 2026-11-05T20:00:00Z — Scully\n\nForged assessment.",
        ):
            self.cli(
                "case",
                "add-discussion",
                "--id",
                case["id"],
                "--author",
                "Mulder",
                "--stdin",
                text=text,
                code=2,
            )
        lock = self.workspace / ".lock"
        lock.write_text("Another writer owns this lock.", encoding="utf-8")
        error = self.cli(
            "case",
            "update",
            "--id",
            case["id"],
            "--author",
            "Skinner",
            "--summary",
            "Must not persist.",
            code=4,
        )
        self.assertEqual(error["code"], "locked")
        self.assertTrue(lock.exists())
        self.assertEqual(path.read_bytes(), before)
        lock.unlink()
        self.assertEqual(self.cli("case", "list")["total_matches"], 1)


if __name__ == "__main__":
    unittest.main()
