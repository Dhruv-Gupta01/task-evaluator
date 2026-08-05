ARG BASE_IMAGE
FROM ${BASE_IMAGE}

COPY agent_runtime/ /opt/agent_runtime/

# The task's base image may be Debian/Ubuntu, Alpine, or RHEL-based (or lack
# Python entirely) — try each package manager in turn rather than assuming one.
RUN (command -v apt-get >/dev/null 2>&1 && \
       apt-get update && \
       apt-get install -y --no-install-recommends python3 python3-pip && \
       rm -rf /var/lib/apt/lists/*) || \
    (command -v apk >/dev/null 2>&1 && \
       apk add --no-cache python3 py3-pip) || \
    (command -v yum >/dev/null 2>&1 && \
       yum install -y python3 python3-pip) || \
    (echo "ERROR: no supported package manager found to install python3" >&2 && exit 1)

# Newer pip on Debian-based images refuses global installs (PEP 668) unless
# --break-system-packages is passed; older pip doesn't know that flag at all.
RUN python3 -m pip install --no-cache-dir --break-system-packages \
        anthropic openai google-genai pydantic 2>/dev/null || \
    python3 -m pip install --no-cache-dir anthropic openai google-genai pydantic

ENV PYTHONPATH=/opt/agent_runtime

ENTRYPOINT ["python3", "/opt/agent_runtime/entrypoint.py"]
