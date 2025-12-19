import unittest
from unittest.mock import ANY, MagicMock, patch

from aider.models import (
    ANTHROPIC_BETA_HEADER,
    Model,
    ModelInfoManager,
    register_models,
    sanity_check_model,
    sanity_check_models,
)


class TestModels(unittest.TestCase):
    def setUp(self):
        """Reset MODEL_SETTINGS before each test"""
        from aider.models import MODEL_SETTINGS

        self._original_settings = MODEL_SETTINGS.copy()

    def tearDown(self):
        """Restore original MODEL_SETTINGS after each test"""
        from aider.models import MODEL_SETTINGS

        MODEL_SETTINGS.clear()
        MODEL_SETTINGS.extend(self._original_settings)

    def test_get_model_info_nonexistent(self):
        manager = ModelInfoManager()
        info = manager.get_model_info("non-existent-model")
        self.assertEqual(info, {})

    def test_max_context_tokens(self):
        model = Model("gpt-3.5-turbo")
        self.assertEqual(model.info["max_input_tokens"], 16385)

        model = Model("gpt-3.5-turbo-16k")
        self.assertEqual(model.info["max_input_tokens"], 16385)

        model = Model("gpt-3.5-turbo-1106")
        self.assertEqual(model.info["max_input_tokens"], 16385)

        model = Model("gpt-4")
        self.assertEqual(model.info["max_input_tokens"], 8 * 1024)

        model = Model("gpt-4-32k")
        self.assertEqual(model.info["max_input_tokens"], 32 * 1024)

        model = Model("gpt-4-0613")
        self.assertEqual(model.info["max_input_tokens"], 8 * 1024)

    @patch("os.environ")
    async def test_sanity_check_model_all_set(self, mock_environ):
        mock_environ.get.return_value = "dummy_value"
        mock_io = MagicMock()
        model = MagicMock()
        model.name = "test-model"
        model.missing_keys = ["API_KEY1", "API_KEY2"]
        model.keys_in_environment = True
        model.info = {"some": "info"}

        await sanity_check_model(mock_io, model)

        mock_io.tool_output.assert_called()
        calls = mock_io.tool_output.call_args_list
        self.assertIn("- API_KEY1: Set", str(calls))
        self.assertIn("- API_KEY2: Set", str(calls))

    @patch("os.environ")
    async def test_sanity_check_model_not_set(self, mock_environ):
        mock_environ.get.return_value = ""
        mock_io = MagicMock()
        model = MagicMock()
        model.name = "test-model"
        model.missing_keys = ["API_KEY1", "API_KEY2"]
        model.keys_in_environment = True
        model.info = {"some": "info"}

        await sanity_check_model(mock_io, model)

        mock_io.tool_output.assert_called()
        calls = mock_io.tool_output.call_args_list
        self.assertIn("- API_KEY1: Not set", str(calls))
        self.assertIn("- API_KEY2: Not set", str(calls))

    async def test_sanity_check_models_bogus_editor(self):
        mock_io = MagicMock()
        main_model = Model("gpt-4")
        main_model.editor_model = Model("bogus-model")

        result = await sanity_check_models(mock_io, main_model)

        self.assertTrue(
            result
        )  # Should return True because there's a problem with the editor model
        mock_io.tool_warning.assert_called_with(ANY)  # Ensure a warning was issued

        warning_messages = [
            warning_call.args[0] for warning_call in mock_io.tool_warning.call_args_list
        ]
        print("Warning messages:", warning_messages)  # Add this line

        self.assertGreaterEqual(mock_io.tool_warning.call_count, 1)  # Expect two warnings
        self.assertTrue(
            any("bogus-model" in msg for msg in warning_messages)
        )  # Check that one of the warnings mentions the bogus model

    @patch("aider.models.check_for_dependencies")
    async def test_sanity_check_model_calls_check_dependencies(self, mock_check_deps):
        """Test that sanity_check_model calls check_for_dependencies"""
        mock_io = MagicMock()
        model = MagicMock()
        model.name = "test-model"
        model.missing_keys = []
        model.keys_in_environment = True
        model.info = {"some": "info"}

        await sanity_check_model(mock_io, model)

        # Verify check_for_dependencies was called with the model name
        mock_check_deps.assert_called_once_with(mock_io, "test-model")

    def test_model_aliases(self):
        # Test common aliases
        model = Model("4")
        self.assertEqual(model.name, "gpt-4-0613")

        model = Model("4o")
        self.assertEqual(model.name, "gpt-4o")

        model = Model("35turbo")
        self.assertEqual(model.name, "gpt-3.5-turbo")

        model = Model("35-turbo")
        self.assertEqual(model.name, "gpt-3.5-turbo")

        model = Model("3")
        self.assertEqual(model.name, "gpt-3.5-turbo")

        model = Model("sonnet")
        self.assertEqual(model.name, "anthropic/claude-sonnet-4-20250514")

        model = Model("haiku")
        self.assertEqual(model.name, "claude-3-5-haiku-20241022")

        model = Model("opus")
        self.assertEqual(model.name, "claude-opus-4-20250514")

        # Test non-alias passes through unchanged
        model = Model("gpt-4")
        self.assertEqual(model.name, "gpt-4")

    def test_o1_use_temp_false(self):
        # Test GitHub Copilot models
        model = Model("github/o1-mini")
        self.assertEqual(model.name, "github/o1-mini")
        self.assertEqual(model.use_temperature, False)

        model = Model("github/o1-preview")
        self.assertEqual(model.name, "github/o1-preview")
        self.assertEqual(model.use_temperature, False)

    def test_parse_token_value(self):
        # Create a model instance to test the parse_token_value method
        model = Model("gpt-4")

        # Test integer inputs
        self.assertEqual(model.parse_token_value(8096), 8096)
        self.assertEqual(model.parse_token_value(1000), 1000)

        # Test string inputs
        self.assertEqual(model.parse_token_value("8096"), 8096)

        # Test k/K suffix (kilobytes)
        self.assertEqual(model.parse_token_value("8k"), 8 * 1024)
        self.assertEqual(model.parse_token_value("8K"), 8 * 1024)
        self.assertEqual(model.parse_token_value("10.5k"), 10.5 * 1024)
        self.assertEqual(model.parse_token_value("0.5K"), 0.5 * 1024)

        # Test m/M suffix (megabytes)
        self.assertEqual(model.parse_token_value("1m"), 1 * 1024 * 1024)
        self.assertEqual(model.parse_token_value("1M"), 1 * 1024 * 1024)
        self.assertEqual(model.parse_token_value("0.5M"), 0.5 * 1024 * 1024)

        # Test with spaces
        self.assertEqual(model.parse_token_value(" 8k "), 8 * 1024)

        # Test conversion from other types
        self.assertEqual(model.parse_token_value(8.0), 8)

    def test_set_thinking_tokens(self):
        # Test that set_thinking_tokens correctly sets the tokens with different formats
        model = Model("gpt-4")

        # Test with integer
        model.set_thinking_tokens(8096)
        self.assertEqual(model.extra_params["thinking"]["budget_tokens"], 8096)
        self.assertFalse(model.use_temperature)

        # Test with string
        model.set_thinking_tokens("10k")
        self.assertEqual(model.extra_params["thinking"]["budget_tokens"], 10 * 1024)

        # Test with decimal value
        model.set_thinking_tokens("0.5M")
        self.assertEqual(model.extra_params["thinking"]["budget_tokens"], 0.5 * 1024 * 1024)

    @patch("aider.models.check_pip_install_extra")
    async def test_check_for_dependencies_bedrock(self, mock_check_pip):
        """Test that check_for_dependencies calls check_pip_install_extra for Bedrock models"""
        from aider.io import InputOutput

        io = InputOutput()

        # Test with a Bedrock model
        from aider.models import check_for_dependencies

        await check_for_dependencies(io, "bedrock/anthropic.claude-3-sonnet-20240229-v1:0")

        # Verify check_pip_install_extra was called with correct arguments
        mock_check_pip.assert_called_once_with(
            io, "boto3", "AWS Bedrock models require the boto3 package.", ["boto3"]
        )

    @patch("aider.models.check_pip_install_extra")
    async def test_check_for_dependencies_vertex_ai(self, mock_check_pip):
        """Test that check_for_dependencies calls check_pip_install_extra for Vertex AI models"""
        from aider.io import InputOutput

        io = InputOutput()

        # Test with a Vertex AI model
        from aider.models import check_for_dependencies

        await check_for_dependencies(io, "vertex_ai/gemini-1.5-pro")

        # Verify check_pip_install_extra was called with correct arguments
        mock_check_pip.assert_called_once_with(
            io,
            "google.cloud.aiplatform",
            "Google Vertex AI models require the google-cloud-aiplatform package.",
            ["google-cloud-aiplatform"],
        )

    @patch("aider.models.check_pip_install_extra")
    async def test_check_for_dependencies_other_model(self, mock_check_pip):
        """Test that check_for_dependencies doesn't call check_pip_install_extra for other models"""
        from aider.io import InputOutput

        io = InputOutput()

        # Test with a non-Bedrock, non-Vertex AI model
        from aider.models import check_for_dependencies

        await check_for_dependencies(io, "gpt-4")

        # Verify check_pip_install_extra was not called
        mock_check_pip.assert_not_called()

    def test_get_repo_map_tokens(self):
        # Test default case (no max_input_tokens in info)
        model = Model("gpt-4")
        model.info = {}
        self.assertEqual(model.get_repo_map_tokens(), 1024)

        # Test minimum boundary (max_input_tokens < 8192)
        model.info = {"max_input_tokens": 4096}
        self.assertEqual(model.get_repo_map_tokens(), 1024)

        # Test middle range (max_input_tokens = 16384)
        model.info = {"max_input_tokens": 16384}
        self.assertEqual(model.get_repo_map_tokens(), 2048)

        # Test maximum boundary (max_input_tokens > 32768)
        model.info = {"max_input_tokens": 65536}
        self.assertEqual(model.get_repo_map_tokens(), 4096)

        # Test exact boundary values
        model.info = {"max_input_tokens": 8192}
        self.assertEqual(model.get_repo_map_tokens(), 1024)

        model.info = {"max_input_tokens": 32768}
        self.assertEqual(model.get_repo_map_tokens(), 4096)

    def test_configure_model_settings(self):
        # Test o3-mini case
        model = Model("something/o3-mini")
        self.assertEqual(model.edit_format, "diff")
        self.assertTrue(model.use_repo_map)
        self.assertFalse(model.use_temperature)

        # Test o1-mini case
        model = Model("something/o1-mini")
        self.assertTrue(model.use_repo_map)
        self.assertFalse(model.use_temperature)
        self.assertFalse(model.use_system_prompt)

        # Test o1-preview case
        model = Model("something/o1-preview")
        self.assertEqual(model.edit_format, "diff")
        self.assertTrue(model.use_repo_map)
        self.assertFalse(model.use_temperature)
        self.assertFalse(model.use_system_prompt)

        # Test o1 case
        model = Model("something/o1")
        self.assertEqual(model.edit_format, "diff")
        self.assertTrue(model.use_repo_map)
        self.assertFalse(model.use_temperature)
        self.assertFalse(model.streaming)

        # Test deepseek v3 case
        model = Model("deepseek-v3")
        self.assertEqual(model.edit_format, "diff")
        self.assertTrue(model.use_repo_map)
        self.assertEqual(model.reminder, "sys")
        self.assertTrue(model.examples_as_sys_msg)

        # Test deepseek reasoner case
        model = Model("deepseek-r1")
        self.assertEqual(model.edit_format, "diff")
        self.assertTrue(model.use_repo_map)
        self.assertTrue(model.examples_as_sys_msg)
        self.assertFalse(model.use_temperature)
        self.assertEqual(model.reasoning_tag, "think")

        # Test provider/deepseek-r1 case
        model = Model("someprovider/deepseek-r1")
        self.assertEqual(model.edit_format, "diff")
        self.assertTrue(model.use_repo_map)
        self.assertTrue(model.examples_as_sys_msg)
        self.assertFalse(model.use_temperature)
        self.assertEqual(model.reasoning_tag, "think")

        # Test provider/deepseek-v3 case
        model = Model("anotherprovider/deepseek-v3")
        self.assertEqual(model.edit_format, "diff")
        self.assertTrue(model.use_repo_map)
        self.assertEqual(model.reminder, "sys")
        self.assertTrue(model.examples_as_sys_msg)

        # Test llama3 70b case
        model = Model("llama3-70b")
        self.assertEqual(model.edit_format, "diff")
        self.assertTrue(model.use_repo_map)
        self.assertTrue(model.send_undo_reply)
        self.assertTrue(model.examples_as_sys_msg)

        # Test gpt-4 case
        model = Model("gpt-4")
        self.assertEqual(model.edit_format, "diff")
        self.assertTrue(model.use_repo_map)
        self.assertTrue(model.send_undo_reply)

        # Test gpt-3.5 case
        model = Model("gpt-3.5")
        self.assertEqual(model.reminder, "sys")

        # Test 3.5-sonnet case
        model = Model("claude-3.5-sonnet")
        self.assertEqual(model.edit_format, "diff")
        self.assertTrue(model.use_repo_map)
        self.assertTrue(model.examples_as_sys_msg)
        self.assertEqual(model.reminder, "user")

        # Test o1- prefix case
        model = Model("o1-something")
        self.assertFalse(model.use_system_prompt)
        self.assertFalse(model.use_temperature)

        # Test qwen case
        model = Model("qwen-coder-2.5-32b")
        self.assertEqual(model.edit_format, "diff")
        self.assertEqual(model.editor_edit_format, "editor-diff")
        self.assertTrue(model.use_repo_map)

    def test_aider_extra_model_settings(self):
        import tempfile

        import yaml

        # Create temporary YAML file with test settings
        test_settings = [
            {
                "name": "aider/extra_params",
                "extra_params": {
                    "extra_headers": {"Foo": "bar"},
                    "some_param": "some value",
                },
            },
        ]

        # Write to a regular file instead of NamedTemporaryFile
        # for better cross-platform compatibility
        tmp = tempfile.mktemp(suffix=".yml")
        try:
            with open(tmp, "w") as f:
                yaml.dump(test_settings, f)

            # Register the test settings
            register_models([tmp])

            # Test that defaults are applied when no exact match
            model = Model("claude-3-5-sonnet-20240620")
            # Test that both the override and existing headers are present
            model = Model("claude-3-5-sonnet-20240620")
            self.assertEqual(model.extra_params["extra_headers"]["Foo"], "bar")
            self.assertEqual(
                model.extra_params["extra_headers"]["anthropic-beta"],
                ANTHROPIC_BETA_HEADER,
            )
            self.assertEqual(model.extra_params["some_param"], "some value")
            self.assertEqual(model.extra_params["max_tokens"], 8192)

            # Test that exact match overrides defaults but not overrides
            model = Model("gpt-4")
            self.assertEqual(model.extra_params["extra_headers"]["Foo"], "bar")
            self.assertEqual(model.extra_params["some_param"], "some value")
        finally:
            # Clean up the temporary file
            import os

            try:
                os.unlink(tmp)
            except OSError:
                pass

    @patch("aider.models.litellm.acompletion")
    @patch.object(Model, "token_count")
    async def test_ollama_num_ctx_set_when_missing(self, mock_token_count, mock_completion):
        mock_token_count.return_value = 1000

        model = Model("ollama/llama3")
        model.extra_params = {}
        messages = [{"role": "user", "content": "Hello"}]

        await model.send_completion(messages, functions=None, stream=False)

        # Verify num_ctx was calculated and added to call
        expected_ctx = int(1000 * 1.25) + 8192  # 9442
        mock_completion.assert_called_once_with(
            model=model.name,
            messages=ANY,
            stream=False,
            temperature=0,
            num_ctx=expected_ctx,
            timeout=600,
            cache_control_injection_points=ANY,
        )

    @patch("aider.models.litellm.acompletion")
    async def test_modern_tool_call_propagation(self, mock_completion):
        # Test modern tool calling (used for MCP Server Tool Calls)
        model = Model("gpt-4")
        messages = [{"role": "user", "content": "Hello"}]

        await model.send_completion(
            messages, functions=None, stream=False, tools=[dict(type="function", function="test")]
        )

        mock_completion.assert_called_with(
            model=model.name,
            messages=ANY,
            stream=False,
            tools=[dict(type="function", function="test")],
            temperature=0,
            timeout=600,
            cache_control_injection_points=ANY,
        )

    @patch("aider.models.litellm.acompletion")
    async def test_legacy_tool_call_propagation(self, mock_completion):
        # Test modern tool calling (used for legacy server tool calling)
        model = Model("gpt-4")
        messages = [{"role": "user", "content": "Hello"}]

        await model.send_completion(messages, functions=["test"], stream=False)

        mock_completion.assert_called_with(
            model=model.name,
            messages=ANY,
            stream=False,
            tools=[dict(type="function", function="test")],
            temperature=0,
            timeout=600,
            cache_control_injection_points=ANY,
            tool_choice=ANY,
        )

    @patch("aider.models.litellm.acompletion")
    async def test_ollama_uses_existing_num_ctx(self, mock_completion):
        model = Model("ollama/llama3")
        model.extra_params = {"num_ctx": 4096}

        messages = [{"role": "user", "content": "Hello"}]
        await model.send_completion(messages, functions=None, stream=False)

        # Should use provided num_ctx from extra_params
        mock_completion.assert_called_once_with(
            model=model.name,
            messages=ANY,
            stream=False,
            temperature=0,
            num_ctx=4096,
            timeout=600,
            cache_control_injection_points=ANY,
        )

    @patch("aider.models.litellm.acompletion")
    async def test_non_ollama_no_num_ctx(self, mock_completion):
        model = Model("gpt-4")
        model.extra_params = {}
        messages = [{"role": "user", "content": "Hello"}]

        await model.send_completion(messages, functions=None, stream=False)

        # Regular models shouldn't get num_ctx
        mock_completion.assert_called_once_with(
            model=model.name,
            messages=ANY,
            stream=False,
            temperature=0,
            timeout=600,
            cache_control_injection_points=ANY,
        )
        self.assertNotIn("num_ctx", mock_completion.call_args.kwargs)

    def test_use_temperature_settings(self):
        # Test use_temperature=True (default) uses temperature=0
        model = Model("gpt-4")
        self.assertTrue(model.use_temperature)
        self.assertEqual(model.use_temperature, True)

        # Test use_temperature=False doesn't pass temperature
        model = Model("github/o1-mini")
        self.assertFalse(model.use_temperature)

        # Test use_temperature as float value
        model = Model("gpt-4")
        model.use_temperature = 0.7
        self.assertEqual(model.use_temperature, 0.7)

    @patch("aider.models.litellm.acompletion")
    async def test_request_timeout_default(self, mock_completion):
        # Test default timeout is used when not specified in extra_params
        model = Model("gpt-4")
        model.extra_params = {}
        messages = [{"role": "user", "content": "Hello"}]
        await model.send_completion(messages, functions=None, stream=False)
        mock_completion.assert_called_with(
            model=model.name,
            messages=ANY,
            stream=False,
            temperature=0,
            timeout=600,  # Default timeout
            cache_control_injection_points=ANY,
        )

    @patch("aider.models.litellm.acompletion")
    async def test_request_timeout_from_extra_params(self, mock_completion):
        # Test timeout from extra_params overrides default
        model = Model("gpt-4")
        model.extra_params = {"timeout": 300}  # 5 minutes
        messages = [{"role": "user", "content": "Hello"}]
        await model.send_completion(messages, functions=None, stream=False)
        mock_completion.assert_called_with(
            model=model.name,
            messages=ANY,
            stream=False,
            temperature=0,
            timeout=300,  # From extra_params
            cache_control_injection_points=ANY,
        )

    @patch("aider.models.litellm.acompletion")
    async def test_use_temperature_in_send_completion(self, mock_completion):
        # Test use_temperature=True sends temperature=0
        model = Model("gpt-4")
        model.extra_params = {}
        messages = [{"role": "user", "content": "Hello"}]
        await model.send_completion(messages, functions=None, stream=False)
        mock_completion.assert_called_with(
            model=model.name,
            messages=ANY,
            stream=False,
            temperature=0,
            timeout=600,
            cache_control_injection_points=ANY,
        )

        # Test use_temperature=False doesn't send temperature
        model = Model("github/o1-mini")
        messages = [{"role": "user", "content": "Hello"}]
        await model.send_completion(messages, functions=None, stream=False)
        self.assertNotIn("temperature", mock_completion.call_args.kwargs)

        # Test use_temperature as float sends that value
        model = Model("gpt-4")
        model.extra_params = {}
        model.use_temperature = 0.7
        messages = [{"role": "user", "content": "Hello"}]
        await model.send_completion(messages, functions=None, stream=False)
        mock_completion.assert_called_with(
            model=model.name,
            messages=ANY,
            stream=False,
            temperature=0.7,
            timeout=600,
            cache_control_injection_points=ANY,
        )

    def test_model_override_kwargs(self):
        """Test that override kwargs are applied to model extra_params."""
        # Test with override kwargs
        model = Model("gpt-4", override_kwargs={"temperature": 0.8, "top_p": 0.9})
        self.assertIn("temperature", model.extra_params)
        self.assertEqual(model.extra_params["temperature"], 0.8)
        self.assertIn("top_p", model.extra_params)
        self.assertEqual(model.extra_params["top_p"], 0.9)

        # Test that override kwargs merge with existing extra_params
        model = Model("gpt-4", override_kwargs={"extra_headers": {"X-Custom": "value"}})
        self.assertIn("extra_headers", model.extra_params)
        self.assertIn("X-Custom", model.extra_params["extra_headers"])
        self.assertEqual(model.extra_params["extra_headers"]["X-Custom"], "value")

        # Test nested dict merging
        model = Model("gpt-4", override_kwargs={"extra_body": {"reasoning_effort": "high"}})
        self.assertIn("extra_body", model.extra_params)
        self.assertIn("reasoning_effort", model.extra_params["extra_body"])
        self.assertEqual(model.extra_params["extra_body"]["reasoning_effort"], "high")

    def test_model_override_kwargs_with_existing_extra_params(self):
        """Test that override kwargs merge correctly with existing extra_params."""
        # Create a model with existing extra_params via model settings
        import tempfile

        import yaml

        test_settings = [
            {
                "name": "gpt-4",
                "extra_params": {"temperature": 0.5, "extra_headers": {"Existing": "header"}},
            },
        ]

        tmp = tempfile.mktemp(suffix=".yml")
        try:
            with open(tmp, "w") as f:
                yaml.dump(test_settings, f)

            register_models([tmp])

            # Test that override kwargs take precedence
            model = Model("gpt-4", override_kwargs={"temperature": 0.8, "top_p": 0.9})
            self.assertEqual(model.extra_params["temperature"], 0.8)  # Override wins
            self.assertEqual(model.extra_params["top_p"], 0.9)  # New param added
            self.assertIn("extra_headers", model.extra_params)
            self.assertEqual(
                model.extra_params["extra_headers"]["Existing"], "header"
            )  # Existing preserved

            # Test nested dict merging
            model = Model("gpt-4", override_kwargs={"extra_headers": {"New": "value"}})
            self.assertIn("Existing", model.extra_params["extra_headers"])
            self.assertIn("New", model.extra_params["extra_headers"])
            self.assertEqual(model.extra_params["extra_headers"]["Existing"], "header")
            self.assertEqual(model.extra_params["extra_headers"]["New"], "value")
        finally:
            import os

            try:
                os.unlink(tmp)
            except OSError:
                pass

    @patch("aider.models.litellm.acompletion")
    async def test_send_completion_with_override_kwargs(self, mock_completion):
        """Test that override kwargs are passed to acompletion."""
        # Create model with override kwargs
        model = Model("gpt-4", override_kwargs={"temperature": 0.8, "top_p": 0.9})
        messages = [{"role": "user", "content": "Hello"}]

        await model.send_completion(messages, functions=None, stream=False)

        # Check that override kwargs are in the call
        mock_completion.assert_called_once()
        call_kwargs = mock_completion.call_args.kwargs

        self.assertIn("temperature", call_kwargs)
        self.assertEqual(call_kwargs["temperature"], 0.8)
        self.assertIn("top_p", call_kwargs)
        self.assertEqual(call_kwargs["top_p"], 0.9)

        # Check that model name and other defaults are still there
        self.assertEqual(call_kwargs["model"], "gpt-4")
        self.assertFalse(call_kwargs["stream"])

    def test_parse_model_with_suffix(self):
        """Test the parse_model_with_suffix function from main.py."""

        # This test simulates the parse_model_with_suffix function logic
        def parse_model_with_suffix(model_name, overrides):
            """Parse model name with optional :suffix and apply overrides."""
            if not model_name:
                return model_name, {}

            # Split on last colon to get model name and suffix
            if ":" in model_name:
                base_model, suffix = model_name.rsplit(":", 1)
            else:
                base_model, suffix = model_name, None

            # Apply overrides if suffix exists
            override_kwargs = {}
            if suffix and base_model in overrides and suffix in overrides[base_model]:
                override_kwargs = overrides[base_model][suffix].copy()

            return base_model, override_kwargs

        # Test cases
        overrides = {
            "gpt-4o": {
                "high": {"reasoning_effort": "high", "temperature": 0.7},
                "low": {"reasoning_effort": "low", "temperature": 0.2},
            },
            "claude-3-5-sonnet": {"fast": {"temperature": 0.3}, "creative": {"temperature": 0.9}},
        }

        # Test with suffix
        base_model, kwargs = parse_model_with_suffix("gpt-4o:high", overrides)
        self.assertEqual(base_model, "gpt-4o")
        self.assertEqual(kwargs, {"reasoning_effort": "high", "temperature": 0.7})

        # Test with different suffix
        base_model, kwargs = parse_model_with_suffix("gpt-4o:low", overrides)
        self.assertEqual(base_model, "gpt-4o")
        self.assertEqual(kwargs, {"reasoning_effort": "low", "temperature": 0.2})

        # Test without suffix
        base_model, kwargs = parse_model_with_suffix("gpt-4o", overrides)
        self.assertEqual(base_model, "gpt-4o")
        self.assertEqual(kwargs, {})

        # Test with unknown suffix
        base_model, kwargs = parse_model_with_suffix("gpt-4o:unknown", overrides)
        self.assertEqual(base_model, "gpt-4o")
        self.assertEqual(kwargs, {})

        # Test with unknown model
        base_model, kwargs = parse_model_with_suffix("unknown-model:high", overrides)
        self.assertEqual(base_model, "unknown-model")
        self.assertEqual(kwargs, {})

        # Test empty model name
        base_model, kwargs = parse_model_with_suffix("", overrides)
        self.assertEqual(base_model, "")
        self.assertEqual(kwargs, {})


if __name__ == "__main__":
    unittest.main()
