from abc import ABC, abstractmethod


class BaseAiderTool(ABC):
    """
    An abstract base class for creating aider tools.
    """

    def __init__(self, coder, **kwargs):
        """
        Initializes the tool with a coder instance.

        :param coder: The coder instance that the tool can interact with.
        :param kwargs: Additional keyword arguments.
        """
        self.coder = coder

    @abstractmethod
    def get_tool_definition(self):
        """
        Returns the JSON schema definition for the tool.

        This definition is used by the LLM to understand how to use the tool.
        It should be a dictionary with 'name', 'description', and 'parameters'.

        :return: A dictionary representing the tool's JSON schema.
        """
        pass

    @abstractmethod
    def run(self, **kwargs):
        """
        The main execution method of the tool.

        This method is called when the LLM decides to use the tool.
        The keyword arguments passed to this method are determined by the
        'parameters' in the tool's JSON schema.

        :param kwargs: The arguments for the tool, as provided by the LLM.
        :return: The output of the tool, to be sent back to the LLM.
        """
        pass
