"""Copy only Controller settings between local sites, never business data.

Run on the Docker host. Secrets move through captured subprocess pipes only;
Frappe re-encrypts Password fields with the destination site's existing key.
The target must have no Controller URL configured. No jobs or users are copied.
"""

import json
import subprocess


def bench(container, root, site, source):
    program = (
        "import frappe, json\n"
        f"frappe.init(site={site!r}, sites_path='.')\n"
        "frappe.connect()\nfrappe.set_user('Administrator')\n"
        "try:\n" + "\n".join("    " + line for line in source.splitlines()) +
        "\nfinally:\n    frappe.db.rollback()\n    frappe.destroy()\n"
    )
    result = subprocess.run(
        ["docker", "exec", "-i", "-w", root + "/sites", container,
         root + "/env/bin/python", "-"], input=program, text=True, capture_output=True,
    )
    if result.returncode:
        # Traces can contain Password values. Do not echo subprocess output.
        raise RuntimeError(f"Settings transfer failed in {container}; transaction rolled back")
    return result.stdout


def main():
    settings = json.loads(bench(
        "controller-frappe-1", "/home/frappe/controller-data/frappe-bench", "controller.kaleam.net", """
active = frappe.get_all('Controller Operation', filters={'state': ['in', ['queued', 'leased', 'running', 'approved', 'awaiting_approval']]}, pluck='name')
assert not active, 'Old Controller has active jobs'
doc = frappe.get_single('Frappe Controller Settings')
values = {}
for field in doc.meta.fields:
    if field.fieldtype in ('Section Break', 'Column Break', 'HTML', 'Button'):
        continue
    value = doc.get(field.fieldname)
    if field.fieldtype == 'Password':
        value = doc.get_password(field.fieldname) if value else ''
    values[field.fieldname] = value
print(json.dumps(values))
"""))
    settings['public_controller_url'] = 'https://erp.kaleam.net'
    # Embedded only in stdin, never argv, shell commands, logs or a disk export.
    bench("erp-backend-1", "/home/frappe/frappe-bench", "erp.kaleam.net", f"""
from frappe_controller.controller_settings import load_controller_settings
values = json.loads({json.dumps(settings)!r})
doc = frappe.get_single('Frappe Controller Settings')
assert not doc.public_controller_url, 'Target already configured; refusing overwrite'
doc.update(values)
doc.save(ignore_permissions=True)
verified = load_controller_settings(frappe)
for field in doc.meta.fields:
    if field.fieldtype == 'Password' and values.get(field.fieldname):
        assert getattr(verified, field.fieldname) == values[field.fieldname]
frappe.db.commit()
""")
    print("Controller settings transferred; Password fields verified; ERP encryption key unchanged")


if __name__ == '__main__':
    main()
