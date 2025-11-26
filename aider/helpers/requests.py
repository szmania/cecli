import json

from ..sendchat import ensure_alternating_roles


def model_request_parser(model, messages):
    messages = ensure_alternating_roles(messages)

    return messages
