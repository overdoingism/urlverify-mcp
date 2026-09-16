You cannot call tools natively. Instead, reply with ONLY a JSON object choosing your next action:
{"action": "<tool name>", "args": {...}}
Available tools:
{tool_list}
When done, use {"action": "submit_verdict", "args": {...submission...}}.
