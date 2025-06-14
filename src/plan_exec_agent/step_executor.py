import json
import os

from anthropic import Anthropic
from arcadepy import Arcade
from dotenv import load_dotenv
from langfuse.decorators import langfuse_context, observe
from openai import OpenAI

from .agent_types import State

from .arcade_utils import ModelProvider, get_toolkits_from_arcade
from .llm_utils import LLMMessageCreator
from .tool_processor import ToolProcessor

load_dotenv()


class StepExecutor:
    def __init__(
        self,
        default_system_prompt: str | None = None,
        user_context: str | None = None,
        enabled_toolkits: list[str] | None = None,
    ):
        self.arcade_client = Arcade(api_key=os.getenv("ARCADE_API_KEY"))
        self.tool_processor = ToolProcessor(arcade_client=self.arcade_client)
        self.enabled_toolkits = enabled_toolkits

        if not enabled_toolkits:
            print("WARNING: No toolkits enabled when initializing StepExecutor")

        # Initialize LLM clients
        anthropic_client = (
            Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
            if os.getenv("ANTHROPIC_API_KEY")
            else None
        )
        openai_client = (
            OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
            if os.getenv("OPENAI_API_KEY")
            else None
        )

        # Initialize message creator with available clients
        self.message_creator = LLMMessageCreator(
            anthropic_client=anthropic_client, openai_client=openai_client
        )

        # inject the user context into the system prompt if its provided
        if default_system_prompt and user_context:
            default_system_prompt = f"""
            {default_system_prompt}
            
            USER CONTEXT:
            {user_context}
            """

        # Store system prompt as instance variable with a default
        self.system_prompt = default_system_prompt or "You are a helpful assistant."
        self.user_context = user_context if user_context else ""

    def _get_reference_tool(self, provider: ModelProvider):
        if provider == ModelProvider.ANTHROPIC:
            return {
                "name": "reference_tool_output",
                "description": "Reference the output of a previously called tool from any of the prior steps.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "tool_id": {
                            "type": "string",
                            "description": "The ID of the previously called tool. This is NOT the name of the tool, it is the ID of the tool call. ",
                        },
                    },
                    "required": ["tool_id"],
                },
            }
        elif provider == ModelProvider.OPENAI:
            return {
                "type": "function",
                "function": {
                    "name": "reference_tool_output",
                    "description": "Reference the output of a previously called tool from any of the prior steps.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "tool_id": {
                                "type": "string",
                                "description": "The ID of the previously called tool. This is NOT the name of the tool, it is the ID of the tool call. ",
                            },
                        },
                        "required": ["tool_id"],
                    },
                },
            }
        else:
            raise ValueError(f"Unsupported provider: {provider}")

    def _get_previous_step_tool(self, provider: ModelProvider):
        """Tool to access results from previous steps in the plan."""
        if provider == ModelProvider.ANTHROPIC:
            return {
                "name": "get_previous_step_result",
                "description": "Get the result from a previous step in the plan. Use this when you need to reference what was accomplished in an earlier step. This is the output from the execution agent, not from a tool call.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "step_number": {
                            "type": "integer",
                            "description": "The step number (1-based) to get the result from. For example, use 1 for the first step, 2 for the second step, etc.",
                            "minimum": 1,
                        },
                    },
                    "required": ["step_number"],
                },
            }
        elif provider == ModelProvider.OPENAI:
            return {
                "type": "function",
                "function": {
                    "name": "get_previous_step_result",
                    "description": "Get the result from a previous step in the plan. Use this when you need to reference what was accomplished in an earlier step. This is the output from the execution agent, not from a tool call.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "step_number": {
                                "type": "integer",
                                "description": "The step number (1-based) to get the result from. For example, use 1 for the first step, 2 for the second step, etc.",
                                "minimum": 1,
                            },
                        },
                        "required": ["step_number"],
                    },
                },
            }
        else:
            raise ValueError(f"Unsupported provider: {provider}")

    def _get_insufficient_context_tool(self, provider: ModelProvider):
        """Tool to signal that the step cannot be completed due to insufficient context."""
        if provider == ModelProvider.ANTHROPIC:
            return {
                "name": "signal_insufficient_context",
                "description": "Signal that this step cannot be completed due to insufficient context or missing required information. Use this when you determine that you do not have enough information to proceed successfully with the current step. If in doubt, use this tool.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "reason": {
                            "type": "string",
                            "description": "Explanation of what specific information or context is missing that prevents completing this step.",
                        },
                    },
                    "required": ["reason"],
                },
            }
        elif provider == ModelProvider.OPENAI:
            return {
                "type": "function",
                "function": {
                    "name": "signal_insufficient_context",
                    "description": "Signal that this step cannot be completed due to insufficient context or missing required information. Use this when you determine that you do not have enough information to proceed successfully with the current step. If in doubt, use this tool.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "reason": {
                                "type": "string",
                                "description": "Explanation of what specific information or context is missing that prevents completing this step.",
                            },
                        },
                        "required": ["reason"],
                    },
                },
            }
        else:
            raise ValueError(f"Unsupported provider: {provider}")

    def get_all_tools(self, provider: ModelProvider):
        # Add tools for referencing previous results
        reference_tool = self._get_reference_tool(provider)
        previous_step_tool = self._get_previous_step_tool(provider)
        insufficient_context_tool = self._get_insufficient_context_tool(provider)
        arcade_tools = get_toolkits_from_arcade(
            self.arcade_client, provider, self.enabled_toolkits
        )
        # inject the custom tools first due to primacy bias
        return [reference_tool, previous_step_tool, insufficient_context_tool] + arcade_tools

    @observe()
    def process_input_with_agent_loop(
        self,
        input_action: str,
        provider: ModelProvider,
        user_id: str,
        system_prompt: str | None = None,
        langfuse_session_id: str | None = None,
        state: State | None = None,
        max_iterations: int = 25,
    ):
        """
        Process the input with the agent loop.

        NOTE: we pass in the langfuse_session_id separately in case there is not a state
        """
        current_system_prompt = (
            system_prompt if system_prompt is not None else self.system_prompt
        )

        # Set the observation name to include the current step if available
        if state and "current_plan" in state and state["current_plan"]:
            current_step = state["current_plan"][0]
            langfuse_context.update_current_observation(name=f"{current_step}")

        if self.user_context:
            input_action += f"\n\nUSER CONTEXT:\n{self.user_context}"

        messages = [{"role": "user", "content": input_action}]

        # authorize only the enabled tools as part of the state of the agent
        available_tools = (
            state["tools"]
            if ("tools" in state and state["tools"])
            else self.get_all_tools(provider)
        )

        response = self.message_creator.create_message(
            provider=provider,
            messages=messages,
            available_tools=available_tools,
            system_prompt=current_system_prompt,
            langfuse_data={"session_id": langfuse_session_id, "user_id": user_id},
        )

        final_text = []
        i = 0

        # Continue processing until we have a complete response
        while True and i < max_iterations:
            assistant_message_content = []
            has_tool_calls = False

            # Handle different response formats based on provider
            if provider == ModelProvider.ANTHROPIC:
                response_contents = response.content
            elif provider == ModelProvider.OPENAI:
                # OpenAI returns a single choice with a message
                message = response.choices[0].message
                # Convert OpenAI format to match Anthropic's structure
                response_contents = []
                if message.content:
                    response_contents.append({"type": "text", "text": message.content})
                if message.tool_calls:
                    for tool_call in message.tool_calls:
                        response_contents.append({
                            "type": "tool_use",
                            "name": tool_call.function.name,
                            "input": json.loads(tool_call.function.arguments),
                            "id": tool_call.id,
                        })

            for content in response_contents:
                if ("type" in content and content["type"] == "text") or (
                    hasattr(content, "type") and content.type == "text"
                ):
                    final_text.append(
                        content["text"] if isinstance(content, dict) else content.text
                    )
                    assistant_message_content.append(content)
                elif ("type" in content and content["type"] == "tool_use") or (
                    hasattr(content, "type") and content.type == "tool_use"
                ):
                    has_tool_calls = True
                    tool_name = (
                        content["name"] if isinstance(content, dict) else content.name
                    )
                    tool_args = (
                        content["input"] if isinstance(content, dict) else content.input
                    )
                    tool_id = content["id"] if isinstance(content, dict) else content.id

                    # Process the specific tool call
                    updated_messages, result_content = (
                        self.tool_processor.process_tool_call(
                            tool_name,
                            tool_args,
                            tool_id,
                            content,
                            assistant_message_content,
                            messages,
                            state,
                            final_text,
                            user_id,
                            provider,
                            langfuse_data={
                                "session_id": langfuse_session_id,
                                "user_id": user_id,
                            },
                        )
                    )

                    # Check if we should break out of the loop
                    if result_content and result_content.startswith("STEP_FAILED_INSUFFICIENT_CONTEXT"):
                        final_text.append(result_content)
                        return final_text

                    # Update conversation context
                    messages = updated_messages
                    if (
                        result_content
                        and tool_name != "get_previous_step_result"
                        and tool_name != "reference_tool_output"
                    ):
                        state["tool_results"][tool_id] = (tool_name, result_content)

                    # Get next response
                    response = self.message_creator.create_message(
                        provider=provider,
                        messages=messages,
                        available_tools=available_tools,
                        system_prompt=current_system_prompt,
                        langfuse_data={
                            "session_id": langfuse_session_id,
                            "user_id": user_id,
                        },
                    )

                    # Break the content loop to process the new response
                    break

            # the RESULT of the last item in final_text was added either above or in the tool_processor
            if not has_tool_calls:
                break

            i += 1
            if i == max_iterations:
                print(
                    f"WARNING:Max iterations reached: {max_iterations}.\nExiting loop."
                )

        return final_text
