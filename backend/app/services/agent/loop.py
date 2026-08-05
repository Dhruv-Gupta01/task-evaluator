import json
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from app.services.agent.prompt import build_initial_user_message, build_system_message
from app.services.agent.tools_spec import tool_specs
from app.services.llm.base import LLMClient, Message, ToolCall


@dataclass
class LoopResult:
    finished: bool
    reason: str
    iterations: int
    transcript: list[dict] = field(default_factory=list)


class AgentLoop:
    def __init__(
        self,
        llm_client: LLMClient,
        workdir: Path,
        instruction_text: str,
        max_iters: int,
        timeout_sec: int,
    ):
        self.llm_client = llm_client
        self.workdir = workdir.resolve()
        self.max_iters = max_iters
        self.timeout_sec = timeout_sec
        self.messages: list[Message] = [
            build_system_message(instruction_text),
            build_initial_user_message(),
        ]
        self.transcript: list[dict] = []

    def _resolve_path(self, rel_path: str) -> Path:
        p = (self.workdir / rel_path).resolve()
        if not str(p).startswith(str(self.workdir)):
            raise ValueError(f"path escapes working directory: {rel_path}")
        return p

    @staticmethod
    def _looks_like_implicit_finish(text: str) -> bool:
        stripped = (text or "").strip()
        if not stripped.startswith("{") or not stripped.endswith("}"):
            return False
        try:
            parsed = json.loads(stripped)
        except (json.JSONDecodeError, ValueError):
            return False
        return isinstance(parsed, dict)

    def execute_tool(self, tool_call: ToolCall) -> str:
        try:
            if tool_call.name == "read_file":
                path = self._resolve_path(tool_call.arguments["path"])
                if not path.is_file():
                    return f"ERROR: file not found: {tool_call.arguments['path']}"
                return path.read_text(errors="replace")[:20000]

            if tool_call.name == "write_file":
                path = self._resolve_path(tool_call.arguments["path"])
                path.parent.mkdir(parents=True, exist_ok=True)
                content = tool_call.arguments["content"]
                path.write_text(content)
                return f"OK: wrote {len(content)} bytes to {tool_call.arguments['path']}"

            if tool_call.name == "run_shell":
                result = subprocess.run(
                    tool_call.arguments["command"],
                    shell=True,
                    cwd=self.workdir,
                    capture_output=True,
                    text=True,
                    timeout=60,
                )
                return (
                    f"exit_code={result.returncode}\n"
                    f"stdout:\n{result.stdout[:10000]}\n"
                    f"stderr:\n{result.stderr[:10000]}"
                )

            if tool_call.name == "finish":
                return "OK"

            return f"ERROR: unknown tool {tool_call.name}"
        except subprocess.TimeoutExpired:
            return "ERROR: command timed out (60s limit)"
        except Exception as e:
            return f"ERROR: {e}"

    async def run(self) -> LoopResult:
        start = time.monotonic()
        specs = tool_specs()

        for i in range(1, self.max_iters + 1):
            if time.monotonic() - start > self.timeout_sec:
                return LoopResult(
                    finished=False, reason="timeout", iterations=i - 1, transcript=self.transcript
                )

            response = await self.llm_client.complete(self.messages, specs)
            self.transcript.append(
                {
                    "iteration": i,
                    "text": response.text,
                    "tool_call": response.tool_call.model_dump() if response.tool_call else None,
                }
            )

            if response.tool_call is None:
                # Some models signal completion as a bare JSON text blob
                # (e.g. {"status": "completed"}) instead of calling the
                # `finish` tool. Real exploratory/reasoning text is never
                # valid standalone JSON, so treat a parseable JSON object
                # here as an implicit finish rather than forcing a wasted
                # extra round-trip to get a "real" tool call.
                if self._looks_like_implicit_finish(response.text):
                    return LoopResult(
                        finished=True,
                        reason="implicit_finish_json",
                        iterations=i,
                        transcript=self.transcript,
                    )

                self.messages.append(Message(role="assistant", content=response.text or ""))
                self.messages.append(
                    Message(
                        role="user",
                        content=(
                            "Please call one of the available tools: read_file, "
                            "write_file, run_shell, or finish."
                        ),
                    )
                )
                continue

            if response.tool_call.name == "finish":
                return LoopResult(
                    finished=True, reason="finish", iterations=i, transcript=self.transcript
                )

            result = self.execute_tool(response.tool_call)
            # Plain natural-language framing, not a bracket-string pattern —
            # models were observed pattern-matching on a bracketed
            # "[tool_call: name(args)]" marker and echoing it back as plain
            # text instead of emitting a real tool call on later turns.
            self.messages.append(
                Message(
                    role="assistant",
                    content=(
                        f"I called {response.tool_call.name} with arguments "
                        f"{response.tool_call.arguments}."
                    ),
                )
            )
            self.messages.append(
                Message(
                    role="user",
                    content=f"Here is the result of that {response.tool_call.name} call:\n{result}",
                )
            )

        return LoopResult(
            finished=False, reason="max_iters", iterations=self.max_iters, transcript=self.transcript
        )
