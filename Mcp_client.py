import asyncio
import json
import os
import shutil
import sys
from contextlib import AsyncExitStack
from pathlib import Path

from dotenv import load_dotenv
from openai import APIError, AsyncOpenAI, RateLimitError
from mcp import Client, StdioServerParameters
from mcp_types import TextContent

load_dotenv()  # load environment variables from .env

# OpenCode's OpenAI-compatible endpoint. Use a model served over chat completions.
OPENCODE_BASE_URL = os.getenv("OPENCODE_BASE_URL", "https://opencode.ai/inference/openai/v1")
OPENCODE_MODEL = os.getenv("OPENCODE_MODEL", "deepseek-v4.1-flash")
MAX_TOOL_TURNS = 10
# Fail a stuck request instead of hanging the chat loop.
REQUEST_TIMEOUT_S = 60.0

# Much of the Mauritian press publishes in French; answers are always in English.
SYSTEM_INSTRUCTION = """\
You answer questions about Mauritian news using the tools provided.
Always reply in English. Many articles are in French: translate their headlines, summaries and quotes into
English, and give the original French headline in parentheses after the translation.
When asked to translate an article, fetch its full text with get_article and give a complete, faithful English
translation, not a summary.
Cite each article you use with its source name, date and link. Use only facts from the tool results; if the
archive has nothing relevant, say so.
The user can also keep their own notes: use create_note when they ask to save, bookmark or remember
something, and list_notes, update_note and delete_note to manage them. Before deleting, make sure which note
the user means; if it is unclear, list the candidates and ask."""


def is_destructive(tool) -> bool:
    """Whether a tool may delete or overwrite data, per its MCP annotations.

    The MCP spec defaults destructive_hint to true for tools that are not
    read-only, and a tool with no annotations makes no promises either way.
    """
    hints = tool.annotations
    if hints is None:
        return True
    return not hints.read_only_hint and hints.destructive_hint is not False


async def confirm(tool_name: str, args: dict) -> bool:
    """Ask the user to approve a destructive tool call. Anything but "y" is a no."""
    prompt = f"\n⚠️  The model wants to run {tool_name} with {args}. Proceed? [y/N] "
    # input() blocks, so keep it off the event loop.
    try:
        answer = await asyncio.to_thread(input, prompt)
    except (EOFError, KeyboardInterrupt):
        return False
    return answer.strip().lower() in ("y", "yes")


class MCPClient:
    def __init__(self):
        # Initialize session and client objects
        self.client: Client | None = None
        self.exit_stack = AsyncExitStack()
        self._llm: AsyncOpenAI | None = None

    @property
    def llm(self) -> AsyncOpenAI:
        """Lazy-initialize the OpenCode client when needed"""
        if self._llm is None:
            self._llm = AsyncOpenAI(
                api_key=os.getenv("OPENCODE_API_KEY"),
                base_url=OPENCODE_BASE_URL,
                timeout=REQUEST_TIMEOUT_S,
                # The SDK backs off and retries on 429 and 5xx responses.
                max_retries=4,
            )
        return self._llm

    async def _generate(self, messages: list[dict], tools: list[dict]):
        """Send one turn to OpenCode"""
        return await self.llm.chat.completions.create(model=OPENCODE_MODEL, messages=messages, tools=tools)

    async def connect_to_server(self, server_script_path: str):
        """Connect to an MCP server

        Args:
            server_script_path: Path to the server script (.py or .js)
        """
        is_python = server_script_path.endswith(".py")
        is_js = server_script_path.endswith(".js")
        if not (is_python or is_js):
            raise ValueError("Server script must be a .py or .js file")

        if is_python:
            path = Path(server_script_path).resolve()
            if shutil.which("uv"):
                command, args = "uv", ["--directory", str(path.parent), "run", path.name]
            else:
                # No uv on PATH: run the server with this interpreter instead.
                command, args = sys.executable, [str(path)]
            server_params = StdioServerParameters(command=command, args=args, env=None)
        else:
            server_params = StdioServerParameters(command="node", args=[server_script_path], env=None)

        # Client launches the command itself when given StdioServerParameters.
        # "auto" probes server/discover, falling back to the 2025-11-25 handshake.
        self.client = await self.exit_stack.enter_async_context(Client(server_params, mode="auto"))

        # List available tools
        response = await self.client.list_tools()
        tools = response.tools
        print(f"\nConnected over protocol {self.client.protocol_version} with tools:", [tool.name for tool in tools])

    async def process_query(self, query: str) -> str:
        """Process a query using OpenCode and available tools"""
        messages: list[dict] = [
            {"role": "system", "content": SYSTEM_INSTRUCTION},
            {"role": "user", "content": query},
        ]

        # MCP input schemas are JSON Schema, which OpenAI-style function tools accept as-is.
        tools_response = await self.client.list_tools()
        tools = [
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description or "",
                    "parameters": tool.input_schema,
                },
            }
            for tool in tools_response.tools
        ]

        # Destructive tools only run after the user says yes.
        needs_confirmation = {tool.name for tool in tools_response.tools if is_destructive(tool)}

        final_text = []

        response = await self._generate(messages, tools)

        for _ in range(MAX_TOOL_TURNS):
            choice = response.choices[0] if response.choices else None
            if choice is None:
                final_text.append("[OpenCode returned no answer: empty response]")
                return "\n".join(final_text)

            message = choice.message
            if message.content:
                final_text.append(message.content)

            if not message.tool_calls:
                if choice.finish_reason not in (None, "stop"):
                    final_text.append(f"[OpenCode stopped: {choice.finish_reason}]")
                return "\n".join(final_text)

            # Send the assistant turn back unchanged (including any reasoning
            # fields the provider added) so the model keeps its context.
            messages.append(message.model_dump(exclude_none=True))

            for call in message.tool_calls:
                name = call.function.name
                try:
                    args = json.loads(call.function.arguments or "{}")
                except json.JSONDecodeError:
                    messages.append(
                        {"role": "tool", "tool_call_id": call.id, "content": "Error: arguments were not valid JSON."}
                    )
                    continue

                if name in needs_confirmation and not await confirm(name, args):
                    final_text.append(f"[Skipped {name}: you declined]")
                    messages.append(
                        {"role": "tool", "tool_call_id": call.id, "content": "Error: The user declined this action."}
                    )
                    continue

                # call_tool validates the result against the declared schema.
                result = await self.client.call_tool(name, args)
                final_text.append(f"[Calling tool {name} with args {args}]")

                # structured_content is data the application can use directly.
                if isinstance(result.structured_content, list):
                    final_text.append(f"[{name} returned {len(result.structured_content)} items]")

                # content is a list of block types; forward only the text ones.
                text = "\n".join(block.text for block in result.content if isinstance(block, TextContent))
                messages.append(
                    {"role": "tool", "tool_call_id": call.id, "content": f"Error: {text}" if result.is_error else text}
                )

            response = await self._generate(messages, tools)

        # The turn cap was hit. Keep the last response's text. If it asked for
        # more tools, say they were not run.
        if response.choices:
            message = response.choices[0].message
            if message.content:
                final_text.append(message.content)
            if message.tool_calls:
                final_text.append(f"[Stopped after {MAX_TOOL_TURNS} tool-use turns]")
        return "\n".join(final_text)

    async def chat_loop(self):
        """Run an interactive chat loop"""
        print("\nMCP Client Started!")
        print("Type your queries or 'quit' to exit.")

        while True:
            # input() blocks, so keep it off the event loop.
            try:
                query = (await asyncio.to_thread(input, "\nQuery: ")).strip()
            except (EOFError, KeyboardInterrupt):
                break

            if query.lower() == "quit":
                break

            try:
                response = await self.process_query(query)
                print("\n" + response)
            except RateLimitError:
                print(
                    f"\nOpenCode rate limit or credit limit reached for {OPENCODE_MODEL}. Check your balance in the "
                    "OpenCode Console, set OPENCODE_MODEL in .env to another model, or try again later."
                )
            except APIError as e:
                print(f"\nOpenCode API error: {e}")
            except Exception as e:
                print(f"\nError: {str(e)}")

    async def cleanup(self):
        """Clean up resources"""
        await self.exit_stack.aclose()


async def main():
    if len(sys.argv) < 2:
        print("Usage: uv run Mcp_client.py <path_to_server_script>   (e.g. news_server.py)")
        sys.exit(1)

    client = MCPClient()
    try:
        await client.connect_to_server(sys.argv[1])

        # Check if we have a valid API key to continue
        api_key = os.getenv("OPENCODE_API_KEY")
        if not api_key:
            print("\nNo OPENCODE_API_KEY found. To query these tools with OpenCode, set your API key in .env:")
            print("  OPENCODE_API_KEY=your-api-key-here")
            print("Get a key from the OpenCode Console: https://opencode.ai/zen")
            return

        await client.chat_loop()
    finally:
        await client.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
