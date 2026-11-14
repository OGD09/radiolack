"""Tests for remote access's state, codes, devices, addresses and
certificates (remote.py), without network: TLS handshakes run in memory."""

import dataclasses
import datetime
import hashlib
import json
import ssl
import unittest
from unittest import mock

from cryptography import x509
from cryptography.hazmat.primitives import serialization

from radiolack import remote
from radiolack.remote import (CODE_ALPHABET, DEFAULT_PORT, DEVICE_EXPIRY_S, LOCAL_DOMAINS, MAX_DEVICES,
                              MAX_WRONG_CODES, PRIVATE_NETWORKS, RemoteAccess, create_authority, device_label,
                              format_code, host_name, is_private, issue_server_certificate, new_code,
                              private_addresses, remote_urls, server_context)

HOST = "radiolack-test"
IPHONE = ("Mozilla/5.0 (iPhone; CPU iPhone OS 18_5 like Mac OS X) AppleWebKit/605.1.15 "
          "(KHTML, like Gecko) Version/18.5 Mobile/15E148 Safari/604.1")
ANDROID = ("Mozilla/5.0 (Linux; Android 15; Pixel 9) AppleWebKit/537.36 (KHTML, like Gecko) "
           "Chrome/141.0.0.0 Mobile Safari/537.36")


class Clock:
    """A clock that moves only when told to."""

    def __init__(self, now: float = 1_800_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def handshake(server: ssl.SSLContext, client: ssl.SSLContext, server_hostname: str) -> None:
    """A TLS handshake in memory: raises ssl.SSLCertVerificationError when the
    client does not trust the server for ``server_hostname``."""
    client_in, client_out, server_in, server_out = (ssl.MemoryBIO() for _ in range(4))
    client_side = client.wrap_bio(client_in, client_out, server_hostname=server_hostname)
    server_side = server.wrap_bio(server_in, server_out, server_side=True)
    finished: set[str] = set()
    for _ in range(10):
        for name, side, outgoing, incoming in (("client", client_side, client_out, server_in),
                                               ("server", server_side, server_out, client_in)):
            try:
                side.do_handshake()
                finished.add(name)
            except ssl.SSLWantReadError:
                pass
            incoming.write(outgoing.read())
        if finished == {"client", "server"}:
            return
    raise AssertionError("the handshake did not end")


def validity(certificate: x509.Certificate) -> datetime.timedelta:
    if hasattr(certificate, "not_valid_after_utc"):     # cryptography 42 and later
        return certificate.not_valid_after_utc - certificate.not_valid_before_utc
    return certificate.not_valid_after - certificate.not_valid_before


class CodeTest(unittest.TestCase):
    def test_a_code_has_eight_characters_that_cannot_be_misread(self):
        codes = {new_code() for _ in range(50)}
        self.assertEqual(len(codes), 50)
        for code in codes:
            self.assertEqual(len(code), 8)
            self.assertLessEqual(set(code), set(CODE_ALPHABET))
        self.assertFalse(set("01IO") & set(CODE_ALPHABET))
        self.assertEqual(format_code("ABCD2345"), "ABCD-2345")


class RemoteAccessTest(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.access = RemoteAccess(clock=self.clock)
        self.changes = 0

        def changed():
            self.changes += 1

        self.access.on_change = changed

    def prepare(self) -> str:
        """Make the code and the authority, as the first turn-on does: returns the code."""
        self.access.prepare(HOST)
        return self.access.stored()["code"]

    def test_a_new_access_has_neither_code_nor_authority(self):
        self.assertEqual(self.access.describe(), {
            "on": False, "port": DEFAULT_PORT, "urls": [], "code": None, "blocked": False, "devices": [],
            "fingerprint": None, "problem": ""})
        self.assertEqual(self.access.stored(), {
            "port": DEFAULT_PORT, "code": None, "wrong_codes": 0, "devices": [], "authority": None})

    def test_the_first_turn_on_makes_the_code_and_the_authority_once(self):
        code = self.prepare()
        authority = self.access.authority
        self.assertEqual(self.changes, 1)
        self.assertEqual(self.access.prepare(HOST), authority)
        self.assertEqual(self.access.stored()["code"], code)
        self.assertEqual(self.changes, 1)
        # The code shows only while on.
        self.assertIsNone(self.access.describe()["code"])
        self.access.started(["https://radiolack-test.local:8443/"])
        self.assertEqual(self.access.describe()["code"], format_code(code))
        self.assertEqual(self.access.describe()["urls"], ["https://radiolack-test.local:8443/"])
        self.access.stopped()
        self.assertEqual((self.access.describe()["code"], self.access.describe()["urls"]), (None, []))
        self.access.stopped("Port 8443 is in use by another program: choose another port.")
        self.assertEqual(self.access.describe()["problem"],
                         "Port 8443 is in use by another program: choose another port.")

    def test_on_while_listening_nowhere_and_unchanged_states_not_sent_again(self):
        code = self.prepare()
        self.access.started(["https://192.168.1.10:8443/"])
        changes = self.changes
        self.access.started(["https://192.168.1.10:8443/"])
        self.assertEqual(self.changes, changes)
        # This computer lost its addresses: remote access stays on, and says why.
        problem = "This computer has no address on a local network for now."
        for _ in range(2):      # at each check, the same: sent once
            self.access.started([], problem)
        self.assertEqual(self.changes, changes + 1)
        state = self.access.describe()
        self.assertEqual((state["on"], state["urls"], state["code"], state["problem"]),
                         (True, [], format_code(code), problem))
        self.access.started(["https://10.0.0.2:8443/"])
        self.assertEqual((self.changes, self.access.describe()["problem"]), (changes + 2, ""))

    def test_the_code_ignores_case_spaces_and_dashes(self):
        code = self.prepare()
        for given in (code, code.lower(), format_code(code), f" {code[:4]} {code[4:]} ".lower()):
            with self.subTest(given=given):
                self.assertTrue(self.access.check_code(given))
        self.assertFalse(self.access.blocked)

    def test_wrong_codes_block_the_code_until_a_new_one_is_made(self):
        code = self.prepare()
        paired = self.access.session(self.access.pair("iPhone · Safari"))
        wrong = "".join(next(c for c in CODE_ALPHABET if c != letter) for letter in code)
        for count in range(1, MAX_WRONG_CODES + 1):
            self.assertFalse(self.access.check_code(wrong if count % 2 else None))
            self.assertEqual(self.access.blocked, count == MAX_WRONG_CODES)
        self.assertFalse(self.access.check_code(code))
        self.assertTrue(self.access.describe()["blocked"])
        self.assertEqual(self.access.stored()["wrong_codes"], MAX_WRONG_CODES)
        # A new code works at once, and every paired device must enter it.
        self.assertEqual(self.access.renew_code(), [paired.id])
        self.assertEqual(self.access.describe()["devices"], [])
        self.assertFalse(self.access.blocked)
        renewed = self.access.stored()["code"]
        self.assertNotEqual(renewed, code)
        self.assertTrue(self.access.check_code(renewed))

    def test_a_paired_device_holds_a_session_until_forgotten(self):
        self.prepare()
        token = self.access.pair("iPhone · Safari")
        device = self.access.session(token)
        self.assertEqual((device.label, device.paired, device.seen), ("iPhone · Safari", self.clock.now,
                                                                      self.clock.now))
        self.assertNotIn(token, json.dumps(self.access.stored()))      # only its hash is kept
        for other in (None, "", token[:-1], token + "A", "x" * 1000):
            with self.subTest(token=other):
                self.assertIsNone(self.access.session(other))
        self.assertEqual([d["id"] for d in self.access.describe()["devices"]], [device.id])
        self.access.forget(device.id)
        self.assertIsNone(self.access.session(token))
        with self.assertRaises(ValueError):
            self.access.forget(device.id)
        with self.assertRaises(ValueError):
            self.access.forget(None)

    def test_a_device_keeps_its_preferences_until_forgotten(self):
        first, second = (self.access.session(self.access.pair(label)) for label in ("iPhone · Safari", "Device"))
        self.assertIsNone(self.access.preferences(first.id))
        values = {"palette": "gray", "volume": 10}
        self.access.set_preferences(first.id, values)
        values["volume"] = 20                                   # the access keeps a copy,
        self.access.preferences(first.id)["volume"] = 30        # and gives one
        self.assertEqual(self.access.preferences(first.id), {"palette": "gray", "volume": 10})
        self.assertIsNone(self.access.preferences(second.id))
        self.assertEqual(self.changes, 2)       # the pairings: the engine saves the preferences itself
        self.access.forget(first.id)
        self.assertIsNone(self.access.preferences(first.id))
        with self.assertRaisesRegex(ValueError, "no longer paired"):
            self.access.set_preferences(first.id, {"volume": 1})

    def test_a_new_code_or_ninety_days_unused_forget_the_preferences_too(self):
        device = self.access.session(self.access.pair("Device"))
        self.access.set_preferences(device.id, {"volume": 10})
        self.access.renew_code()
        self.assertIsNone(self.access.preferences(device.id))
        token = self.access.pair("Device")
        device = self.access.session(token)
        self.access.set_preferences(device.id, {"volume": 10})
        self.clock.now += DEVICE_EXPIRY_S + 1
        self.assertIsNone(self.access.session(token))
        self.assertIsNone(self.access.preferences(device.id))

    def test_at_most_twenty_devices(self):
        for _ in range(MAX_DEVICES):
            self.access.pair("Device")
        with self.assertRaisesRegex(ValueError, "too many devices"):
            self.access.pair("Device")
        self.assertEqual(len(self.access.describe()["devices"]), MAX_DEVICES)

    def test_a_device_unused_for_ninety_days_is_forgotten(self):
        token = self.access.pair("Device")
        self.clock.now += DEVICE_EXPIRY_S
        self.assertIsNotNone(self.access.session(token))
        self.clock.now += 1
        self.assertIsNone(self.access.session(token))
        self.assertEqual(self.access.describe()["devices"], [])

    def test_a_connected_device_is_not_forgotten(self):
        device = self.access.session(self.access.pair("Device"))
        self.access.connected(device)
        self.clock.now += DEVICE_EXPIRY_S + 1
        self.access.prepare(HOST)
        [described] = self.access.describe()["devices"]
        self.assertTrue(described["connected"])
        self.access.disconnected(device)
        self.assertFalse(self.access.describe()["devices"][0]["connected"])

    def test_the_last_use_is_noted_to_the_hour(self):
        device = self.access.session(self.access.pair("Device"))
        paired = self.clock.now
        self.clock.now += 1800
        self.access.connected(device)
        self.assertEqual(device.seen, paired)
        self.clock.now += 1800
        self.access.disconnected(device)
        self.assertEqual(device.seen, paired + 3600)
        self.assertEqual(self.access.describe()["devices"][0]["seen"], paired + 3600)

    def test_the_port_changes_only_while_off(self):
        for port in (1023, 65536, "9000", 9000.0, True, None):
            with self.subTest(port=port), self.assertRaises(ValueError):
                self.access.set_port(port)
        self.access.set_port(9000)
        self.assertEqual(self.access.describe()["port"], 9000)
        self.access.started([])
        with self.assertRaisesRegex(ValueError, "only while remote access is off"):
            self.access.set_port(9001)

    def test_what_is_stored_is_restored(self):
        self.access.set_port(9443)
        code = self.prepare()
        self.access.check_code("WRONG")
        tokens = [self.access.pair(label) for label in ("iPhone · Safari", "Android device · Chrome")]
        saved = json.loads(json.dumps(self.access.stored()))
        self.assertNotIn("on", saved)
        restored = RemoteAccess(clock=self.clock)
        restored.restore(saved)
        self.assertEqual(restored.stored(), saved)
        self.assertEqual(restored.describe(), self.access.describe())
        self.assertEqual([restored.session(token).label for token in tokens],
                         ["iPhone · Safari", "Android device · Chrome"])
        self.assertTrue(restored.check_code(code))

    def test_a_devices_preferences_are_stored_with_it(self):
        devices = [self.access.session(self.access.pair(label)) for label in ("iPhone · Safari", "Device")]
        self.access.set_preferences(devices[0].id, {"palette": "gray", "volume": 10})
        saved = json.loads(json.dumps(self.access.stored()))
        self.assertEqual(saved["devices"][0]["preferences"], {"palette": "gray", "volume": 10})
        self.assertNotIn("preferences", saved["devices"][1])    # it gave none: as in older files
        restored = RemoteAccess(clock=self.clock)
        restored.restore(saved)
        self.assertEqual(restored.stored(), saved)
        self.assertEqual([restored.preferences(device.id) for device in devices],
                         [{"palette": "gray", "volume": 10}, None])

    def test_restoring_leaves_out_preferences_that_are_not_valid(self):
        others = [{**self.saved_device(), "id": f"{index:08x}", "session": f"{index:064x}"} for index in (1, 2, 3)]
        with self.assertLogs("radiolack.preferences", "WARNING") as logs:
            self.access.restore({"devices": [
                {**self.saved_device(), "preferences": {"volume": 10, "palette": ["gray"], "": 1}},
                {**others[0], "preferences": "gray"}, {**others[1], "preferences": None}, others[2]]})
        self.assertEqual(len(logs.output), 2)
        self.assertEqual(self.access.preferences(self.saved_device()["id"]), {"volume": 10})
        self.assertEqual([self.access.preferences(device["id"]) for device in others], [None, None, None])
        self.assertEqual(len(self.access.describe()["devices"]), 4)     # every device stays paired

    def test_restoring_leaves_out_what_is_not_valid(self):
        self.access.restore({
            "port": 80, "code": "abcdefgh", "wrong_codes": 2,
            "devices": [{"id": "1234abcd"}, "device", {**self.saved_device(), "label": ""}, self.saved_device()],
            "authority": {"root": "-----BEGIN CERTIFICATE-----\nnot base64\n",
                          "certificate": "-----BEGIN CERTIFICATE-----", "key": "-----BEGIN PRIVATE KEY-----"}})
        self.assertEqual(self.access.stored(), {
            "port": DEFAULT_PORT, "code": None, "wrong_codes": 0, "devices": [self.saved_device()],
            "authority": None})
        for saved in ({}, {"devices": "none"}, {"port": True}):
            with self.subTest(saved=saved):
                RemoteAccess().restore(saved)

    def test_an_authority_whose_key_does_not_match_is_left_out(self):
        authority, other = create_authority(HOST), create_authority(HOST)
        for broken in ({"key": other.key}, {"certificate": other.certificate},
                       {"key": "-----BEGIN PRIVATE KEY-----\nAAAA\n-----END PRIVATE KEY-----\n"}):
            with self.subTest(broken=list(broken)):
                access = RemoteAccess()
                access.restore({"authority": {**dataclasses.asdict(authority), **broken}})
                self.assertIsNone(access.authority)
        access = RemoteAccess()
        access.restore({"authority": dataclasses.asdict(authority)})
        self.assertEqual(access.authority, authority)

    def test_a_code_without_a_readable_count_of_wrong_codes_stays_blocked(self):
        for wrong, blocked in ((3, False), (None, True), (-1, True), ("2", True), (True, True), (99, True)):
            with self.subTest(wrong_codes=wrong):
                access = RemoteAccess()
                access.restore({"code": "ABCD2345", "wrong_codes": wrong})
                self.assertEqual(access.blocked, blocked)

    @staticmethod
    def saved_device() -> dict:
        return {"id": "1234abcd", "label": "iPhone · Safari", "session": "0" * 64, "paired": 1, "seen": 2}


class AddressTest(unittest.TestCase):
    def test_private_addresses(self):
        for address in ("192.168.1.20", "10.1.2.3", "172.16.0.1", "172.31.255.254", "100.64.0.1",
                        "169.254.10.10", "127.0.0.1", "::ffff:192.168.1.20"):
            with self.subTest(address=address):
                self.assertTrue(is_private(address))
        for address in ("8.8.8.8", "172.32.0.1", "100.128.0.1", "fe80::1", "fd00::1", "::1", "", "radiolack",
                        "192.168.1.300"):
            with self.subTest(address=address):
                self.assertFalse(is_private(address))

    def test_home_networks_come_first_and_loopback_never(self):
        found = ["10.8.0.6", "127.0.0.1", "8.8.8.8", "192.168.1.20", "100.64.0.7", "172.17.0.1"]
        with mock.patch.object(remote, "_interface_addresses", return_value=found), \
                mock.patch.object(remote, "_default_route_address", return_value="10.8.0.6"):
            self.assertEqual(private_addresses(), ["192.168.1.20", "172.17.0.1", "10.8.0.6", "100.64.0.7"])
        with mock.patch.object(remote, "_interface_addresses", return_value=[]), \
                mock.patch.object(remote, "_default_route_address", return_value=None):
            self.assertEqual(private_addresses(), [])

    def test_this_computers_addresses_are_private(self):
        for address in private_addresses():     # reads the interfaces, sends nothing
            with self.subTest(address=address):
                self.assertTrue(is_private(address))
                self.assertFalse(address.startswith("127."))

    def test_urls_give_the_addresses_before_the_name(self):
        # The name also leads to the computer's IPv6 addresses, if any, where
        # remote access does not listen: an iPhone then fails to open it.
        self.assertEqual(remote_urls(HOST, ["192.168.1.20", "10.0.0.2"], 9443), [
            "https://192.168.1.20:9443/", "https://10.0.0.2:9443/", "https://radiolack-test.local:9443/"])
        self.assertEqual(remote_urls("", ["192.168.1.20"], 8443), ["https://192.168.1.20:8443/"])

    def test_the_host_name_is_one_dns_label_or_nothing(self):
        for name, label in (("Roamer.example.org", "roamer"), ("radio-1", "radio-1"), ("radio_1", ""),
                            ("-radio", ""), ("", ""), ("x" * 64, "")):
            with self.subTest(name=name), mock.patch("socket.gethostname", return_value=name):
                self.assertEqual(host_name(), label)


class DeviceLabelTest(unittest.TestCase):
    def test_labels_name_the_device_and_the_browser_in_known_words_only(self):
        for agent, label in (
                (IPHONE, "iPhone · Safari"),
                (IPHONE.replace("iPhone;", "iPad;").replace("iPhone OS", "OS"), "iPad · Safari"),
                (ANDROID, "Android device · Chrome"),
                ("Mozilla/5.0 (X11; Linux x86_64; rv:140.0) Gecko/20100101 Firefox/140.0", "Linux computer · Firefox"),
                ("Mozilla/5.0 (iPhone; CPU iPhone OS 18_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) "
                 "CriOS/141.0.0.0 Mobile/15E148 Safari/604.1", "iPhone · Chrome"),
                ("<script>alert(1)</script>", "Device"),
                ("", "Device")):
            with self.subTest(agent=agent):
                self.assertEqual(device_label(agent), label)


class CertificateTest(unittest.TestCase):
    """The local authority and the server certificates it signs, checked as
    a device does, with OpenSSL."""

    @classmethod
    def setUpClass(cls):
        cls.authority = create_authority(HOST)
        cls.trust = ssl.create_default_context(cadata=cls.authority.root)

    def serve(self, names: list[str], addresses: list[str]) -> ssl.SSLContext:
        return server_context(*issue_server_certificate(self.authority, names, addresses))

    def test_the_server_certificate_verifies_for_its_names_and_addresses(self):
        server = self.serve([f"{HOST}.local"], ["192.168.1.20", "10.0.0.2"])
        for name in (f"{HOST}.local", "192.168.1.20", "10.0.0.2"):
            with self.subTest(name=name):
                handshake(server, self.trust, name)
        for name in ("other.local", "192.168.1.21"):
            with self.subTest(name=name), self.assertRaises(ssl.SSLCertVerificationError):
                handshake(server, self.trust, name)
        handshake(self.serve([], ["192.168.1.20"]), self.trust, "192.168.1.20")     # without a name
        handshake(self.serve(["radio.home.arpa", "radio.internal"], []), self.trust, "radio.internal")

    def test_the_authority_vouches_for_local_names_and_addresses_only(self):
        for names, addresses, name in ((["bank.example"], [], "bank.example"), ([], ["8.8.8.8"], "8.8.8.8"),
                                       (["bank.example", f"{HOST}.local"], [], f"{HOST}.local")):
            with self.subTest(names=names, addresses=addresses):
                with self.assertRaises(ssl.SSLCertVerificationError) as caught:
                    handshake(self.serve(names, addresses), self.trust, name)
                self.assertIn("permitted subtree violation", caught.exception.verify_message)

    def test_the_root_signs_one_constrained_intermediate_and_keeps_no_key(self):
        root = x509.load_pem_x509_certificate(self.authority.root.encode("ascii"))
        intermediate = x509.load_pem_x509_certificate(self.authority.certificate.encode("ascii"))
        key = serialization.load_pem_private_key(self.authority.key.encode("ascii"), password=None)
        self.assertEqual(root.extensions.get_extension_for_class(x509.BasicConstraints).value.path_length, 1)
        self.assertEqual(intermediate.issuer, root.subject)
        self.assertEqual(key.public_key().public_numbers(), intermediate.public_key().public_numbers())
        constraints = intermediate.extensions.get_extension_for_class(x509.NameConstraints)
        self.assertTrue(constraints.critical)
        self.assertEqual(set(constraints.value.permitted_subtrees),
                         {x509.IPAddress(network) for network in PRIVATE_NETWORKS}
                         | {x509.DNSName(domain) for domain in LOCAL_DOMAINS})
        self.assertIsNone(constraints.value.excluded_subtrees)
        self.assertEqual(list(intermediate.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value),
                         [x509.oid.ExtendedKeyUsageOID.SERVER_AUTH])
        self.assertEqual(
            intermediate.extensions.get_extension_for_class(x509.BasicConstraints).value.path_length, 0)
        self.assertEqual(self.authority.fingerprint, " ".join(
            f"{byte:02X}" for byte in hashlib.sha256(self.authority.root_der()).digest()))

    def test_a_server_certificate_lasts_within_what_browsers_accept(self):
        chain, _key = issue_server_certificate(self.authority, [f"{HOST}.local"], ["192.168.1.20"])
        certificate = x509.load_pem_x509_certificate(chain)
        self.assertLessEqual(validity(certificate), datetime.timedelta(days=398))
        self.assertFalse(certificate.extensions.get_extension_for_class(x509.BasicConstraints).value.ca)
        self.assertEqual(list(certificate.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value),
                         [x509.oid.ExtendedKeyUsageOID.SERVER_AUTH])


if __name__ == "__main__":
    unittest.main()
