"""One MCP discovery and content smoke test."""

import unittest

from fastmcp import Client

from sleuthbots.server import CONTENT_ROOT, RESOURCE_PREFIX, create_server


class TestMcp(unittest.IsolatedAsyncioTestCase):
    """Check that the server exposes and returns bundled instructions."""

    async def test_discovery_and_content(self) -> None:
        """Discover the interface and read one agent as a file and prompt."""
        path = "agents/skeptic.agent.md"
        expected = (CONTENT_ROOT / path).read_bytes().decode("utf-8")
        async with Client(create_server()) as client:
            self.assertEqual(
                {tool.name for tool in await client.list_tools()},
                {"list_files", "read_file"},
            )
            self.assertIn(
                RESOURCE_PREFIX + path,
                {
                    str(resource.uri)
                    for resource in await client.list_resources()
                },
            )
            self.assertIn(
                "agent-skeptic",
                {prompt.name for prompt in await client.list_prompts()},
            )
            result = await client.call_tool("read_file", {"path": path})
            self.assertEqual(result.content[0].text, expected)
            prompt = await client.get_prompt("agent-skeptic")
            self.assertEqual(prompt.messages[0].content.text, expected)
