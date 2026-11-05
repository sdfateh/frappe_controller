"""Disable the moved Agent on the old Controller without deleting history."""

from move_controller_settings import bench


def main():
    bench("controller-frappe-1", "/home/frappe/controller-data/frappe-bench", "controller.kaleam.net", """
active = frappe.get_all('Controller Operation', filters={
    'server_agent': 'server-01',
    'state': ['not in', ['succeeded', 'failed', 'cancelled', 'needs_intervention']],
}, pluck='name')
assert not active, 'Refusing to retire Agent with unfinished jobs'
agent = frappe.get_doc('Server Agent', 'server-01')
agent.enabled = 0
agent.save(ignore_permissions=True)
frappe.db.commit()
""")
    print("Old Controller Agent disabled; all historical records preserved")


if __name__ == '__main__':
    main()
