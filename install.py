#!/usr/bin/env python3
"""Install native Copilot agents and skills using only Python 3.10+."""

import argparse
import os
import stat
import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path

# These names define the bundled agents and their sibling skill directories.
ROOT = Path(__file__).absolute().parent
ROLES = ("coordinator", "skeptic", "theorist")
SKILLS = ("sleuth-workspace-cli", "sleuth-case-cli", "sleuth-theory-cli")


def check_path(path: Path, directory: bool = False) -> None:
    """Reject traversal, links, special files, and non-directory ancestors."""
    if ".." in path.parts:
        raise ValueError(f"Parent traversal is not allowed: {path}")
    for item in (*reversed(path.parents), path):
        try:
            info = item.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or (
            getattr(info, "st_file_attributes", 0)
            & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
        ):
            raise ValueError(f"Linked path is not allowed: {item}")
        is_directory = item != path or directory
        if is_directory and not stat.S_ISDIR(info.st_mode):
            raise ValueError(f"Expected a directory: {item}")
        if not is_directory and (
            not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
        ):
            raise ValueError(f"Expected an unlinked regular file: {item}")


def skill_files(directory: Path) -> Iterator[Path]:
    """Yield every skill support file, excluding Python runtime caches."""
    check_path(directory, directory=True)
    for path in sorted(directory.iterdir()):
        check_path(path, directory=path.is_dir())
        if path.name == "__pycache__" or path.suffix in (".pyc", ".pyo"):
            continue
        if path.is_dir():
            yield from skill_files(path)
        else:
            yield path


def payload() -> dict[Path, bytes]:
    """Load preserved payload, adapting only installed agent dispatch IDs."""
    files = {}
    for role in ROLES:
        source = ROOT / "agents" / f"{role}.agent.md"
        check_path(source)
        content = source.read_bytes()
        for target in ROLES:
            content = content.replace(
                f"sleuthbots:{target}".encode(),
                f"sleuthbots-{target}".encode(),
            )
        files[Path("agents") / f"sleuthbots-{role}.agent.md"] = content

    common = ROOT / "skills" / "sleuth_common.py"
    check_path(common)
    files[common.relative_to(ROOT)] = common.read_bytes()
    for name in SKILLS:
        directory = ROOT / "skills" / name
        # Required entry points must exist even if a checkout is incomplete.
        for filename in ("SKILL.md", f"{name}.py"):
            required = directory / filename
            check_path(required)
            if not required.is_file():
                raise ValueError(f"Missing skill entry point: {required}")
        for source in skill_files(directory):
            files[source.relative_to(ROOT)] = source.read_bytes()
    return files


def install(destination: Path, overwrite: bool = False) -> tuple[int, int]:
    """Preflight all paths, then copy individual files, never directories."""
    destination = destination.expanduser().absolute()
    check_path(destination, directory=True)
    if destination.is_relative_to(ROOT) or ROOT.is_relative_to(destination):
        raise ValueError("Destination must not overlap the source checkout.")
    files = payload()
    pending = []
    unchanged = 0
    for relative, content in files.items():
        target = destination / relative
        check_path(target)
        if target.is_file():
            if target.read_bytes() == content:
                unchanged += 1
                continue
            if not overwrite:
                raise ValueError(
                    f"Conflict: {target}; use --overwrite to replace this file."
                )
        pending.append((target, content))

    for target, content in pending:
        target.parent.mkdir(parents=True, exist_ok=True)
        check_path(target)
        if not overwrite:
            # Exclusive creation also protects files created after preflight.
            with target.open("xb") as stream:
                stream.write(content)
            continue
        # Replace only this file, without truncating existing contents on error.
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(
                dir=target.parent, delete=False
            ) as stream:
                temporary = Path(stream.name)
                stream.write(content)
            os.replace(temporary, target)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
    return len(pending), unchanged


def main() -> int:
    """Expose help, a short installation result, and actionable failures."""
    parser = argparse.ArgumentParser(
        description=__doc__,
        epilog=(
            "Installs agents/sleuthbots-*.agent.md and skills/ with their "
            "shared module. Identical files are left untouched. Restart "
            "Copilot, then use /agent and /skills list. "
            "Use trusted local directories; concurrent installs "
            "are unsupported."
        ),
    )
    parser.add_argument(
        "--destination",
        type=Path,
        default=Path.home() / ".copilot",
        metavar="DIR",
        help="Copilot user directory (default: ~/.copilot)",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="replace conflicting payload files only; never follow links",
    )
    args = parser.parse_args()
    try:
        copied, unchanged = install(args.destination, args.overwrite)
    except (OSError, ValueError) as error:
        print(f"Installation failed: {error}", file=sys.stderr)
        return 1
    print(
        f"Installed 3 agents and 3 skills: {copied} copied, "
        f"{unchanged} unchanged. Restart Copilot; use /agent and /skills list."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
