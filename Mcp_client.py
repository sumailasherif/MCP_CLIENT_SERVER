import asyncio
import os
import shutil
import sys
from contextlib import AsyncExitStack
from pathlib import Path

from dotenv import load_dotenv
from google import genai
from google.genai import errors, types
from mcp import Client, StdioServerParameters
from mcp_types import TextContent

load_dotenv()  # load environment variables from .env

GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.6-flash")
MAX_TOOL_TURNS = 10
# Fail a stuck request instead of hanging the chat loop.
REQUEST_TIMEOUT_MS = 60_000

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
    prompt = f"\n⚠️  Gemini wants to run {tool_name} with {args}. Proceed? [y/N] "
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
        self._gemini: genai.Client | None = None

    @property
    def gemini(self) -> genai.Client:
        """Lazy-initialize Gemini client when needed"""
        if self._gemini is None:
            self._gemini = genai.Client(
                api_key=os.getenv("GEMINI_API_KEY"),
                http_options=types.HttpOptions(
                    timeout=REQUEST_TIMEOUT_MS,
                    # Free-tier keys often get 429/503 under load; back off and retry.
                    retry_options=types.HttpRetryOptions(attempts=4, http_status_codes=[429, 500, 503, 504]),
                ),
            )
        return self._gemini

    async def _generate(self, contents: list[types.Content], config: types.GenerateContentConfig):
        """Send one turn to Gemini"""
        return await self.gemini.aio.models.generate_content(model=GEMINI_MODEL, contents=contents, config=config)

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
        """Process a query using Gemini and available tools"""
        contents = [types.Content(role="user", parts=[types.Part(text=query)])]

        # MCP input schemas are JSON Schema, which Gemini accepts as-is.
        tools_response = await self.client.list_tools()
        config = types.GenerateContentConfig(
            tools=[
                types.Tool(
                    function_declarations=[
                        types.FunctionDeclaration(
                            name=tool.name, description=tool.description, parameters_json_schema=tool.input_schema
                        )
                        for tool in tools_response.tools
                    ]
                )
            ],
            # This loop calls the MCP tools itself, so turn off the SDK's own
            # automatic function calling.
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
            system_instruction=SYSTEM_INSTRUCTION,
        )

        # Destructive tools only run after the user says yes.
        needs_confirmation = {tool.name for tool in tools_response.tools if is_destructive(tool)}

        final_text = []

        response = await self._generate(contents, config)

        for _ in range(MAX_TOOL_TURNS):
            # No candidate means the prompt itself was blocked.
            candidate = response.candidates[0] if response.candidates else None
            if candidate is None or candidate.content is None:
                final_text.append(f"[Gemini returned no answer: {response.prompt_feedback or 'empty response'}]")
                return "\n".join(final_text)

            model_turn = candidate.content
            function_calls = []
            for part in model_turn.parts or []:
                # Skip thought summaries; keep only the answer text.
                if part.text and not part.thought:
                    final_text.append(part.text)
                elif part.function_call:
                    function_calls.append(part.function_call)

            if not function_calls:
                if candidate.finish_reason not in (None, types.FinishReason.STOP):
                    final_text.append(f"[Gemini stopped: {candidate.finish_reason.name}]")
                return "\n".join(final_text)

            function_responses = []
            for call in function_calls:
                args = call.args or {}
                if call.name in needs_confirmation and not await confirm(call.name, args):
                    final_text.append(f"[Skipped {call.name}: you declined]")
                    function_responses.append(
                        types.Part(
                            function_response=types.FunctionResponse(
                                id=call.id, name=call.name, response={"error": "The user declined this action."}
                            )
                        )
                    )
                    continue

                # call_tool validates the result against the declared schema.
                result = await self.client.call_tool(call.name, args)
                final_text.append(f"[Calling tool {call.name} with args {args}]")

                # structured_content is data the application can use directly.
                if isinstance(result.structured_content, list):
                    final_text.append(f"[{call.name} returned {len(result.structured_content)} items]")

                # content is a list of block types; forward only the text ones.
                text = "\n".join(block.text for block in result.content if isinstance(block, TextContent))
                function_responses.append(
                    types.Part(
                        function_response=types.FunctionResponse(
                            id=call.id,
                            name=call.name,
                            response={"error": text} if result.is_error else {"result": text},
                        )
                    )
                )

            # Send the model's turn back unchanged so its thought signatures are kept.
            contents.append(model_turn)
            contents.append(types.Content(role="user", parts=function_responses))

            response = await self._generate(contents, config)

        # The turn cap was hit. Keep the last response's text. If it asked for
        # more tools, say they were not run.
        if response.candidates and response.candidates[0].content:
            parts = response.candidates[0].content.parts or []
            final_text.extend(part.text for part in parts if part.text and not part.thought)
            if any(part.function_call for part in parts):
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
            except errors.ClientError as e:
                if e.code == 429:
                    print(
                        f"\nGemini quota reached for {GEMINI_MODEL}. Free-tier quotas are per model, so you can set "
                        "GEMINI_MODEL in .env to another model, or wait for the quota to reset."
                    )
                else:
                    print(f"\nError: {str(e)}")
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
        api_key = os.getenv("GEMINI_API_KEY")
        if not api_key:
            print("\nNo GEMINI_API_KEY found. To query these tools with Gemini, set your API key in .env:")
            print("  GEMINI_API_KEY=your-api-key-here")
            print("Get a free key at https://aistudio.google.com/apikey")
            return

        await client.chat_loop()
    finally:
        await client.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
