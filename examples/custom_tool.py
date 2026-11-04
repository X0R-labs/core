"""Add your own tool to X0R Core in ~20 lines.

Drop a file like this next to the built-ins, register it, then allow it on an agent:

    tools: ["knowledge_search", "order_lookup"]

Tools receive a ToolContext scoped to the calling workspace, so a tool can never read
another tenant's data by accident.
"""

from __future__ import annotations

from x0r_core.tools.base import Tool, ToolContext
from x0r_core.tools.builtin import REGISTRY


async def order_lookup(ctx: ToolContext, order_id: str) -> dict:
    # In a real deployment this would hit your order service. It gets the workspace id,
    # so you can scope the upstream call to the right account.
    fake_orders = {
        "A-1001": {"status": "in_transit", "eta": "2026-11-06", "value_usd": 1240},
        "A-1002": {"status": "delivered", "eta": None, "value_usd": 89},
    }
    order = fake_orders.get(order_id.upper())
    if order is None:
        return {"error": f"no order {order_id!r} in workspace {ctx.workspace_id}"}
    return {"order_id": order_id.upper(), **order}


ORDER_LOOKUP = Tool(
    name="order_lookup",
    description="Look up a customer order by id and return its status and ETA.",
    parameters={
        "type": "object",
        "properties": {"order_id": {"type": "string", "description": "e.g. A-1001"}},
        "required": ["order_id"],
    },
    handler=order_lookup,
)

REGISTRY[ORDER_LOOKUP.name] = ORDER_LOOKUP
