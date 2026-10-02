import sys
from pathlib import Path
from types import SimpleNamespace
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from frappe_controller.operation_retry import RETRY_STATES, validate_retry_source
from frappe_controller.operation_service import OperationAuthoringError


class OperationRetryTests(unittest.TestCase):
    def source(self, **values):
        return SimpleNamespace(**dict(
            dict(state="failed", operation_type="site.create_blank", bulk_parent=None, bulk_target=None),
            **values,
        ))

    def test_all_terminal_failure_states_are_supported(self):
        for state in RETRY_STATES:
            validate_retry_source(self.source(state=state), recovery_confirmed=True)

    def test_active_and_successful_states_cannot_retry(self):
        for state in ["queued", "leased", "running", "awaiting_approval", "succeeded", "unknown"]:
            with self.subTest(state=state), self.assertRaises(OperationAuthoringError):
                validate_retry_source(self.source(state=state), recovery_confirmed=True)

    def test_uncertain_states_require_explicit_recovery_confirmation(self):
        for state in ["needs_intervention", "timed_out"]:
            with self.assertRaisesRegex(OperationAuthoringError, "partial changes"):
                validate_retry_source(self.source(state=state))

    def test_bulk_and_data_update_workflows_cannot_be_bypassed(self):
        for values in [dict(bulk_parent="bulk-1"), dict(bulk_target="target-1"),
                       dict(operation_type="data.update"), dict(operation_type="data.update.break_glass")]:
            with self.subTest(values=values), self.assertRaises(OperationAuthoringError):
                validate_retry_source(self.source(**values), recovery_confirmed=True)


if __name__ == "__main__":
    unittest.main()
