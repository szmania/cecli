from ..sendchat import ensure_alternating_roles


def add_reasoning_content(messages):
    """Add empty reasoning content field to assistant messages if not present.

    Args:
        messages: List of message dictionaries

    Returns:
        List of messages with reasoning content added to assistant messages
    """
    for msg in messages:
        if msg.get("role") == "assistant" and "reasoning_content" not in msg:
            msg["reasoning_content"] = ""
    return messages


def remove_empty_tool_calls(messages):
    """Remove messages with tool_calls that are empty arrays.

    Args:
        messages: List of message dictionaries

    Returns:
        List of messages with empty tool_calls messages removed
    """
    return [
        msg
        for msg in messages
        if not (msg.get("role") == "assistant" and "tool_calls" in msg and msg["tool_calls"] == [])
    ]


def _process_thought_signature(container):
    if "provider_specific_fields" not in container:
        container["provider_specific_fields"] = {}

    if not container["provider_specific_fields"]:
        container["provider_specific_fields"] = {}

    psf = container["provider_specific_fields"]

    if "thought_signature" not in psf:
        if "thought_signatures" in psf:
            sigs = psf["thought_signatures"]
            if isinstance(sigs, list) and len(sigs) > 0:
                psf["thought_signature"] = sigs[0]
            elif isinstance(sigs, str):
                psf["thought_signature"] = sigs
            psf.pop("thought_signatures", None)

    if "thought_signature" not in psf:
        psf["thought_signature"] = "skip_thought_signature_validator"


def thought_signature(model, messages):
    # Add thought signatures for Vertex AI and Gemini models
    if model.name.startswith("vertex_ai/") or model.name.startswith("gemini/"):
        for msg in messages:
            # Handle top-level provider_specific_fields
            if "provider_specific_fields" in msg or msg.get("role") == "assistant":
                _process_thought_signature(msg)

            if "tool_calls" in msg:
                tool_calls = msg["tool_calls"]
                if tool_calls:
                    for call in tool_calls:
                        if call:
                            _process_thought_signature(call)

            if "function_call" in msg:
                call = msg["function_call"]
                if call:
                    _process_thought_signature(call)

    return messages


def concatenate_user_messages(messages):
    """Concatenate user messages at the end of the array separated by assistant "(empty response)" messages.

    This function works backwards from the end of the messages array, collecting
    user messages until it encounters an assistant message that is not "(empty response)",
    a tool message, or a system message. All collected user messages are concatenated
    into a single user message at the end, and the original user messages are removed.

    Args:
        messages: List of message dictionaries

    Returns:
        List of messages with concatenated user messages
    """
    if not messages:
        return messages

    user_messages_to_concat = []
    i = len(messages) - 1

    while i >= 0:
        msg = messages[i]
        role = msg.get("role")
        content = msg.get("content", "")

        if isinstance(content, list):
            break

        if role == "user":
            user_messages_to_concat.insert(0, content)  # Insert at beginning to maintain order
            i -= 1
            continue

        # If it's an assistant message with "(empty response)", skip it and continue backwards
        if role == "assistant" and content == "(empty response)":
            i -= 1
            continue

        # If we hit any other type of message (non-empty assistant, tool, system, etc.), stop
        break

        # If we collected any user messages to concatenate
    if user_messages_to_concat:
        # Remove the original user messages (and any skipped empty assistant messages)
        # by keeping only messages up to index i (inclusive)
        result = messages[: i + 1] if i >= 0 else []

        # Helper to extract text from strings or structured content lists
        def get_text(c):
            if isinstance(c, str):
                return c
            if isinstance(c, list) and len(c) > 0:
                # Extracts 'text' from the first block if it's a dict
                return c[0].get("text", "") if isinstance(c[0], dict) else str(c[0])
            return str(c)

        concatenated_content = "\n".join(get_text(c) for c in user_messages_to_concat)
        result.append({"role": "user", "content": concatenated_content})

        return result

    return messages


def model_request_parser(model, messages):
    messages = thought_signature(model, messages)
    messages = remove_empty_tool_calls(messages)
    messages = ensure_alternating_roles(messages)
    messages = add_reasoning_content(messages)
    messages = concatenate_user_messages(messages)
    return messages
