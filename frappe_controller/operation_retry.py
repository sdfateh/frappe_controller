"""Retry eligibility shared by the API and immutable Operation validation."""

from .lifecycle_authoring import LIFECYCLE_AUTHORING_OPERATIONS
from .operation_service import OperationAuthoringError


RETRY_STATES = frozenset({"failed", "cancelled", "timed_out", "needs_intervention", "dead_letter", "rejected"})
RECOVERY_STATES = frozenset({"timed_out", "needs_intervention"})


def validate_retry_source(operation, *, recovery_confirmed=False):
    if operation.state not in RETRY_STATES:
        raise OperationAuthoringError("Only finished, unsuccessful operations can be retried")
    if operation.bulk_parent or operation.bulk_target:
        raise OperationAuthoringError("Retry bulk children through the Bulk Operation workflow")
    if operation.operation_type not in LIFECYCLE_AUTHORING_OPERATIONS:
        raise OperationAuthoringError("This operation requires its dedicated workflow and a fresh preview")
    if operation.state in RECOVERY_STATES and not recovery_confirmed:
        raise OperationAuthoringError(
            "Review the original job and verify it has stopped and any partial changes are safe before confirming recovery"
        )
