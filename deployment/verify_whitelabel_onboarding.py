"""Build-time regression check against the patched, pinned Whitelabel source.

Run inside the ERP image build, where both upstream application trees exist.
"""

import ast
import json
from pathlib import Path
from types import SimpleNamespace


root = Path("/home/frappe/frappe-bench/apps")
schema = json.loads((root / "frappe/frappe/desk/doctype/onboarding_step/onboarding_step.json").read_text())
actions = next(field["options"].splitlines() for field in schema["fields"] if field["fieldname"] == "action")


class Step(SimpleNamespace):
    def save(self, ignore_permissions=False):
        raise AssertionError("Cosmetic updates must not resave obsolete document links")


def set_value(doctype, name, updates):
    assert doctype == "Onboarding Step"
    assert set(updates) <= {"intro_video_url", "description", "action", "path"}
    if "action" in updates:
        assert updates["action"] in actions
    steps[name].__dict__.update(updates)


steps = {
    name: Step(action=action, path=path, video_url="https://youtu.be/example",
               reference_document="Removed Legacy DocType", is_complete=1, is_skipped=0,
               flags=SimpleNamespace(), intro_video_url="intro", description="description")
    for name, action, path in [
        ("legacy", "Watch Video", None),
        ("custom_destination", "Watch Video", "https://example.com/guide"),
        ("report", "View Report", None),
    ]
}
source = ast.parse((root / "whitelabel/whitelabel/api.py").read_text())
function = next(node for node in source.body if isinstance(node, ast.FunctionDef) and node.name == "update_onborad_steps")
namespace = {"frappe": SimpleNamespace(
    db=SimpleNamespace(set_value=set_value),
    get_all=lambda *args, **kwargs: [SimpleNamespace(name=name) for name in steps],
    get_doc=lambda doctype, name: steps[name],
)}
exec(compile(ast.Module(body=[function], type_ignores=[]), "whitelabel/api.py", "exec"), namespace)
for _ in range(2):
    namespace["update_onborad_steps"]()
    assert steps["legacy"].action == "View Docs"
    assert steps["legacy"].path == "https://youtu.be/example"
    assert steps["custom_destination"].path == "https://example.com/guide"
    assert steps["report"].action == "View Report" and steps["report"].path is None
    for step in steps.values():
        assert step.is_complete == 1 and step.is_skipped == 0
        assert step.reference_document == "Removed Legacy DocType"
        assert step.video_url == "https://youtu.be/example"
        assert step.intro_video_url == "" and step.description == ""
print("Whitelabel v16 onboarding regression checks passed")
