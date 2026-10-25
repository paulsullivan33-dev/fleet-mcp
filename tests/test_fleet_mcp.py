"""Tests for fleet_mcp.py -- findings F07 (config validation) and F08
(async, bounded service execution).

Run from the repo root:  python3 -m unittest discover -s tests
"""

import asyncio
import json
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(
    0, os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)

import fleet_mcp

PY = sys.executable


def write_config(directory, text):
    path = os.path.join(directory, "services.json")
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    return path


def config_of(*services):
    return json.dumps({"services": list(services)})


def valid_service(**overrides):
    svc = {
        "name": "echoer",
        "description": "test service",
        "command": [PY, "-c", "print('hi')"],
        "parameters": {},
        "timeout_secs": 30,
    }
    svc.update(overrides)
    return svc


class LoadServicesTests(unittest.TestCase):
    """F07: malformed configs produce validation errors, never crashes."""

    def load(self, text):
        with tempfile.TemporaryDirectory() as d:
            return fleet_mcp.load_services(write_config(d, text))

    def test_valid_config_loads(self):
        services, errors, fatal = self.load(config_of(valid_service()))
        self.assertFalse(fatal)
        self.assertEqual(errors, [])
        self.assertEqual([s["name"] for s in services], ["echoer"])
        self.assertEqual(services[0]["timeout_secs"], 30)

    def test_malformed_roots_are_fatal_not_crashes(self):
        for text in ("[]", '"hello"', "42", "null"):
            with self.subTest(text=text):
                services, errors, fatal = self.load(text)
                self.assertTrue(fatal)
                self.assertEqual(services, [])
                self.assertTrue(errors)

    def test_services_not_a_list_is_fatal(self):
        services, errors, fatal = self.load('{"services": {"a": 1}}')
        self.assertTrue(fatal)
        self.assertTrue(errors)

    def test_invalid_json_and_missing_file_are_fatal(self):
        services, errors, fatal = self.load("{not json")
        self.assertTrue(fatal)
        services, errors, fatal = fleet_mcp.load_services("/nonexistent/x.json")
        self.assertTrue(fatal)
        self.assertTrue(errors)

    def test_unreadable_path_is_fatal_not_a_crash(self):
        # A directory is not a readable config file (IsADirectoryError).
        with tempfile.TemporaryDirectory() as d:
            services, errors, fatal = fleet_mcp.load_services(d)
        self.assertTrue(fatal)
        self.assertTrue(errors)

    def test_non_object_entries_are_skipped_with_errors(self):
        for entry in ("null", '"x"', "[]", "5"):
            with self.subTest(entry=entry):
                services, errors, fatal = self.load('{"services": [%s]}' % entry)
                self.assertFalse(fatal)
                self.assertEqual(services, [])
                self.assertTrue(errors)

    def test_malformed_parameter_shapes_do_not_crash(self):
        bad_params = (
            '"parameters": []',
            '"parameters": {"host": "string"}',
            '"parameters": {"host": {"type": "weird"}}',
        )
        for frag in bad_params:
            with self.subTest(frag=frag):
                svc = valid_service()
                text = config_of(svc).replace('"parameters": {}', frag)
                services, errors, fatal = self.load(text)
                self.assertFalse(fatal)
                self.assertEqual(services, [])
                self.assertTrue(errors)

    def test_bad_timeouts_are_validation_errors(self):
        for bad in ('"abc"', "null", "0", "-5", "3601", "1.5", "true"):
            with self.subTest(bad=bad):
                text = config_of(valid_service()).replace(
                    '"timeout_secs": 30', '"timeout_secs": %s' % bad
                )
                services, errors, fatal = self.load(text)
                self.assertFalse(fatal)
                self.assertEqual(services, [])
                self.assertTrue(
                    any("timeout_secs" in e for e in errors), errors
                )

    def test_string_timeout_and_default_timeout(self):
        text = config_of(valid_service()).replace(
            '"timeout_secs": 30', '"timeout_secs": "45"'
        )
        services, errors, fatal = self.load(text)
        self.assertEqual(errors, [])
        self.assertEqual(services[0]["timeout_secs"], 45)
        svc = valid_service()
        del svc["timeout_secs"]
        services, errors, fatal = self.load(config_of(svc))
        self.assertEqual(services[0]["timeout_secs"], fleet_mcp.DEFAULT_TIMEOUT)

    def test_valid_entries_survive_bad_siblings(self):
        text = config_of(valid_service(), {"bogus": True}, valid_service(name="second"))
        services, errors, fatal = self.load(text)
        self.assertFalse(fatal)
        self.assertEqual([s["name"] for s in services], ["echoer", "second"])
        self.assertTrue(errors)


class RefreshPreservationTests(unittest.TestCase):
    """F07: a failed reload keeps the last valid service set."""

    def setUp(self):
        self._old_path = fleet_mcp.CONFIG_PATH
        self._old_services = fleet_mcp._services
        self._dir = tempfile.TemporaryDirectory()
        fleet_mcp.CONFIG_PATH = os.path.join(self._dir.name, "services.json")
        fleet_mcp._services = []

    def tearDown(self):
        fleet_mcp.CONFIG_PATH = self._old_path
        fleet_mcp._services = self._old_services
        self._dir.cleanup()

    def write(self, text):
        with open(fleet_mcp.CONFIG_PATH, "w", encoding="utf-8") as f:
            f.write(text)

    def test_failed_reload_keeps_previous_services(self):
        self.write(config_of(valid_service()))
        self.assertEqual(fleet_mcp.refresh_services(), "")
        self.assertEqual(len(fleet_mcp._services), 1)

        self.write("{garbage")
        err = fleet_mcp.refresh_services()
        self.assertIn("not valid JSON", err)
        self.assertIn("kept the previous configuration", err)
        self.assertEqual([s["name"] for s in fleet_mcp._services], ["echoer"])

        self.write(config_of(valid_service(name="renamed")))
        self.assertEqual(fleet_mcp.refresh_services(), "")
        self.assertEqual([s["name"] for s in fleet_mcp._services], ["renamed"])


def make_svc(command, timeout=30, parameters=None, name="svc"):
    return {
        "name": name,
        "description": "",
        "command": command,
        "parameters": parameters or {},
        "timeout_secs": timeout,
        "workdir": None,
    }


class RunServiceTests(unittest.IsolatedAsyncioTestCase):
    """F08: async execution -- no loop blocking, bounded output, kills."""

    async def test_simple_command_output(self):
        svc = make_svc([PY, "-c", "print('hello fleet')"])
        self.assertEqual(await fleet_mcp.run_service(svc, {}), "hello fleet")

    async def test_slow_tool_does_not_block_a_fast_one(self):
        slow = make_svc(
            [PY, "-c", "import time; time.sleep(1.2); print('slow done')"],
            name="slow",
        )
        fast = make_svc([PY, "-c", "print('fast done')"], name="fast")
        started = time.monotonic()
        slow_task = asyncio.create_task(fleet_mcp.run_service(slow, {}))
        fast_result = await fleet_mcp.run_service(fast, {})
        fast_elapsed = time.monotonic() - started
        self.assertEqual(fast_result, "fast done")
        # With a blocking subprocess.run in the handler this would be >= 1.2s.
        self.assertLess(fast_elapsed, 1.0)
        self.assertEqual(await slow_task, "slow done")

    async def test_output_flood_is_bounded(self):
        svc = make_svc([PY, "-c", "print('x' * 300000)"])
        started = time.monotonic()
        result = await fleet_mcp.run_service(svc, {})
        self.assertLess(time.monotonic() - started, 10)
        self.assertIn("truncated", result)
        self.assertLessEqual(
            len(result), fleet_mcp.MAX_OUTPUT + 100
        )

    @unittest.skipUnless(os.name == "posix", "process-group kill is POSIX")
    async def test_timeout_kills_the_process(self):
        with tempfile.TemporaryDirectory() as d:
            pidfile = os.path.join(d, "pid")
            code = (
                "import os, time; "
                f"open({pidfile!r}, 'w').write(str(os.getpid())); "
                "time.sleep(60)"
            )
            svc = make_svc([PY, "-c", code], timeout=1)
            result = await fleet_mcp.run_service(svc, {})
            self.assertIn("timed out", result)
            with open(pidfile) as f:
                pid = int(f.read())
            with self.assertRaises(ProcessLookupError):
                os.kill(pid, 0)

    @unittest.skipUnless(os.name == "posix", "process-group kill is POSIX")
    async def test_cancellation_kills_the_process(self):
        with tempfile.TemporaryDirectory() as d:
            pidfile = os.path.join(d, "pid")
            code = (
                "import os, time; "
                f"open({pidfile!r}, 'w').write(str(os.getpid())); "
                "time.sleep(60)"
            )
            svc = make_svc([PY, "-c", code], timeout=60)
            task = asyncio.create_task(fleet_mcp.run_service(svc, {}))
            for _ in range(100):
                if os.path.exists(pidfile):
                    break
                await asyncio.sleep(0.05)
            with open(pidfile) as f:
                pid = int(f.read())
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            for _ in range(50):
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    break
                await asyncio.sleep(0.05)
            with self.assertRaises(ProcessLookupError):
                os.kill(pid, 0)

    async def test_unknown_and_mistyped_arguments_are_rejected(self):
        svc = make_svc(
            [PY, "-c", "print('ran')"],
            parameters={"host": {"type": "string", "required": True}},
        )
        self.assertIn("unknown parameter", await fleet_mcp.run_service(svc, {"nope": 1}))
        typed = make_svc(
            [PY, "-c", "print('ran')"],
            parameters={"count": {"type": "integer", "required": True}},
        )
        self.assertIn("must be of type", await fleet_mcp.run_service(typed, {"count": "three"}))
        self.assertIn("must be of type", await fleet_mcp.run_service(typed, {"count": True}))
        ok = await fleet_mcp.run_service(typed, {"count": 3})
        self.assertEqual(ok, "ran")

    async def test_nonzero_exit_is_reported(self):
        svc = make_svc([PY, "-c", "import sys; sys.exit(3)"])
        self.assertIn("exit code 3", await fleet_mcp.run_service(svc, {}))


if __name__ == "__main__":
    unittest.main()
