import importlib.util
import pathlib
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class BrokerTests(unittest.TestCase):
    def test_redacts_common_secrets(self):
        broker = load("broker", ROOT / "core" / "broker.py")
        text = "password=hunter2 token=abc123 Authorization: Bearer eyJabc.def.ghi"
        out = broker.redact(text)
        self.assertNotIn("hunter2", out)
        self.assertNotIn("abc123", out)

    def test_kubectl_mutation_verbs_absent(self):
        text = (ROOT / "core" / "broker.py").read_text()
        self.assertIn('allowed = {"get", "describe", "logs", "top", "version"}', text)
        self.assertNotIn('"delete",', text)
        self.assertNotIn('"patch",', text)


class RunnerTests(unittest.TestCase):
    def test_fault_catalogue_is_bounded(self):
        runner = load("runner", ROOT / "core" / "runner.py")
        self.assertEqual(
            set(runner.FAULTS),
            {"pod-kill", "db-unreachable", "bad-service-selector",
             "bad-configmap", "cpu-stress", "network-policy-deny"},
        )

    def test_state_path_can_be_isolated(self):
        with tempfile.TemporaryDirectory() as d:
            text = (ROOT / "core" / "runner.py").read_text()
            self.assertIn('os.environ.get("BBR_STATE"', text)


if __name__ == "__main__":
    unittest.main()
