"""SearchDocs tool - on-demand cecli documentation search for the creator agent."""

from cecli.tools.utils.base_tool import BaseTool
from cecli.tools.utils.helpers import is_creator_coder
from cecli.tools.utils.responses import ToolResponse


class Tool(BaseTool):
    NORM_NAME = "searchdocs"
    RESULT_TYPE = "list"
    TRACK_INVOCATIONS = False
    SCHEMA = {
        "type": "function",
        "function": {
            "name": "SearchDocs",
            "description": (
                "Search cecli's bundled documentation on demand. Returns relevant "
                "documentation chunks. Scoped to the creator sub-agent."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": (
                            "What to look up, e.g. 'custom command format', "
                            "'hook payload schema', or 'MCP server config'."
                        ),
                    },
                    "max_results": {
                        "type": "integer",
                        "default": 5,
                        "minimum": 1,
                        "maximum": 20,
                        "description": "Maximum number of documentation chunks to return.",
                    },
                },
                "required": ["query"],
            },
        },
    }

    @classmethod
    async def execute(cls, coder, query=None, max_results=5, **kwargs):
        """Return documentation chunks matching *query*.

        This is the creator's only documentation path: retrieval is explicit
        rather than injected automatically into the conversation.
        """
        from cecli.helpers.conversation import ConversationService

        response = ToolResponse(cls.NORM_NAME, result_type=cls.RESULT_TYPE)

        if not is_creator_coder(coder):
            response.append_error("SearchDocs is scoped to the creator sub-agent.")
            return response

        query = str(query or "").strip()
        if not query:
            response.append_error("'query' must be a non-empty string.")
            return response

        try:
            limit = max(1, min(int(max_results or 5), 20))
        except (TypeError, ValueError):
            limit = 5

        try:
            chunks = ConversationService.get_chunks(coder)
            docs = chunks.retrieve_creator_docs(coder, query, n_results=limit)
        except Exception as exc:
            response.append_error(f"SearchDocs failed: {exc}")
            return response

        if not docs:
            response.append_result("No documentation matched this query.")
            return response

        for doc in docs:
            url = doc.get("url", "")
            document = doc.get("document", "")
            url_attr = f' from_url="{url}"' if url else ""

            response.append_result(
                f"<doc{url_attr}>\n{document}\n</doc>",
                metadata={"id": doc.get("id", ""), "url": url},
            )

        return response
