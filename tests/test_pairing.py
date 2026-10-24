import unittest

from nexus.proto.pairing import EXPIRES_IN, PairingError, ServerPairing, commit, new_nonce, sas_code

FP_S, FP_C, FP_EVIL = "1" * 64, "2" * 64, "3" * 64


class Clock:
    t = 0.0

    def __call__(self):
        return self.t


def start(fp_c=FP_C, clock=None):
    """Servidor viu fp_c; o cliente (honesto) usa FP_C e viu FP_S."""
    n_c = new_nonce()
    sp = ServerPairing(FP_S, fp_c, clock=clock or Clock())
    ch = sp.on_request(commit(n_c, FP_C, FP_S))
    return sp, ch, n_c


class TestPairing(unittest.TestCase):
    def test_happy_path_codes_match(self):
        sp, ch, n_c = start()
        code = sp.on_reveal(ch["session"], n_c)
        self.assertEqual(code, sas_code(ch["session"], FP_S, FP_C, ch["n_s"], n_c))
        self.assertRegex(code, r"^\d{6}$")
        sp.on_local_confirm(True)
        self.assertFalse(sp.paired)
        sp.on_client_confirm(ch["session"], True)
        self.assertTrue(sp.paired)
        self.assertIsNone(sp.code)

    def test_mitm_swapped_cert_fails_commit(self):
        # MITM apresentou outro cert ao servidor: o commit do cliente honesto não confere
        sp, ch, n_c = start(fp_c=FP_EVIL)
        with self.assertRaises(PairingError):
            sp.on_reveal(ch["session"], n_c)
        self.assertEqual(sp.state, "failed")

    def test_mitm_codes_diverge(self):
        n_c, n_s, s = new_nonce(), new_nonce(), "sess"
        self.assertNotEqual(sas_code(s, FP_S, FP_C, n_s, n_c), sas_code(s, FP_S, FP_EVIL, n_s, n_c))
        self.assertNotEqual(sas_code(s, FP_S, FP_C, n_s, n_c), sas_code(s, FP_EVIL, FP_C, n_s, n_c))

    def test_commit_mismatch(self):
        sp, ch, _ = start()
        with self.assertRaises(PairingError):
            sp.on_reveal(ch["session"], new_nonce())
        with self.assertRaises(PairingError):  # sessão morta após a falha
            sp.on_local_confirm(True)

    def test_bad_nonce_fails_session(self):
        sp, ch, _ = start()
        with self.assertRaises(PairingError):
            sp.on_reveal(ch["session"], "zz")
        self.assertEqual(sp.state, "failed")

    def test_wrong_session(self):
        sp, ch, n_c = start()
        with self.assertRaises(PairingError):
            sp.on_reveal("outra", n_c)

    def test_out_of_order(self):
        sp = ServerPairing(FP_S, FP_C)
        with self.assertRaises(PairingError):
            sp.on_local_confirm(True)
        sp, ch, n_c = start()
        with self.assertRaises(PairingError):
            sp.on_client_confirm(ch["session"], True)  # antes do reveal

    def test_repeated_request(self):
        sp, _, _ = start()
        with self.assertRaises(PairingError):
            sp.on_request("0" * 64)

    def test_expired(self):
        clock = Clock()
        sp, ch, n_c = start(clock=clock)
        sp.on_reveal(ch["session"], n_c)
        clock.t = EXPIRES_IN + 1
        with self.assertRaises(PairingError):
            sp.on_local_confirm(True)
        self.assertFalse(sp.paired)

    def test_expired_before_reveal(self):
        clock = Clock()
        sp, ch, n_c = start(clock=clock)
        clock.t = EXPIRES_IN + 1
        with self.assertRaises(PairingError):
            sp.on_reveal(ch["session"], n_c)

    def test_reused_confirmation(self):
        sp, ch, n_c = start()
        sp.on_reveal(ch["session"], n_c)
        sp.on_client_confirm(ch["session"], True)
        with self.assertRaises(PairingError):
            sp.on_client_confirm(ch["session"], True)
        self.assertFalse(sp.paired)

    def test_confirm_after_done(self):
        sp, ch, n_c = start()
        sp.on_reveal(ch["session"], n_c)
        sp.on_client_confirm(ch["session"], True)
        sp.on_local_confirm(True)
        with self.assertRaises(PairingError):
            sp.on_local_confirm(True)

    def test_reject_either_side(self):
        for side in ("local", "client"):
            with self.subTest(side):
                sp, ch, n_c = start()
                sp.on_reveal(ch["session"], n_c)
                with self.assertRaises(PairingError):
                    if side == "local":
                        sp.on_local_confirm(False)
                    else:
                        sp.on_client_confirm(ch["session"], False)
                self.assertFalse(sp.paired)


if __name__ == "__main__":
    unittest.main()
