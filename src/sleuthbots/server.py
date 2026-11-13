"""Serve a startup snapshot of bundled files; never execute their contents."""

import stat
from collections.abc import Callable
from pathlib import Path

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError

# Content lives inside the package so installed wheels need no source checkout.
CONTENT_ROOT = Path(__file__).parent / "content"
RESOURCE_PREFIX = "sleuthbots://bundle/"


def load_bundle(root: Path) -> dict[str, str]:
    """Snapshot UTF-8 files, rejecting linked or nonregular storage.

    The installation must be trusted and not concurrently modified at startup.
    Requests use exact snapshot keys, never filesystem paths.
    """
    root = root.absolute()
    files: dict[str, str] = {}
    for path in [root, *sorted(root.rglob("*"))]:
        # Check ancestors too, including a symlinked content root.
        for component in (*reversed(path.parents), path):
            info = component.lstat()
            if stat.S_ISLNK(info.st_mode) or (
                getattr(info, "st_file_attributes", 0)
                & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
            ):
                raise ValueError("Linked bundle paths are not supported.")
        if stat.S_ISDIR(info.st_mode):
            continue
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError("Expected an unlinked regular bundle file.")
        # Python may create caches when a bundled CLI is used separately.
        if "__pycache__" in path.parts or path.suffix == ".pyc":
            continue
        if not path.resolve().is_relative_to(root.resolve()):
            raise ValueError("Bundle file is outside the content directory.")
        files[path.relative_to(root).as_posix()] = path.read_bytes().decode(
            "utf-8"
        )
    if not files:
        raise ValueError("The bundled content is missing or empty.")
    return files


def text_provider(text: str) -> Callable[[], str]:
    """Bind immutable text without exposing a path parameter to MCP clients."""

    def provide() -> str:
        """Return the original bundled text without interpolation."""
        return text

    return provide


def create_server() -> FastMCP:
    """Register two read-only tools, file resources, and agent/skill prompts."""
    bundle = load_bundle(CONTENT_ROOT)
    mcp = FastMCP(
        "sleuthbots",
        instructions=(
            "Browse bundled investigation instructions with list_files and "
            "read_file, resources, or agent/skill prompts. This server does "
            "not run models, subagents, bundled scripts, or investigations. "
            "The host must supply any authorized execution tools."
        ),
    )

    @mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": False})
    def list_files() -> list[str]:
        """List exact relative paths accepted by read_file."""
        return sorted(bundle)

    @mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": False})
    def read_file(path: str) -> str:
        """Read one bundled UTF-8 file by its exact list_files path."""
        if path not in bundle:
            raise ToolError("Unknown bundled file; use a path from list_files.")
        return bundle[path]

    for path, text in bundle.items():
        mcp.resource(
            RESOURCE_PREFIX + path,
            name=path,
            description="Verbatim bundled file: " + path,
            mime_type="text/plain; charset=utf-8",
        )(text_provider(text))

        # Only agent definitions and skill manuals become no-argument prompts.
        parts = Path(path).parts
        if len(parts) == 2 and parts[0] == "agents":
            name = "agent-" + parts[1].removesuffix(".agent.md")
        elif (
            len(parts) == 3 and parts[0] == "skills" and parts[2] == "SKILL.md"
        ):
            name = "skill-" + parts[1]
        else:
            continue
        mcp.prompt(
            name=name,
            description="Verbatim instructions from " + path,
        )(text_provider(text))

    return mcp


def main() -> None:
    """Serve MCP over stdio; framework diagnostics go to stderr."""
    create_server().run(transport="stdio", show_banner=False)
