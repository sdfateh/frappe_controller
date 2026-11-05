# Build from the frappe_controller repository root. Keep the upstream ERP apps
# and dependencies from the deployed v16 image; replace Controller completely
# so its retired Operation DocType cannot be rediscovered during schema sync.
FROM kaleam/mos@sha256:6c1bef16fff0cf53a60edf4e495eb63bd22d009270e7cdaf59238ce1bafec5be
USER frappe
WORKDIR /home/frappe/frappe-bench
COPY --chown=frappe:frappe deployment/whitelabel-v16-onboarding.patch /tmp/whitelabel-v16-onboarding.patch
COPY --chown=frappe:frappe deployment/verify_whitelabel_onboarding.py /tmp/verify_whitelabel_onboarding.py
RUN sed -i 's/\r$//' apps/whitelabel/whitelabel/api.py \
    && patch --batch --fuzz=0 -p1 < /tmp/whitelabel-v16-onboarding.patch \
    && env/bin/python /tmp/verify_whitelabel_onboarding.py \
    && rm /tmp/whitelabel-v16-onboarding.patch /tmp/verify_whitelabel_onboarding.py
RUN rm -rf apps/frappe_controller/frappe_controller
COPY --chown=frappe:frappe frappe_controller/ apps/frappe_controller/frappe_controller/
COPY --chown=frappe:frappe pyproject.toml README.md apps/frappe_controller/
RUN env/bin/python -m pip install --no-deps --no-build-isolation --disable-pip-version-check --no-cache-dir -e apps/frappe_controller \
    && test ! -d apps/frappe_controller/frappe_controller/frappe_controller/doctype/operation \
    && bench build --app frappe_controller
