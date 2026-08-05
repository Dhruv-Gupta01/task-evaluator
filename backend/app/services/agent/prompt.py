from app.services.llm.base import Message

SYSTEM_PREAMBLE = (
    "You are an autonomous coding agent working in a Linux container. "
    "You have four tools: read_file, write_file, run_shell, and finish. "
    "Use run_shell to explore the filesystem and run commands. "
    "Use read_file/write_file to inspect and modify files. "
    "All paths are relative to your current working directory unless absolute. "
    "When you believe you have completed the task, call finish. "
    "Work efficiently — you have a limited number of turns.\n\n"
    "TASK INSTRUCTIONS:\n"
)


def build_system_message(instruction_text: str) -> Message:
    return Message(role="system", content=SYSTEM_PREAMBLE + instruction_text)


def build_initial_user_message() -> Message:
    return Message(
        role="user",
        content="Begin. Explore the working directory first if you need to understand the current state.",
    )
