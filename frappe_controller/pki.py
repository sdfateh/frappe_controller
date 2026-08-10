"""Concrete PKI primitives for agent enrollment and certificate rotation.

The CSR DER carried by :class:`VerifiedCSR` is deliberately ephemeral.  Store
adapters persist its digests, while the issuer uses it to guarantee that the
certificate contains the exact key and identity whose signature was verified.
"""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import (
    ec,
    ed25519,
    ed448,
    padding,
    rsa,
)
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from .security import CertificateIssuance, ControllerRequestError, VerifiedCSR, require_agent_id

AGENT_URI_PREFIX = "urn:frappe-controller:agent:"
_ALLOWED_HASHES = (hashes.SHA256, hashes.SHA384, hashes.SHA512)


def agent_identity_uri(agent_id: str) -> str:
    """Return the sole identity representation accepted in agent SANs."""
    return AGENT_URI_PREFIX + require_agent_id(agent_id)


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _public_key_digest(public_key: object) -> str:
    encoded = public_key.public_bytes(  # type: ignore[attr-defined]
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return hashlib.sha256(encoded).hexdigest()


def _validate_public_key(public_key: object) -> None:
    if isinstance(public_key, rsa.RSAPublicKey):
        if public_key.key_size < 2048:
            raise ControllerRequestError("csr_key_too_weak", 403)
        return
    if isinstance(public_key, ec.EllipticCurvePublicKey):
        if not isinstance(public_key.curve, (ec.SECP256R1, ec.SECP384R1)):
            raise ControllerRequestError("csr_key_not_allowed", 403)
        return
    if isinstance(public_key, (ed25519.Ed25519PublicKey, ed448.Ed448PublicKey)):
        return
    raise ControllerRequestError("csr_key_not_allowed", 403)


def _csr_agent_id(csr: x509.CertificateSigningRequest) -> str:
    common_names = csr.subject.get_attributes_for_oid(NameOID.COMMON_NAME)
    if len(common_names) != 1:
        raise ControllerRequestError("csr_identity_missing", 403)
    try:
        agent_id = require_agent_id(common_names[0].value)
    except ValueError:
        raise ControllerRequestError("csr_identity_invalid", 403) from None
    try:
        san = csr.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    except x509.ExtensionNotFound:
        raise ControllerRequestError("csr_identity_missing", 403) from None
    names = list(san)
    expected = x509.UniformResourceIdentifier(agent_identity_uri(agent_id))
    if names != [expected]:
        raise ControllerRequestError("csr_identity_invalid", 403)
    return agent_id


def verify_agent_csr(csr_pem: str) -> VerifiedCSR:
    """Parse a PEM CSR, verify its signature/key policy, and bind its identity.

    Agent identity must appear both as the sole subject common name and as the
    sole SAN, using ``urn:frappe-controller:agent:<agent_id>``.  Requiring one
    canonical SAN avoids ambiguous DNS, IP, email, or multiple-agent claims.
    """
    if not isinstance(csr_pem, str) or "PRIVATE KEY" in csr_pem:
        raise ControllerRequestError("invalid_csr")
    encoded = csr_pem.encode("ascii", "strict")
    if not (64 <= len(encoded) <= 64 * 1024):
        raise ControllerRequestError("invalid_csr")
    try:
        csr = x509.load_pem_x509_csr(encoded)
    except (ValueError, TypeError):
        raise ControllerRequestError("csr_verification_failed", 403) from None
    if not csr.is_signature_valid:
        raise ControllerRequestError("csr_verification_failed", 403)
    signature_hash = csr.signature_hash_algorithm
    if signature_hash is not None and not isinstance(signature_hash, _ALLOWED_HASHES):
        raise ControllerRequestError("csr_signature_algorithm_not_allowed", 403)
    public_key = csr.public_key()
    _validate_public_key(public_key)
    agent_id = _csr_agent_id(csr)
    der = csr.public_bytes(serialization.Encoding.DER)
    return VerifiedCSR(
        agent_id=agent_id,
        public_key_sha256=_public_key_digest(public_key),
        csr_sha256=hashlib.sha256(der).hexdigest(),
        csr_der=der,
    )


def _verify_signature(certificate: x509.Certificate, issuer_public_key: object) -> None:
    algorithm = certificate.signature_hash_algorithm
    if algorithm is not None and not isinstance(algorithm, _ALLOWED_HASHES):
        raise ControllerRequestError("certificate_signature_algorithm_not_allowed", 401)
    try:
        if isinstance(issuer_public_key, rsa.RSAPublicKey):
            issuer_public_key.verify(
                certificate.signature,
                certificate.tbs_certificate_bytes,
                padding.PKCS1v15(),
                algorithm,
            )
        elif isinstance(issuer_public_key, ec.EllipticCurvePublicKey):
            issuer_public_key.verify(
                certificate.signature,
                certificate.tbs_certificate_bytes,
                ec.ECDSA(algorithm),
            )
        elif isinstance(issuer_public_key, (ed25519.Ed25519PublicKey, ed448.Ed448PublicKey)):
            issuer_public_key.verify(certificate.signature, certificate.tbs_certificate_bytes)
        else:
            raise ValueError("unsupported issuer key")
    except (InvalidSignature, TypeError, ValueError):
        raise ControllerRequestError("certificate_chain_invalid", 401) from None


def load_ca_certificate(ca_certificate_pem: bytes | str) -> x509.Certificate:
    encoded = ca_certificate_pem.encode("ascii") if isinstance(ca_certificate_pem, str) else ca_certificate_pem
    try:
        certificate = x509.load_pem_x509_certificate(encoded)
        constraints = certificate.extensions.get_extension_for_class(x509.BasicConstraints).value
    except (ValueError, TypeError, x509.ExtensionNotFound):
        raise ValueError("CA certificate is invalid") from None
    if not constraints.ca:
        raise ValueError("CA certificate is not a CA")
    try:
        usage = certificate.extensions.get_extension_for_class(x509.KeyUsage).value
    except x509.ExtensionNotFound:
        usage = None
    if usage is not None and not usage.key_cert_sign:
        raise ValueError("CA certificate cannot sign certificates")
    return certificate


def validate_agent_certificate(
    certificate_pem: bytes | str,
    ca_certificate_pem: bytes | str,
    *,
    now: datetime | None = None,
) -> tuple[x509.Certificate, str]:
    """Re-validate a proxy-forwarded leaf against the pinned direct agent CA."""
    encoded = certificate_pem.encode("ascii") if isinstance(certificate_pem, str) else certificate_pem
    if len(encoded) > 32 * 1024 or b"PRIVATE KEY" in encoded:
        raise ControllerRequestError("client_certificate_invalid", 401)
    try:
        certificate = x509.load_pem_x509_certificate(encoded)
    except (ValueError, TypeError):
        raise ControllerRequestError("client_certificate_invalid", 401) from None
    ca_certificate = load_ca_certificate(ca_certificate_pem)
    if certificate.issuer != ca_certificate.subject:
        raise ControllerRequestError("certificate_chain_invalid", 401)
    _verify_signature(certificate, ca_certificate.public_key())
    current = (now or datetime.now(UTC)).astimezone(UTC)
    if not (_utc(certificate.not_valid_before) <= current < _utc(certificate.not_valid_after)):
        raise ControllerRequestError("client_certificate_expired", 401)
    if (
        _utc(certificate.not_valid_before) < _utc(ca_certificate.not_valid_before)
        or _utc(certificate.not_valid_after) > _utc(ca_certificate.not_valid_after)
    ):
        raise ControllerRequestError("certificate_chain_invalid", 401)
    try:
        constraints = certificate.extensions.get_extension_for_class(x509.BasicConstraints).value
        usages = certificate.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
        key_usage = certificate.extensions.get_extension_for_class(x509.KeyUsage).value
        san = certificate.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    except x509.ExtensionNotFound:
        raise ControllerRequestError("client_certificate_invalid", 401) from None
    if (
        constraints.ca
        or ExtendedKeyUsageOID.CLIENT_AUTH not in usages
        or not key_usage.digital_signature
        or key_usage.key_cert_sign
    ):
        raise ControllerRequestError("client_certificate_invalid", 401)
    common_names = certificate.subject.get_attributes_for_oid(NameOID.COMMON_NAME)
    if len(common_names) != 1:
        raise ControllerRequestError("client_certificate_invalid", 401)
    try:
        agent_id = require_agent_id(common_names[0].value)
    except ValueError:
        raise ControllerRequestError("client_certificate_invalid", 401) from None
    if list(san) != [x509.UniformResourceIdentifier(agent_identity_uri(agent_id))]:
        raise ControllerRequestError("client_certificate_invalid", 401)
    return certificate, agent_id


class CertificateAuthority:
    """In-process issuer backed by a protected PEM CA key and certificate."""

    def __init__(
        self,
        ca_certificate_pem: bytes | str,
        ca_private_key_pem: bytes | str,
        *,
        private_key_password: bytes | None = None,
        ca_chain_pem: str = "",
        lifetime: timedelta = timedelta(days=30),
        backdate: timedelta = timedelta(seconds=30),
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if not timedelta(minutes=5) <= lifetime <= timedelta(days=90):
            raise ValueError("certificate lifetime is outside policy")
        if not timedelta(0) <= backdate <= timedelta(minutes=5):
            raise ValueError("certificate backdate is outside policy")
        if "PRIVATE KEY" in ca_chain_pem:
            raise ValueError("CA chain contains a private key")
        self._ca_certificate = load_ca_certificate(ca_certificate_pem)
        key_bytes = ca_private_key_pem.encode("ascii") if isinstance(ca_private_key_pem, str) else ca_private_key_pem
        try:
            self._ca_private_key = serialization.load_pem_private_key(
                key_bytes,
                password=private_key_password,
            )
        except (ValueError, TypeError):
            raise ValueError("CA private key is invalid") from None
        try:
            _validate_public_key(self._ca_certificate.public_key())
        except ControllerRequestError:
            raise ValueError("CA key algorithm or size is outside policy") from None
        if not hmac.compare_digest(
            _public_key_digest(self._ca_certificate.public_key()),
            _public_key_digest(self._ca_private_key.public_key()),
        ):
            raise ValueError("CA certificate and private key do not match")
        current = clock().astimezone(UTC)
        if not (_utc(self._ca_certificate.not_valid_before) <= current < _utc(self._ca_certificate.not_valid_after)):
            raise ValueError("CA certificate is not current")
        issuer_pem = self._ca_certificate.public_bytes(serialization.Encoding.PEM).decode("ascii")
        additional_chain = ""
        if ca_chain_pem.strip():
            try:
                chain = x509.load_pem_x509_certificates(ca_chain_pem.encode("ascii"))
            except (UnicodeEncodeError, ValueError):
                raise ValueError("CA chain is invalid") from None
            previous = self._ca_certificate
            seen = {previous.fingerprint(hashes.SHA256())}
            canonical_chain: list[str] = []
            for certificate in chain:
                fingerprint = certificate.fingerprint(hashes.SHA256())
                if fingerprint in seen or previous.issuer != certificate.subject:
                    raise ValueError("CA chain is invalid")
                try:
                    _verify_signature(previous, certificate.public_key())
                except ControllerRequestError:
                    raise ValueError("CA chain is invalid") from None
                seen.add(fingerprint)
                canonical_chain.append(
                    certificate.public_bytes(serialization.Encoding.PEM).decode("ascii")
                )
                previous = certificate
            if not canonical_chain:
                raise ValueError("CA chain is invalid")
            additional_chain = "".join(canonical_chain)
        self._ca_chain_pem = issuer_pem + additional_chain
        self._lifetime = lifetime
        self._backdate = backdate
        self._clock = clock

    @classmethod
    def from_files(
        cls,
        certificate_path: str | Path,
        private_key_path: str | Path,
        **kwargs: object,
    ) -> "CertificateAuthority":
        return cls(Path(certificate_path).read_bytes(), Path(private_key_path).read_bytes(), **kwargs)

    def __call__(self, verified_csr: VerifiedCSR) -> CertificateIssuance:
        if type(verified_csr) is not VerifiedCSR or not verified_csr.csr_der:
            raise ControllerRequestError("verified_csr_payload_missing", 500)
        try:
            csr = x509.load_der_x509_csr(verified_csr.csr_der)
        except ValueError:
            raise ControllerRequestError("verified_csr_payload_invalid", 500) from None
        repeated = verify_agent_csr(csr.public_bytes(serialization.Encoding.PEM).decode("ascii"))
        if (
            repeated.agent_id != verified_csr.agent_id
            or not hmac.compare_digest(repeated.public_key_sha256, verified_csr.public_key_sha256)
            or not hmac.compare_digest(repeated.csr_sha256, verified_csr.csr_sha256)
        ):
            raise ControllerRequestError("verified_csr_payload_invalid", 500)
        current = self._clock().astimezone(UTC)
        not_before = current - self._backdate
        not_after = min(current + self._lifetime, _utc(self._ca_certificate.not_valid_after))
        if not_after <= current:
            raise ControllerRequestError("ca_certificate_expired", 500)
        public_key = csr.public_key()
        key_usage = x509.KeyUsage(
            digital_signature=True,
            content_commitment=False,
            key_encipherment=isinstance(public_key, rsa.RSAPublicKey),
            data_encipherment=False,
            key_agreement=False,
            key_cert_sign=False,
            crl_sign=False,
            encipher_only=False,
            decipher_only=False,
        )
        builder = (
            x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, verified_csr.agent_id)]))
            .issuer_name(self._ca_certificate.subject)
            .public_key(public_key)
            .serial_number(x509.random_serial_number())
            .not_valid_before(not_before)
            .not_valid_after(not_after)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(key_usage, critical=True)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CLIENT_AUTH]), critical=True)
            .add_extension(
                x509.SubjectAlternativeName([
                    x509.UniformResourceIdentifier(agent_identity_uri(verified_csr.agent_id))
                ]),
                critical=True,
            )
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(public_key), critical=False)
            .add_extension(
                x509.AuthorityKeyIdentifier.from_issuer_public_key(self._ca_certificate.public_key()),
                critical=False,
            )
        )
        signing_algorithm = None if isinstance(
            self._ca_private_key,
            (ed25519.Ed25519PrivateKey, ed448.Ed448PrivateKey),
        ) else hashes.SHA256()
        certificate = builder.sign(self._ca_private_key, signing_algorithm)
        certificate_pem = certificate.public_bytes(serialization.Encoding.PEM).decode("ascii")
        return CertificateIssuance(
            serial=format(certificate.serial_number, "X"),
            fingerprint_sha256=certificate.fingerprint(hashes.SHA256()).hex(),
            public_key_sha256=_public_key_digest(certificate.public_key()),
            not_before=_utc(certificate.not_valid_before),
            not_after=_utc(certificate.not_valid_after),
            certificate_pem=certificate_pem,
            ca_chain_pem=self._ca_chain_pem,
        )
