import requests
from bs4 import BeautifulSoup

from aider.tools.utils.base_tool import BaseTool


class Tool(BaseTool):
    """
    A tool to fetch the latest headlines from yahoo.com.
    """

    NORM_NAME = "getyahooheadlines"
    SCHEMA = {
        "type": "function",
        "function": {
            "name": "GetYahooHeadlines",
            "description": "Fetches the latest headlines from yahoo.com.",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    }

    @classmethod
    async def execute(cls, coder, **params):
        """
        Fetches the latest headlines from yahoo.com.
        """
        try:
            url = "https://www.yahoo.com"
            response = requests.get(url)
            response.raise_for_status()  # Raise an exception for bad status codes

            soup = BeautifulSoup(response.text, "html.parser")

            headlines = []
            # Yahoo headlines are often in h3 tags with a specific class or structure
            for headline in soup.find_all("h3"):
                if headline.a:
                    headlines.append(headline.a.get_text(strip=True))

            if not headlines:
                return "Could not find any headlines on yahoo.com."

            return "\n".join(headlines)

        except requests.exceptions.RequestException as e:
            return f"Error fetching yahoo.com: {e}"
        except Exception as e:
            return f"An unexpected error occurred: {e}"
