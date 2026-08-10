"""Administrator-authenticated enrollment and authenticated rotation flows."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
import os
from pathlib import Path
from typing import Any, Protocol

from ..controller_store import ControllerStore
from ..security import (
    CertificateIssuance,
    ControllerRequestError,
    TrustedAdministrator,
    TrustedPeerIdentity,
    VerifiedCSR,
    parse_exact_json,
    timestamp,
    validate_common,
)

_ENROLL_FIELDS = frozenset({"protocol_version", "agent_id", "enrollment_token", "csr_pem"})
_ROTATE_FIELDS = frozenset({"protocol_version", "agent_id", "audience", "csr_pem"})


class CSRVerifier(Protocol):
    def __call__(self, csr_pem: str) -> VerifiedCSR: ...


class CertificateIssuer(Protocol):
    def __call__(self, verified_csr: VerifiedCSR) -> CertificateIssuance: ...


@dataclass(frozen=True, slots=True)
class FrappeEnrollmentRuntime:
    """Production dependencies for Frappe enrollment and rotation routes."""

    store: Any
    verify_csr: CSRVerifier
    issue_certificate: CertificateIssuer

    @classmethod
    def from_environment(
        cls,
        *,
        environ: Mapping[str, str] | None = None,
        frappe_module: Any | None = None,
    ) -> "FrappeEnrollmentRuntime":
        from ..frappe_security_store import FrappeCertificateStore
        from ..pki import CertificateAuthority, verify_agent_csr

        values = environ if environ is not None else os.environ
        certificate_path = values.get("FRAPPE_CONTROLLER_CA_CERT_FILE", "")
        private_key_path = values.get("FRAPPE_CONTROLLER_CA_KEY_FILE", "")
        if not certificate_path or not private_key_path:
            raise RuntimeError("controller CA certificate and key files are required")
        password_path = values.get("FRAPPE_CONTROLLER_CA_KEY_PASSWORD_FILE", "")
        chain_path = values.get("FRAPPE_CONTROLLER_CA_CHAIN_FILE", "")
        password = Path(password_path).read_bytes().rstrip(b"\r\n") if password_path else None
        chain = Path(chain_path).read_text(encoding="ascii") if chain_path else ""
        return cls(
            store=FrappeCertificateStore.from_environment(
                environ=dict(values), frappe_module=frappe_module
            ),
            verify_csr=verify_agent_csr,
            issue_certificate=CertificateAuthority.from_files(
                certificate_path,
                private_key_path,
                private_key_password=password,
                ca_chain_pem=chain,
            ),
        )


def _csr(body: Mapping[str, Any], verifier: CSRVerifier) -> VerifiedCSR:
    csr_pem = body.get("csr_pem")
    if not isinstance(csr_pem, str) or not (64 <= len(csr_pem.encode("utf-8")) <= 64 * 1024):
        raise ControllerRequestError("invalid_csr")
    if "PRIVATE KEY" in csr_pem:
        raise ControllerRequestError("private_key_rejected")
    try:
        verified = verifier(csr_pem)
    except ControllerRequestError:
        raise
    except Exception:
        raise ControllerRequestError("csr_verification_failed", 403) from None
    if type(verified) is not VerifiedCSR:
        raise ControllerRequestError("csr_verification_failed", 403)
    return verified


def _response(issuance: CertificateIssuance) -> dict[str, Any]:
    return {
        "accepted": True,
        "certificate": issuance.certificate_pem,
        "ca_chain": issuance.ca_chain_pem,
        "serial": issuance.serial,
        "fingerprint_sha256": issuance.fingerprint_sha256,
        "not_before": timestamp(issuance.not_before),
        "not_after": timestamp(issuance.not_after),
    }


def enroll_agent(
    store: ControllerStore,
    raw_body: bytes | str | Mapping[str, Any],
    *,
    administrator: TrustedAdministrator,
    verify_csr: CSRVerifier,
    issue_certificate: CertificateIssuer,
    now: datetime | None = None,
) -> dict[str, Any]:
    if type(administrator) is not TrustedAdministrator:
        raise ControllerRequestError("administrator_authentication_required", 401)
    body = parse_exact_json(raw_body, _ENROLL_FIELDS, limit=96 * 1024)
    if body["protocol_version"] != "1.0":
        raise ControllerRequestError("unsupported_protocol")
    if not isinstance(body["agent_id"], str) or not isinstance(body["enrollment_token"], str):
        raise ControllerRequestError("invalid_enrollment_request")
    verified = _csr(body, verify_csr)
    if verified.agent_id != body["agent_id"]:
        raise ControllerRequestError("csr_agent_mismatch", 403)
    store.claim_enrollment_token(body["enrollment_token"], verified, now=now)
    try:
        issuance = issue_certificate(verified)
    except Exception:
        raise ControllerRequestError("certificate_issuance_failed", 500) from None
    if type(issuance) is not CertificateIssuance:
        raise ControllerRequestError("certificate_issuance_failed", 500)
    store.complete_enrollment(
        body["enrollment_token"],
        verified,
        issuance,
        actor=administrator.actor_id,
        now=now,
    )
    return _response(issuance)


def rotate_certificate(
    store: ControllerStore,
    path_agent_id: str,
    raw_body: bytes | str | Mapping[str, Any],
    peer: TrustedPeerIdentity,
    *,
    verify_csr: CSRVerifier,
    issue_certificate: CertificateIssuer,
    overlap_seconds: int = 120,
    now: datetime | None = None,
) -> dict[str, Any]:
    body = parse_exact_json(raw_body, _ROTATE_FIELDS, limit=96 * 1024)
    agent_id = validate_common(body, path_agent_id)
    store.authenticate_peer(peer, agent_id, body["agent_id"], now=now)
    verified = _csr(body, verify_csr)
    if verified.agent_id != agent_id:
        raise ControllerRequestError("csr_agent_mismatch", 403)
    try:
        issuance = issue_certificate(verified)
    except Exception:
        raise ControllerRequestError("certificate_issuance_failed", 500) from None
    if type(issuance) is not CertificateIssuance:
        raise ControllerRequestError("certificate_issuance_failed", 500)
    store.rotate_certificate(
        peer,
        verified,
        issuance,
        actor=f"agent:{agent_id}",
        overlap_seconds=overlap_seconds,
        now=now,
    )
    return _response(issuance)
