"""Local Markdown persistence and argument handling for the three CLI skills.

The workspace is trusted, user-owned storage, not a multi-user security boundary.
No investigative commands, integrations, editors, or network calls run here.
"""

import argparse
import json
import os
import re
import secrets
import stat
import sys
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from datetime import datetime, timezone
from pathlib import Path

# Record schemas deliberately have no format versions or revision counters.
ID_PATTERN = re.compile(r"[0-9a-f]{16}")
TIME_PATTERN = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?Z")
ENTRY_HEADING_PATTERN = re.compile(r"### (\S+) — (.+)")
SECTION_HEADING_PATTERN = re.compile(r"^ {0,3}#{1,2}(?:[ \t]|$)")
BASE_FIELDS = {
    "id",
    "title",
    "description",
    "created_at",
    "updated_at",
}
CASE_FIELDS = BASE_FIELDS | {
    "summary",
    "blocked_reason",
    "round_limit",
    "rounds_completed",
}
THEORY_FIELDS = BASE_FIELDS | {"case_id", "verdict", "conclusion"}
STATES = ("open", "resolved", "blocked")
VERDICTS = ("unknown", "accepted", "disproven")
DEFAULT_ROUND_LIMIT = 15


class SleuthError(Exception):
    """A safe diagnostic with a stable process exit code."""

    def __init__(
        self,
        message: str,
        exit_code: int = 2,
        code: str = "invalid",
        path: Path | None = None,
        field: str | None = None,
    ) -> None:
        super().__init__(message)
        self.exit_code = exit_code
        self.details = {"code": code, "message": message}
        if path is not None:
            self.details["path"] = str(path)
        if field is not None:
            self.details["field"] = field


def require(condition: bool, message: str, field: str | None = None) -> None:
    """Reject invalid arguments or stored data without printing their contents."""
    if not condition:
        raise SleuthError(message, field=field)


def label(value: str, field: str) -> str:
    """Validate a nonempty single-line title or author label."""
    require(
        isinstance(value, str)
        and bool(value.strip())
        and not any(c in value for c in "\r\n\x00"),
        "Expected a nonempty single-line label.",
        field,
    )
    return value


def nonempty(value: str, field: str) -> str:
    """Validate nonempty prose without imposing a reasoning schema."""
    require(
        isinstance(value, str) and bool(value.strip()) and "\x00" not in value,
        "Expected nonempty text.",
        field,
    )
    return value


def identifier(value: str) -> str:
    """Require a complete lowercase hexadecimal ID before constructing paths."""
    require(
        isinstance(value, str) and ID_PATTERN.fullmatch(value) is not None,
        "Expected a full 16-character lowercase hexadecimal ID.",
        "id",
    )
    return value


def integer(value: int, field: str, minimum: int = 1) -> int:
    """Reject booleans and non-integers, including hand-edited JSON values."""
    require(
        type(value) is int and value >= minimum,
        f"Expected an integer of at least {minimum}.",
        field,
    )
    return value


def timestamp(value: str) -> datetime:
    """Parse an inclusive UTC RFC 3339 timestamp."""
    require(
        isinstance(value, str) and TIME_PATTERN.fullmatch(value) is not None,
        "Expected a UTC RFC 3339 timestamp ending in Z.",
    )
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise SleuthError("Invalid UTC timestamp.") from error


def now() -> str:
    """Return a UTC timestamp with microseconds to preserve edit ordering."""
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def safe_path(
    path: Path, directory: bool = False, missing: bool = False
) -> bool:
    """Check each boundary without following symlinks or reparse points.

    Checks reduce accidental linked-path writes; they do not prevent malicious
    races by another user with access to this directory.
    """
    for component in (*reversed(path.parents), path):
        try:
            info = component.lstat()
        except FileNotFoundError:
            if missing:
                return False
            raise SleuthError(
                "Workspace or record not found.",
                3,
                "missing",
                path=path,
            ) from None
        linked = stat.S_ISLNK(info.st_mode) or (
            getattr(info, "st_file_attributes", 0)
            & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        )
        require(not linked, "Linked storage paths are not supported.")
        if component != path:
            require(
                stat.S_ISDIR(info.st_mode),
                "Storage boundary is not a directory.",
            )
            continue
        if directory:
            require(stat.S_ISDIR(info.st_mode), "Expected a storage directory.")
        else:
            require(
                stat.S_ISREG(info.st_mode) and info.st_nlink == 1,
                "Expected an unlinked regular file.",
            )
    return True


def read_text(path: Path) -> str:
    """Read UTF-8 only after checking file type and linked-file hazards."""
    safe_path(path)
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeError as error:
        raise SleuthError("Input must be valid UTF-8.", path=path) from error


def strict_json(text: str) -> dict:
    """Parse only a JSON object, rejecting duplicate keys and nonfinite values."""

    def pairs(values: list) -> dict:
        result = {}
        for key, value in values:
            require(key not in result, "Duplicate JSON metadata key.")
            result[key] = value
        return result

    def constant(_: str) -> None:
        raise SleuthError("Nonfinite JSON numbers are unsupported.")

    try:
        result = json.loads(
            text,
            object_pairs_hook=pairs,
            parse_constant=constant,
        )
    except (ValueError, RecursionError) as error:
        raise SleuthError("Malformed JSON metadata.") from error
    require(isinstance(result, dict), "Metadata must be a JSON object.")
    return result


def atomic_write(path: Path, text: str) -> None:
    """Flush a same-directory temporary file and replace one complete file."""
    safe_path(path.parent, directory=True)
    safe_path(path, missing=True)
    temporary = None
    try:
        fd, name = tempfile.mkstemp(prefix=".sleuth-", dir=path.parent)
        temporary = Path(name)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        safe_path(path, missing=True)
        os.replace(temporary, path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


@contextmanager
def workspace_lock(workspace: Path) -> Iterator[None]:
    """Fail fast on contention and release only the owning operation's lock."""
    safe_path(workspace, directory=True)
    path = workspace / ".lock"
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as error:
        raise SleuthError(
            "Workspace is locked; do not remove it until no writer is active.",
            4,
            "locked",
            path=path,
        ) from error
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(json.dumps({"pid": os.getpid(), "created_at": now()}))
        yield
    finally:
        path.unlink()


def load_config(workspace: Path) -> dict:
    """Require initialized, well-formed workspace artifacts."""
    safe_path(workspace, directory=True)
    safe_path(workspace / "cases", directory=True)
    require(
        read_text(workspace / ".gitignore") == "*\n",
        "Conflicting workspace ignore rules; correct them explicitly.",
    )
    return read_config(workspace / "config.json")


def read_config(path: Path) -> dict:
    """Validate config even when other initialization artifacts are missing."""
    config = strict_json(read_text(path))
    require(
        set(config) == {"default_round_limit"},
        "Unsupported configuration fields.",
    )
    integer(config["default_round_limit"], "default_round_limit")
    return config


def validate_record(
    metadata: dict, body: str, kind: str, record_id: str, case_id: str
) -> None:
    """Check the exact schema and immutable identity against the record path."""
    fields = CASE_FIELDS if kind == "case" else THEORY_FIELDS
    require(set(metadata) == fields, "Missing or unsupported record fields.")
    require(
        metadata["id"] == identifier(record_id), "Record identity mismatch."
    )
    label(metadata["title"], "title")
    require(
        isinstance(metadata["description"], str), "Description must be text."
    )
    created = timestamp(metadata["created_at"])
    require(
        timestamp(metadata["updated_at"]) >= created, "Invalid edit timeline."
    )
    section = "Overview" if kind == "case" else "Hypothesis"
    lines = list(unfenced_lines(body))
    discussion = discussion_sections(body)
    require(
        body.startswith(f'# {metadata["title"]}\n')
        and [line for _, line in lines if line.startswith("# ")]
        == [f'# {metadata["title"]}']
        and len(discussion) == 1
        and any(
            0 < index < discussion[0] and line == f"## {section}"
            for index, line in lines
        ),
        "Expected managed title, overview/hypothesis, and one discussion section.",
    )
    if kind == "case":
        require(isinstance(metadata["summary"], str), "Summary must be text.")
        reason = metadata["blocked_reason"]
        if reason is not None:
            nonempty(reason, "blocked_reason")
        integer(metadata["round_limit"], "round_limit")
        integer(metadata["rounds_completed"], "rounds_completed", 0)
        require(
            metadata["rounds_completed"] <= metadata["round_limit"],
            "Completed rounds exceed the case limit.",
        )
        return
    require(
        metadata["case_id"] == identifier(case_id), "Containing case mismatch."
    )
    require(metadata["verdict"] in VERDICTS, "Invalid theory verdict.")
    require(isinstance(metadata["conclusion"], str), "Conclusion must be text.")
    if metadata["verdict"] == "unknown":
        require(
            metadata["conclusion"] == "", "Unknown conclusion must be empty."
        )
    else:
        nonempty(metadata["conclusion"], "conclusion")


def unfenced_lines(body: str) -> Iterator[tuple[int, str]]:
    """Yield lines outside CommonMark-style backtick and tilde fenced blocks."""
    fence_char = None
    fence_length = 0
    for index, line in enumerate(body.splitlines()):
        match = re.match(r"^ {0,3}(`{3,}|~{3,})(.*)$", line)
        if match:
            marker, rest = match.groups()
            if fence_char is None:
                fence_char, fence_length = marker[0], len(marker)
                continue
            if (
                marker[0] == fence_char
                and len(marker) >= fence_length
                and not rest.strip()
            ):
                fence_char = None
                continue
        if fence_char is None:
            yield index, line


def discussion_sections(body: str) -> list[int]:
    """Locate the managed discussion section, ignoring fenced heading text."""
    return [
        index for index, line in unfenced_lines(body) if line == "## Discussion"
    ]


def latest_author(body: str) -> str | None:
    """Read the last timestamped author heading inside the discussion section."""
    in_discussion = False
    author = None
    for _, line in unfenced_lines(body):
        if line == "## Discussion":
            in_discussion = True
        elif SECTION_HEADING_PATTERN.match(line):
            in_discussion = False
        elif in_discussion:
            match = ENTRY_HEADING_PATTERN.fullmatch(line)
            if match:
                timestamp(match[1])
                author = label(match[2], "author")
    return author


def append_discussion(body: str, author: str, text: str) -> str:
    """Append within Discussion even if manually authored sections follow it."""
    label(author, "author")
    nonempty(text, "discussion")
    # Normalize incoming newlines but retain all existing body content.
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    require(
        not any(
            SECTION_HEADING_PATTERN.match(line)
            for _, line in unfenced_lines(text)
        ),
        "Discussion text must not contain section-level H1/H2 headings; "
        "use H3-H6 subheadings or fenced raw data.",
    )
    require(
        not any(
            ENTRY_HEADING_PATTERN.fullmatch(line)
            for _, line in unfenced_lines(text)
        ),
        "Discussion text must not contain reserved entry headings; "
        "fence quoted timestamp/author entries.",
    )
    start = discussion_sections(body)[0]
    lines = body.splitlines(keepends=True)
    end = len(lines)
    for index, line in unfenced_lines(body):
        if index > start and SECTION_HEADING_PATTERN.match(line):
            end = index
            break
    heading = f"### {now()} — {author}"
    entry = f"\n{heading}\n\n{text.rstrip()}\n"
    result = (
        "".join(lines[:end]).rstrip("\n")
        + "\n"
        + entry
        + ("\n" if end < len(lines) else "")
        + "".join(lines[end:])
    )
    require(
        any(line == heading for _, line in unfenced_lines(result)),
        "Close existing fenced blocks before appending discussion.",
    )
    return result


def record_path(
    workspace: Path, case_id: str, kind: str, record_id: str | None = None
) -> Path:
    """Construct a contained path from validated full IDs."""
    case_id = identifier(case_id)
    record_id = identifier(record_id or case_id)
    directory = workspace / "cases" / case_id
    safe_path(directory, directory=True)
    return directory / f"{record_id}.sleuth.{kind}.md"


def read_record(path: Path, kind: str, case_id: str) -> dict:
    """Read the first standalone-delimited JSON object and preserve its body."""
    text = read_text(path)
    lines = text.splitlines(keepends=True)
    require(
        bool(lines) and lines[0].rstrip("\n") == "---",
        "Missing opening delimiter.",
    )
    end = next(
        (i for i in range(1, len(lines)) if lines[i].rstrip("\n") == "---"),
        None,
    )
    require(end is not None, "Missing closing delimiter.")
    metadata = strict_json("".join(lines[1:end]))
    body = "".join(lines[end + 1 :]).lstrip("\n")
    record_id = path.name.removesuffix(f".sleuth.{kind}.md")
    validate_record(metadata, body, kind, record_id, case_id)
    return {"path": str(path), "metadata": metadata, "body_markdown": body}


def write_record(record: dict, kind: str, case_id: str) -> None:
    """Validate and atomically persist one stored record without computed fields."""
    metadata = record["metadata"]
    path = Path(record["path"])
    validate_record(
        metadata,
        record["body_markdown"],
        kind,
        path.name.removesuffix(f".sleuth.{kind}.md"),
        case_id,
    )
    text = "---\n" + json.dumps(metadata, ensure_ascii=False, indent=2)
    atomic_write(path, text + "\n---\n\n" + record["body_markdown"])


def theories(workspace: Path, case_id: str) -> list[dict]:
    """Read only immediate theory files, never linked directories."""
    directory = record_path(workspace, case_id, "case").parent
    return [
        read_record(path, "theory", case_id)
        for path in directory.glob("*.sleuth.theory.md")
    ]


def derived_case(workspace: Path, record: dict) -> dict:
    """Compute acceptance and state; reject contradictory hand-edited records."""
    metadata = dict(record["metadata"])
    accepted = sorted(
        theory["metadata"]["id"]
        for theory in theories(workspace, metadata["id"])
        if theory["metadata"]["verdict"] == "accepted"
    )
    require(
        not (accepted and metadata["blocked_reason"]),
        "Accepted theories conflict with a blocked reason; pause for human correction.",
    )
    metadata["accepted_theory_ids"] = accepted
    metadata["state"] = (
        "resolved"
        if accepted
        else "blocked" if metadata["blocked_reason"] else "open"
    )
    return {**record, "metadata": metadata}


def new_id(directory: Path, kind: str) -> str:
    """Check random collisions before allocating a case or theory filename."""
    for _ in range(100):
        value = secrets.token_hex(8)
        path = directory / (
            value if kind == "case" else f"{value}.sleuth.theory.md"
        )
        if not os.path.lexists(path):
            return value
    raise SleuthError("Unable to allocate an unused record ID.", 5, "io")


class Parser(argparse.ArgumentParser):
    """Route argparse failures through the same diagnostic envelope."""

    def error(self, message: str) -> None:
        # Do not echo invalid argument values, which might contain private data.
        raise SleuthError("Invalid arguments; use --help for command syntax.")


def input_option(parser: argparse.ArgumentParser, name: str) -> None:
    """Add an exclusive literal-text or explicit UTF-8 stdin option."""
    group = parser.add_mutually_exclusive_group()
    group.add_argument(f"--{name}")
    group.add_argument(f"--{name}-stdin", action="store_true")


def build_parser(kind: str) -> Parser:
    """Build the small, explicit command surface for one CLI skill."""
    parser = Parser(description=f"Sleuthbots {kind} records")
    parser.add_argument("--workspace-path")
    parser.add_argument("--json", action="store_true")
    commands = parser.add_subparsers(dest="action", required=True)
    if kind == "workspace":
        commands.add_parser("init")
        commands.add_parser("show")
        configure = commands.add_parser("configure")
        configure.add_argument("--default-round-limit", required=True, type=int)
        return parser
    for action in (
        "create",
        "get",
        "update",
        "list",
        "query",
        "add-discussion",
    ):
        command = commands.add_parser(action)
        if kind == "theory":
            command.add_argument("--case-id", required=True)
        if action in ("get", "update", "add-discussion"):
            command.add_argument("--id", required=True)
        if action in ("create", "update"):
            command.add_argument("--title", required=action == "create")
            input_option(command, "description")
            if kind == "case":
                command.add_argument("--round-limit", type=int)
            if action == "update":
                command.add_argument("--author", required=True)
                command.add_argument("--reason")
                if kind == "case":
                    input_option(command, "summary")
                    command.add_argument("--state", choices=STATES)
                    command.add_argument("--blocked-reason")
                    command.add_argument("--rounds-completed", type=int)
                else:
                    command.add_argument("--verdict", choices=VERDICTS)
                    command.add_argument("--conclusion")
        if action == "add-discussion":
            command.add_argument("--author", required=True)
            group = command.add_mutually_exclusive_group(required=True)
            group.add_argument("--text")
            group.add_argument("--stdin", action="store_true")
        if action in ("list", "query"):
            field = "state" if kind == "case" else "verdict"
            command.add_argument(
                f"--{field}", choices=STATES if kind == "case" else VERDICTS
            )
            command.add_argument("--title")
            command.add_argument("--text", required=action == "query")
            command.add_argument("--created-after")
            command.add_argument("--updated-after")
            command.add_argument("--limit", type=int)
    return parser


def stdin_text() -> str:
    """Read explicitly requested UTF-8 stdin to EOF without prompting."""
    try:
        if hasattr(sys.stdin, "buffer"):
            return sys.stdin.buffer.read().decode("utf-8")
        return sys.stdin.read()
    except UnicodeError as error:
        raise SleuthError("Stdin must be valid UTF-8.") from error


def init_workspace(workspace: Path) -> dict:
    """Add missing artifacts without clobbering any existing workspace data."""
    created = []
    if not safe_path(workspace, directory=True, missing=True):
        workspace.mkdir(parents=True, mode=0o700)
        created.append(str(workspace))
    with workspace_lock(workspace):
        # Validate all existing artifacts before creating any missing ones.
        cases = workspace / "cases"
        exists_cases = safe_path(cases, directory=True, missing=True)
        config_path = workspace / "config.json"
        exists_config = safe_path(config_path, missing=True)
        if exists_config:
            read_config(config_path)
        ignore_path = workspace / ".gitignore"
        exists_ignore = safe_path(ignore_path, missing=True)
        if exists_ignore:
            require(
                read_text(ignore_path) == "*\n",
                "Conflicting workspace ignore rules.",
            )
        if not exists_cases:
            cases.mkdir(mode=0o700)
            created.append(str(cases))
        for path, exists, text in (
            (
                config_path,
                exists_config,
                json.dumps({"default_round_limit": DEFAULT_ROUND_LIMIT}) + "\n",
            ),
            (ignore_path, exists_ignore, "*\n"),
        ):
            if not exists:
                atomic_write(path, text)
                created.append(str(path))
        config = load_config(workspace)
    return {
        "path": str(workspace),
        "initialized": True,
        "config": config,
        "created": created,
    }


def create_record(
    workspace: Path, args: argparse.Namespace, kind: str, config: dict
) -> dict:
    """Create an unknown theory or an open case with a copied round budget."""
    title = label(args.title, "title")
    description = args.description if args.description is not None else ""
    if kind == "case":
        directory = workspace / "cases"
        record_id = new_id(directory, kind)
        case_id = record_id
        limit = (
            args.round_limit
            if args.round_limit is not None
            else config["default_round_limit"]
        )
        integer(limit, "round_limit")
        directory = directory / record_id
    else:
        case_id = identifier(args.case_id)
        case = read_record(
            record_path(workspace, case_id, "case"), "case", case_id
        )
        derived_case(workspace, case)
        directory = Path(case["path"]).parent
        record_id = new_id(directory, kind)
    time = now()
    metadata = {
        "id": record_id,
        "title": title,
        "description": description,
        "created_at": time,
        "updated_at": time,
    }
    if kind == "case":
        metadata.update(
            summary="",
            blocked_reason=None,
            round_limit=limit,
            rounds_completed=0,
        )
        section = "Overview"
    else:
        metadata.update(case_id=case_id, verdict="unknown", conclusion="")
        section = "Hypothesis"
    body = f"# {title}\n\n## {section}\n\n{description}\n\n## Discussion\n"
    validate_record(metadata, body, kind, record_id, case_id)
    record = {
        "path": str(directory / f"{record_id}.sleuth.{kind}.md"),
        "metadata": metadata,
        "body_markdown": body,
    }
    if kind == "case":
        directory.mkdir(mode=0o700)
    try:
        write_record(record, kind, case_id)
    except BaseException:
        if kind == "case":
            # rmdir cannot remove nonempty data; preserve the original failure
            # if the newly allocated directory can no longer be removed safely.
            with suppress(OSError):
                directory.rmdir()
        raise
    return record


def patch_case(workspace: Path, record: dict, args: argparse.Namespace) -> None:
    """Apply justified lifecycle and budget edits to only the case document."""
    metadata = record["metadata"]
    accepted = derived_case(workspace, record)["metadata"][
        "accepted_theory_ids"
    ]
    fields = (
        "title",
        "description",
        "summary",
        "state",
        "round_limit",
        "rounds_completed",
    )
    require(
        any(getattr(args, field) is not None for field in fields),
        "No patch fields supplied.",
    )
    require(
        args.blocked_reason is None or args.state == "blocked",
        "Blocked reason is only valid with --state blocked.",
    )
    needs_reason = (
        args.state is not None
        or args.round_limit is not None
        or args.rounds_completed is not None
    )
    if needs_reason:
        nonempty(args.reason, "reason")
    if args.state is not None:
        if args.state == "resolved":
            require(bool(accepted), "Resolved requires an accepted theory.")
        else:
            require(
                not accepted,
                "Reopen accepted theories before changing case state.",
            )
        if args.state == "blocked":
            metadata["blocked_reason"] = nonempty(
                args.blocked_reason, "blocked_reason"
            )
        else:
            metadata["blocked_reason"] = None
    for field in ("summary", "round_limit", "rounds_completed"):
        value = getattr(args, field)
        if value is not None:
            metadata[field] = value
    if args.reason is not None:
        record["body_markdown"] = append_discussion(
            record["body_markdown"],
            args.author,
            nonempty(args.reason, "reason"),
        )


def patch_theory(
    workspace: Path, record: dict, args: argparse.Namespace
) -> None:
    """Enforce current-claim assessment, finalization, and justified reopening."""
    metadata = record["metadata"]
    changed_claim = any(
        getattr(args, field) is not None
        and getattr(args, field) != metadata[field]
        for field in ("title", "description")
    )
    require(
        args.title is not None
        or args.description is not None
        or args.verdict is not None,
        "No patch fields supplied.",
    )
    require(
        args.conclusion is None or args.verdict in ("accepted", "disproven"),
        "Conclusion is only valid with a final verdict.",
    )
    if changed_claim:
        require(
            metadata["verdict"] == "unknown",
            "Reopen a final claim before editing it.",
        )
        require(
            args.verdict is None,
            "Do not change the claim and verdict together.",
        )
        record["body_markdown"] = append_discussion(
            record["body_markdown"],
            f"{args.author} (claim change)",
            "Claim changed. Reassess the current title and description before deciding.",
        )
    if args.verdict is None:
        require(
            args.reason is None,
            "Reason is only valid with a verdict transition.",
        )
        return
    label(args.author, "author")
    require(args.author == "Skinner", "Skinner records verdict transitions.")
    nonempty(args.reason, "reason")
    old = metadata["verdict"]
    if args.verdict == "unknown":
        require(old != "unknown", "Theory is already unknown.")
        note = (
            f'Reopened from {old}.\n\nPrevious conclusion:\n\n{metadata["conclusion"]}'
            f"\n\nJustification: {args.reason}"
        )
        metadata["conclusion"] = ""
    else:
        require(old == "unknown", "Reopen before another final decision.")
        require(not changed_claim, "Do not finalize a changed claim.")
        require(
            latest_author(record["body_markdown"]) == "Scully",
            "Latest discussion entry must be Scully's current assessment.",
        )
        conclusion = nonempty(args.conclusion, "conclusion")
        case_id = metadata["case_id"]
        case = read_record(
            record_path(workspace, case_id, "case"), "case", case_id
        )
        derived_case(workspace, case)
        if args.verdict == "accepted":
            require(
                case["metadata"]["blocked_reason"] is None,
                "Explicitly open the blocked case with justification before acceptance.",
            )
        metadata["conclusion"] = conclusion
        note = f"Decision: {args.verdict}.\n\n{conclusion}\n\nReason: {args.reason}"
    metadata["verdict"] = args.verdict
    record["body_markdown"] = append_discussion(
        record["body_markdown"], args.author, note
    )


def update_record(
    workspace: Path, record: dict, args: argparse.Namespace, kind: str
) -> None:
    """Patch supplied fields, preserving identity and every existing discussion."""
    label(args.author, "author")
    if args.title is not None:
        label(args.title, "title")
    if kind == "case":
        patch_case(workspace, record, args)
    else:
        patch_theory(workspace, record, args)
    metadata = record["metadata"]
    if args.title is not None:
        old_h1 = f'# {metadata["title"]}\n'
        record["body_markdown"] = (
            f"# {args.title}\n" + record["body_markdown"][len(old_h1) :]
        )
        metadata["title"] = args.title
    if args.description is not None:
        section = "Overview" if kind == "case" else "Hypothesis"
        prefix = f'# {metadata["title"]}\n\n## {section}\n\n'
        old_prefix = prefix + metadata["description"] + "\n\n"
        if record["body_markdown"].startswith(old_prefix):
            record["body_markdown"] = (
                prefix
                + args.description
                + "\n\n"
                + record["body_markdown"][len(old_prefix) :]
            )
        metadata["description"] = args.description
    metadata["updated_at"] = now()
    case_id = metadata["id"] if kind == "case" else metadata["case_id"]
    write_record(record, kind, case_id)


def list_records(workspace: Path, args: argparse.Namespace, kind: str) -> dict:
    """Apply AND literal filters, inclusive timestamps, ordering, then a limit."""
    bounds = {}
    for field in ("created", "updated"):
        value = getattr(args, f"{field}_after")
        if value is not None:
            bounds[f"{field}_at"] = timestamp(value)
    if args.limit is not None:
        integer(args.limit, "limit")
    if args.text is not None:
        nonempty(args.text, "text")
    if kind == "case":
        records = []
        for directory in (workspace / "cases").iterdir():
            identifier(directory.name)
            safe_path(directory, directory=True)
            record = read_record(
                record_path(workspace, directory.name, "case"),
                "case",
                directory.name,
            )
            records.append(derived_case(workspace, record))
        status_field = "state"
    else:
        case_id = identifier(args.case_id)
        case = read_record(
            record_path(workspace, case_id, "case"), "case", case_id
        )
        derived_case(workspace, case)
        records = theories(workspace, case_id)
        status_field = "verdict"
    matches = []
    for record in records:
        metadata = record["metadata"]
        status = getattr(args, status_field)
        if status is not None and status != metadata[status_field]:
            continue
        if (
            args.title is not None
            and args.title.casefold() not in metadata["title"].casefold()
        ):
            continue
        text = "\n".join(
            [
                metadata["title"],
                metadata["description"],
                record["body_markdown"],
                metadata["summary" if kind == "case" else "conclusion"],
            ]
        )
        if (
            args.text is not None
            and args.text.casefold() not in text.casefold()
        ):
            continue
        if any(
            timestamp(metadata[field]) < bound
            for field, bound in bounds.items()
        ):
            continue
        matches.append({"path": record["path"], "metadata": metadata})
    matches.sort(
        key=lambda item: (
            timestamp(item["metadata"]["created_at"]),
            item["metadata"]["id"],
        )
    )
    return {"items": matches[: args.limit], "total_matches": len(matches)}


def execute(kind: str, args: argparse.Namespace, workspace: Path) -> dict:
    """Execute a short operation under the single shared workspace lock."""
    if kind == "workspace" and args.action == "init":
        return init_workspace(workspace)
    if kind == "workspace" and args.action == "show":
        if not safe_path(workspace, directory=True, missing=True):
            return {
                "path": str(workspace),
                "initialized": False,
                "config": None,
            }
        # A partially initialized workspace is visible but not silently repaired.
        with workspace_lock(workspace):
            complete = all(
                os.path.lexists(workspace / name)
                for name in ("cases", "config.json", ".gitignore")
            )
            if complete:
                config = load_config(workspace)
            else:
                config = None
                for name in ("cases", "config.json", ".gitignore"):
                    safe_path(
                        workspace / name,
                        directory=name == "cases",
                        missing=True,
                    )
                if os.path.lexists(workspace / "config.json"):
                    config = read_config(workspace / "config.json")
                if os.path.lexists(workspace / ".gitignore"):
                    require(
                        read_text(workspace / ".gitignore") == "*\n",
                        "Conflicting workspace ignore rules.",
                    )
            return {
                "path": str(workspace),
                "initialized": complete,
                "config": config,
            }
    with workspace_lock(workspace):
        config = load_config(workspace)
        if kind == "workspace":
            config["default_round_limit"] = integer(
                args.default_round_limit,
                "default_round_limit",
            )
            atomic_write(workspace / "config.json", json.dumps(config) + "\n")
            return {"path": str(workspace), "config": config}
        if args.action in ("list", "query"):
            return list_records(workspace, args, kind)
        if args.action == "create":
            record = create_record(workspace, args, kind, config)
        else:
            case_id = identifier(args.id if kind == "case" else args.case_id)
            case = read_record(
                record_path(workspace, case_id, "case"), "case", case_id
            )
            derived_case(workspace, case)
            record = (
                case
                if kind == "case"
                else read_record(
                    record_path(workspace, case_id, kind, args.id),
                    kind,
                    case_id,
                )
            )
            if args.action == "update":
                update_record(workspace, record, args, kind)
            elif args.action == "add-discussion":
                record["body_markdown"] = append_discussion(
                    record["body_markdown"],
                    args.author,
                    args.text,
                )
                record["metadata"]["updated_at"] = now()
                write_record(record, kind, case_id)
        if kind == "case":
            record = derived_case(workspace, record)
        if args.action != "get":
            return {
                "path": record["path"],
                "id": record["metadata"]["id"],
                "metadata": record["metadata"],
            }
        return record


def main(kind: str, argv: list[str] | None = None) -> int:
    """Emit one JSON envelope or readable output and return a documented status."""
    argv = list(sys.argv[1:] if argv is None else argv)
    json_output = "--json" in argv
    command = kind
    writing_output = False
    try:
        for stream in (sys.stdout, sys.stderr):
            reconfigure = getattr(stream, "reconfigure", None)
            if reconfigure is not None:
                try:
                    reconfigure(encoding="utf-8", errors="strict")
                except (OSError, ValueError) as error:
                    raise SleuthError(
                        "Cannot configure UTF-8 output.", 5, "output"
                    ) from error
        args = build_parser(kind).parse_args(argv)
        command = f"{kind}.{args.action}"
        stdin_fields = [
            field
            for field in ("description", "summary")
            if getattr(args, f"{field}_stdin", False)
        ]
        require(
            len(stdin_fields) <= 1, "Only one input field can consume stdin."
        )
        if stdin_fields:
            setattr(args, stdin_fields[0], stdin_text())
        if getattr(args, "stdin", False):
            args.text = stdin_text()
        workspace = Path(os.path.abspath(args.workspace_path or ".sleuthbots"))
        data = execute(kind, args, workspace)
        writing_output = True
        result = {"ok": True, "command": command, "data": data}
        if json_output:
            print(json.dumps(result, ensure_ascii=False, allow_nan=False))
        else:
            # JSON indentation is also a legible metadata view; get includes prose.
            body = data.get("body_markdown")
            print(
                json.dumps(
                    {k: v for k, v in data.items() if k != "body_markdown"},
                    ensure_ascii=False,
                    indent=2,
                )
            )
            if body is not None:
                print("\n" + body, end="" if body.endswith("\n") else "\n")
        sys.stdout.flush()
        return 0
    except SleuthError as error:
        failure = error
    except (OSError, UnicodeError):
        failure = SleuthError(
            (
                "Output failed. Reread the record before retrying; "
                "the operation may have completed."
                if writing_output
                else "File operation failed; check permissions, open files, and available space."
            ),
            5,
            "output" if writing_output else "io",
        )
    try:
        if json_output:
            print(
                json.dumps(
                    {"ok": False, "command": command, "error": failure.details}
                )
            )
            sys.stdout.flush()
        else:
            print(f"Error: {failure}", file=sys.stderr)
    except (OSError, UnicodeError):
        with suppress(OSError, UnicodeError):
            print(f"Error: {failure}", file=sys.stderr)
    return failure.exit_code
