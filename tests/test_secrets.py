"""The system secret store: which one each platform gets, and the Linux one's behaviour without a
keyring daemon. keyring itself is faked; nothing touches a real keyring or the Keychain."""
import sys
import types
import unittest
from unittest import mock

from live.sources import secrets
from live.sources.base import SourceError


def fake_keyring(backend):
    """A stand-in `keyring` package whose get_keyring() is `backend`, and its fail backend."""
    fail_mod = types.ModuleType("keyring.backends.fail")

    class Fail:
        pass

    fail_mod.Keyring = Fail
    kr = types.ModuleType("keyring")
    kr.get_keyring = lambda: backend if backend is not None else Fail()
    kr.get_password = lambda service, name: backend.items.get((service, name))
    kr.set_password = lambda service, name, value: backend.items.__setitem__((service, name), value)
    kr.delete_password = lambda service, name: backend.items.pop((service, name))
    return {"keyring": kr, "keyring.backends": types.ModuleType("keyring.backends"), "keyring.backends.fail": fail_mod}


class Backend:
    def __init__(self):
        self.items = {}


class SecretStoreTest(unittest.TestCase):
    def test_each_platform_gets_its_store(self):
        with mock.patch.object(sys, "platform", "darwin"):
            self.assertIsInstance(secrets.KeychainStore("disko.test"), secrets.MacKeychainStore)
        with mock.patch.object(sys, "platform", "linux"):
            self.assertIsInstance(secrets.KeychainStore("disko.test"), secrets.SecretServiceStore)

    def test_secret_service_round_trip(self):
        backend = Backend()
        with mock.patch.dict(sys.modules, fake_keyring(backend)):
            store = secrets.SecretServiceStore("disko.test")
            self.assertIsNone(store.get("token"))
            store.set("token", "s3cret")
            self.assertEqual(backend.items, {("disko.test", "token"): "s3cret"})
            self.assertEqual(store.get("token"), "s3cret")
            store.delete("token")
            store.delete("token")  # deleting what isn't there is fine
            self.assertIsNone(store.get("token"))

    def test_no_keyring_daemon_refuses_to_save_and_writes_nothing(self):
        with mock.patch.dict(sys.modules, fake_keyring(None)):
            store = secrets.SecretServiceStore("disko.test")
            self.assertIsNone(store.get("token"))
            with self.assertRaises(SourceError) as e:
                store.set("token", "s3cret")
            self.assertIn("keyring", str(e.exception))
            self.assertNotIn("s3cret", str(e.exception))

    def test_names_are_checked(self):
        with self.assertRaises(ValueError):
            secrets.SecretServiceStore("bad service!")
        with mock.patch.dict(sys.modules, fake_keyring(Backend())):
            with self.assertRaises(ValueError):
                secrets.SecretServiceStore("disko.test").set("bad name", "x")


if __name__ == "__main__":
    unittest.main()
