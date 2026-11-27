"""X0R Guard: decides whether an agent may take an action.

This package uses only the standard library. It knows nothing about models, gateways,
databases or the web app, so any connector can call it.
"""
from .engine import (
    MAX_STRING_LENGTH,
    POLICY_VERSION,
    ActionRequest,
    Authorizer,
    Caller,
    Decision,
    HookContext,
    ToolPolicy,
    check_action,
    check_arguments,
)
from .sdk import (
    DecisionNotRecorded,
    DecisionRecord,
    GuardedClient,
    GuardError,
    Policy,
    ToolCallDenied,
    UnguardedAttribute,
    UnguardedCall,
    wrap,
)

__all__ = [
    "MAX_STRING_LENGTH",
    "POLICY_VERSION",
    "ActionRequest",
    "Authorizer",
    "Caller",
    "Decision",
    "HookContext",
    "ToolPolicy",
    "check_action",
    "check_arguments",
    "DecisionNotRecorded",
    "DecisionRecord",
    "GuardError",
    "GuardedClient",
    "Policy",
    "ToolCallDenied",
    "UnguardedAttribute",
    "UnguardedCall",
    "wrap",
]
