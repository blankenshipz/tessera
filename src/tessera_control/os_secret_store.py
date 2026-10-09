"""OS custody only. No discovery, environment/file fallback, logging or HTTP surface."""

import ctypes as C
import re
import sys
from typing import Protocol

from .salad_secrets import SecretBuffer

MAX_SECRET_BYTES = 16384


class SecretStoreError(Exception):
    def __init__(self):
        super().__init__("OS secret store unavailable or access denied.")


def validate_value(value: bytearray):
    # Closed UTF-8 text credential format; no implicit trimming or normalization.
    try:
        if type(value) is not bytearray or not 16 <= len(value) <= MAX_SECRET_BYTES:
            raise ValueError
        text = value.decode("utf-8", errors="strict")
        if any(ord(c) < 32 or ord(c) == 127 for c in text):
            raise ValueError
    except (ValueError, UnicodeError):
        raise SecretStoreError() from None


def account(reference, version):
    if not re.fullmatch(r"credential-ref:[a-f0-9]{32}", reference) or (
        type(version) is not int or not 1 <= version <= 100
    ):
        raise SecretStoreError()
    return f"{reference}:{version}".encode("ascii")


class OSSecretStore(Protocol):
    status: str

    def put(self, reference: str, version: int, value: bytearray): ...
    def exists(self, reference: str, version: int) -> bool: ...
    def delete(self, reference: str, version: int): ...
    def resolve(self, reference: str, version: int) -> SecretBuffer: ...


class UnavailableOSStore:
    status = "unavailable"

    def put(self, *args):
        raise SecretStoreError()

    exists = delete = resolve = put


class WindowsCredentialManager(UnavailableOSStore):
    """Explicit unavailable adapter: no reviewed native binding ships yet."""


class LinuxSecretService(UnavailableOSStore):
    """Explicit unavailable adapter: no installed/verified libsecret binding assumed."""


class MacOSKeychain:
    """Native Security.framework generic passwords, scoped to our service/opaque ID.

    No security(1) argv password, shell, keychain enumeration or credentials probe.
    Loading this adapter does not read any item. Locked/denied stores fail closed.
    """

    status = "macos_keychain"
    service = b"org.tessera.protected-secrets.v1"

    def __init__(self):
        if sys.platform != "darwin":
            raise SecretStoreError()
        try:
            self.lib = C.CDLL("/System/Library/Frameworks/Security.framework/Security")
            self.cf = C.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
            u, p = C.c_uint32, C.c_void_p
            signatures = {
                "SecKeychainAddGenericPassword": [p, u, p, u, p, u, p, C.POINTER(p)],
                "SecKeychainFindGenericPassword": [
                    p,
                    u,
                    p,
                    u,
                    p,
                    C.POINTER(u),
                    C.POINTER(p),
                    C.POINTER(p),
                ],
                "SecKeychainItemFreeContent": [p, p],
                "SecKeychainItemDelete": [p],
                "SecKeychainSetUserInteractionAllowed": [C.c_ubyte],
            }
            for name, args in signatures.items():
                method = getattr(self.lib, name)
                method.argtypes, method.restype = args, C.c_int32
            self.cf.CFRelease.argtypes, self.cf.CFRelease.restype = [p], None
            if self.lib.SecKeychainSetUserInteractionAllowed(False):
                raise SecretStoreError()
        except (OSError, AttributeError):
            raise SecretStoreError() from None

    def put(self, reference, version, value):
        validate_value(value)
        name = account(reference, version)
        data = (C.c_char * len(value)).from_buffer(value)
        # Add, never update: duplicate version is an error, not a silent overwrite.
        code = self.lib.SecKeychainAddGenericPassword(
            None, len(self.service), self.service, len(name), name, len(value), data, None
        )
        if code:
            raise SecretStoreError()

    def _find(self, reference, version, *, read=False):
        name = account(reference, version)
        size, data, item = C.c_uint32(), C.c_void_p(), C.c_void_p()
        code = self.lib.SecKeychainFindGenericPassword(
            None,
            len(self.service),
            self.service,
            len(name),
            name,
            C.byref(size) if read else None,
            C.byref(data) if read else None,
            C.byref(item),
        )
        if code == -25300:  # errSecItemNotFound
            return None, None
        if code:
            raise SecretStoreError()
        value = None
        try:
            if read:
                if not 16 <= size.value <= MAX_SECRET_BYTES:
                    raise SecretStoreError()
                value = bytearray(size.value)
                C.memmove((C.c_char * size.value).from_buffer(value), data, size.value)
            return item, value
        except BaseException:
            self.cf.CFRelease(item)
            raise
        finally:
            if data.value:
                C.memset(data, 0, size.value)
                self.lib.SecKeychainItemFreeContent(None, data)

    def exists(self, reference, version):
        item, _ = self._find(reference, version)
        if item is None:
            return False
        self.cf.CFRelease(item)
        return True

    def delete(self, reference, version):
        item, _ = self._find(reference, version)
        if item is not None:
            try:
                if self.lib.SecKeychainItemDelete(item):
                    raise SecretStoreError()
            finally:
                self.cf.CFRelease(item)

    def resolve(self, reference, version):
        item, value = self._find(reference, version, read=True)
        if item is None:
            raise SecretStoreError()
        self.cf.CFRelease(item)
        return SecretBuffer(value)


def platform_store():
    if sys.platform == "darwin":
        return MacOSKeychain()
    if sys.platform == "win32":
        return WindowsCredentialManager()
    return LinuxSecretService()


class DevelopmentMemoryStore:
    """Explicit injected test fake. Never selected by configuration or platform detection."""

    status = "development_memory_NON_PRODUCTION"

    def __init__(self):
        self._items = {}

    def put(self, reference, version, value):
        validate_value(value)
        key = account(reference, version)
        if key in self._items:
            raise SecretStoreError()
        self._items[key] = bytearray(value)

    def exists(self, reference, version):
        return account(reference, version) in self._items

    def delete(self, reference, version):
        value = self._items.pop(account(reference, version), None)
        if value is not None:
            value[:] = b"\0" * len(value)

    def resolve(self, reference, version):
        value = self._items.get(account(reference, version))
        if value is None:
            raise SecretStoreError()
        return SecretBuffer(bytearray(value))
