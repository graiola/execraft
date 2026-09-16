"""Task-completion configuration and typed contracts.

The service itself is intentionally not imported here: workspace lifecycle code
imports orchestration modules, so eagerly importing the service would create a
package-initialization cycle. Consumers that execute completion should import
``execraft.completion.service.TaskCompletionService`` directly.
"""

from .models import TaskCompletionError, TaskCompletionPolicy, TaskCompletionResult
from .policy import task_completion_policy_from_scheduling

__all__ = [
    "TaskCompletionError",
    "TaskCompletionPolicy",
    "TaskCompletionResult",
    "task_completion_policy_from_scheduling",
]
