"""Read-only GMod queue tools for ordinary chat."""

from yggdrasil.agent.tools.base import chat_tool
from yggdrasil.tools import gmod_queue

gmod_queue_lookup = chat_tool("gmod_queue_lookup")(gmod_queue.gmod_queue_lookup)
gmod_queue_list = chat_tool("gmod_queue_list")(gmod_queue.gmod_queue_list)
