# Controller configuration screenshots

Captured from the deployed `controller.kaleam.net` Desk on 3 October 2026 for
`../../CONTROLLER_CONFIGURATION_GUIDE.md`. These are screenshots of the actual
UI, not generated illustrations. They are not current health evidence.

Password input regions and signing/enrollment material were concealed before
capture. No credentials, cookies, browser storage state or browser traces are
included. The existing Server Agent/Bench identifiers and public ingress address
remain visible to explain the deployment. Review those identifiers before
sharing outside the intended administrative audience.

The production Approval Policy is an unsaved example. The Create Site dialog
uses an illustrative hostname and was never submitted. No token was generated,
password retrieved, job created, or setting saved for the screenshots.

The current deployed screens do not yet include the new Bench required-app field
or Customer readiness button. The guide explicitly distinguishes these from
the pushed source. Replace screenshots after the matching release is deployed
and repeat credential-redaction and visual checks.

To regenerate the PDF/HTML after editing the Markdown or screenshots, run
`python docs/build_configuration_guide.py` in an isolated documentation
environment containing `markdown-it-py`, `playwright` and its Chromium browser.
The renderer is local-only and does not contact the Controller.
