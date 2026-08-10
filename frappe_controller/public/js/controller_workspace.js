/* Read-only Desk helpers. No command submission or mutation actions live here. */
(function () {
  "use strict";

  const terminalFailures = new Set([
    "failed",
    "timed_out",
    "needs_intervention",
    "dead_letter",
    "rejected",
  ]);

  const rollout = (frappe.boot && frappe.boot.frappe_controller_features) || {
    master_enabled: false,
    workspace_visible: false,
    features: {},
  };

  const featureEnabled = (feature, environment = null) => {
    if (!rollout.master_enabled) return false;
    const environments = rollout.features[feature];
    if (!Array.isArray(environments) || !environments.length) return false;
    return environment === null || environments.includes(environment);
  };

  window.frappeControllerDesk = Object.freeze({
    featureEnabled,
    operationIndicator(state) {
      if (terminalFailures.has(state)) return [__(state), "red"];
      if (state === "succeeded") return [__(state), "green"];
      if (state === "running") return [__(state), "blue"];
      return [__(state || "unknown"), "gray"];
    },
    agentIndicator(status) {
      if (status === "offline" || status === "error") return [__(status), "red"];
      if (status === "degraded" || status === "maintenance") return [__(status), "orange"];
      return [__(status || "unknown"), status === "ready" ? "green" : "gray"];
    },
  });

  const guardHiddenWorkspace = () => {
    if (rollout.workspace_visible || !frappe.get_route) return;
    const route = frappe.get_route();
    if ((route || []).some((part) => part === "Frappe Controller")) {
      frappe.show_alert({ message: __("Frappe Controller is disabled"), indicator: "orange" });
      frappe.set_route("home");
    }
  };
  if (frappe.router && frappe.router.on) {
    frappe.router.on("change", guardHiddenWorkspace);
  }
  frappe.ready(guardHiddenWorkspace);
})();
