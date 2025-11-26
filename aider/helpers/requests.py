import hashlib
import json

from ..sendchat import ensure_alternating_roles


def thought_signature(thought):
    """
    Hash the thought to create a unique signature.
    """
    thought_json = json.dumps(thought, sort_keys=True)
    return hashlib.sha256(thought_json.encode()).hexdigest()


def model_request_parser(model, messages):
    messages = ensure_alternating_roles(messages)

    if not model.is_gemini_2_5_pro():
        for message in messages:
            if message.get("role") == "assistant" and "tool_calls" in message:
                tool_calls = message.get("tool_calls")
                if not tool_calls:
                    continue
                for tool_call in tool_calls:
                    if (
                        tool_call.get("type") == "function"
                        and tool_call.get("function", {}).get("name") == "sequentialthinking"
                    ):
                        try:
                            arguments = json.loads(tool_call["function"]["arguments"])
                            signature = thought_signature(arguments)
                            arguments["signature"] = signature
                            tool_call["function"]["arguments"] = json.dumps(arguments)
                        except (json.JSONDecodeError, KeyError):
                            continue

    return messages
