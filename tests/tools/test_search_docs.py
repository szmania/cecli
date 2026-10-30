"""Tests for the creator-scoped SearchDocs tool."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from cecli.helpers.conversation import ConversationService
from cecli.tools import search_docs


def _result(response):
    return response.to_dict()


def _coder(creator=True):
    return SimpleNamespace(agent_config={"creator_mode": creator})


@pytest.mark.asyncio
async def test_search_docs_returns_retrieved_chunks(monkeypatch):
    chunks = SimpleNamespace(
        retrieve_creator_docs=Mock(
            return_value=[
                {
                    "id": "config/hooks.md#0",
                    "url": "https://cecli.dev/docs/config/hooks/",
                    "document": "Hook documentation body.",
                }
            ]
        )
    )
    monkeypatch.setattr(ConversationService, "get_chunks", staticmethod(lambda coder: chunks))
    coder = _coder()

    response = await search_docs.Tool.execute(coder, query="  hook config  ", max_results=3)

    chunks.retrieve_creator_docs.assert_called_once_with(coder, "hook config", n_results=3)
    result = _result(response)["result"]
    assert len(result) == 1
    assert "https://cecli.dev/docs/config/hooks/" in result[0]["content"]
    assert "Hook documentation body." in result[0]["content"]
    assert result[0]["_"]["id"] == "config/hooks.md#0"


@pytest.mark.asyncio
async def test_search_docs_reports_no_results(monkeypatch):
    chunks = SimpleNamespace(retrieve_creator_docs=Mock(return_value=[]))
    monkeypatch.setattr(ConversationService, "get_chunks", staticmethod(lambda coder: chunks))

    response = await search_docs.Tool.execute(_coder(), query="hooks")

    result = _result(response)["result"]
    assert "No documentation matched" in result[0]["content"]


@pytest.mark.asyncio
async def test_search_docs_rejects_empty_query(monkeypatch):
    chunks = SimpleNamespace(retrieve_creator_docs=Mock())
    monkeypatch.setattr(ConversationService, "get_chunks", staticmethod(lambda coder: chunks))

    response = await search_docs.Tool.execute(_coder(), query="   ")

    data = _result(response)
    assert data["result"] == []
    assert "'query' must be a non-empty string." in data["errors"]
    chunks.retrieve_creator_docs.assert_not_called()


@pytest.mark.asyncio
async def test_search_docs_is_scoped_to_creator(monkeypatch):
    monkeypatch.setattr(search_docs, "is_creator_coder", lambda coder: False)

    response = await search_docs.Tool.execute(_coder(creator=False), query="hooks")

    data = _result(response)
    assert data["result"] == []
    assert "scoped to the creator sub-agent" in data["errors"][0]


@pytest.mark.asyncio
async def test_search_docs_clamps_max_results(monkeypatch):
    chunks = SimpleNamespace(retrieve_creator_docs=Mock(return_value=[]))
    monkeypatch.setattr(ConversationService, "get_chunks", staticmethod(lambda coder: chunks))

    await search_docs.Tool.execute(_coder(), query="hooks", max_results=500)

    assert chunks.retrieve_creator_docs.call_args.kwargs["n_results"] == 20


@pytest.mark.asyncio
async def test_search_docs_reports_retrieval_failure(monkeypatch):
    chunks = SimpleNamespace(
        retrieve_creator_docs=Mock(side_effect=RuntimeError("index unavailable"))
    )
    monkeypatch.setattr(ConversationService, "get_chunks", staticmethod(lambda coder: chunks))

    response = await search_docs.Tool.execute(_coder(), query="hooks")

    data = _result(response)
    assert data["result"] == []
    assert "SearchDocs failed: index unavailable" in data["errors"][0]
