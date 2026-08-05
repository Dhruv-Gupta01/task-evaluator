#!/usr/bin/env python3
import asyncio
import json
import os
import sys
import traceback
from pathlib import Path

sys.path.insert(0, "/opt/agent_runtime")

from app.services.agent.agent_factory import get_llm_client_from_env  # noqa: E402
from app.services.agent.loop import AgentLoop  # noqa: E402


def main() -> None:
    workdir = Path(os.environ.get("AGENT_WORKDIR", "/workdir"))
    instruction_path = Path(os.environ.get("INSTRUCTION_PATH", "/task/instruction.md"))
    max_iters = int(os.environ.get("LLM_MAX_ITERS", "30"))
    timeout_sec = int(os.environ.get("AGENT_TIMEOUT_SEC", "600"))

    instruction_text = (
        instruction_path.read_text() if instruction_path.is_file() else "(no instructions found)"
    )

    result_path = workdir / ".agent_result.json"

    try:
        client = get_llm_client_from_env()
        loop = AgentLoop(
            llm_client=client,
            workdir=workdir,
            instruction_text=instruction_text,
            max_iters=max_iters,
            timeout_sec=timeout_sec,
        )
        result = asyncio.run(loop.run())
        result_path.write_text(
            json.dumps(
                {
                    "finished": result.finished,
                    "reason": result.reason,
                    "iterations": result.iterations,
                    "transcript": result.transcript,
                }
            )
        )
    except Exception:
        result_path.write_text(
            json.dumps({"finished": False, "reason": "error", "error": traceback.format_exc()})
        )
        sys.exit(1)


if __name__ == "__main__":
    main()
