"""model-interface: the model call and the tool execution, taken out of the agent.

An agent sends one request in Anthropic's Messages format or OpenAI's Chat Completions
format. This platform calls the model with its own provider key, runs any registered tool
the model asks for in a container the agent can't reach, and answers with everything that
happened. The agent keeps its context and its input; it never holds a provider key and
never runs a command.
"""
