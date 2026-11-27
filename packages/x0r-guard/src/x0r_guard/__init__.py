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
from .identity import (
    CallerTokenError,
    audience_key,
    sign_caller,
    token_key_id,
    verify_caller,
)
from .policy_file import PolicyFile, PolicyFileError, load_policy_file, load_policy_text
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
from .sink import core_sink

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
    "CallerTokenError",
    "audience_key",
    "token_key_id",
    "sign_caller",
    "verify_caller",
    "PolicyFile",
    "PolicyFileError",
    "load_policy_file",
    "load_policy_text",
    "DecisionNotRecorded",
    "DecisionRecord",
    "GuardError",
    "GuardedClient",
    "Policy",
    "ToolCallDenied",
    "UnguardedAttribute",
    "UnguardedCall",
    "wrap",
    "core_sink",
]
