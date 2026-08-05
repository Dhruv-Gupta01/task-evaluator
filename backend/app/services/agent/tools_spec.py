from app.services.llm.base import ToolSpec

_RAW_TOOL_SPECS = [
    {
        "name": "read_file",
        "description": "Read the contents of a file, relative to the working directory.",
        "parameters": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    },
    {
        "name": "write_file",
        "description": (
            "Write (overwrite) the contents of a file, relative to the working "
            "directory. Creates parent directories if needed."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "content": {"type": "string"},
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": "run_shell",
        "description": (
            "Run a shell command in the working directory and return its "
            "stdout, stderr, and exit code."
        ),
        "parameters": {
            "type": "object",
            "properties": {"command": {"type": "string"}},
            "required": ["command"],
        },
    },
    {
        "name": "finish",
        "description": "Call this when you believe the task is complete.",
        "parameters": {"type": "object", "properties": {}},
    },
]


def tool_specs() -> list[ToolSpec]:
    return [ToolSpec(**spec) for spec in _RAW_TOOL_SPECS]
