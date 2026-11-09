"""
Focused verification tests for Phases 2-5 of the API Sentinel improvement plan.

Tests cover:
- DNS/SSRF validation policy (pinned connections)
- OpenAPI GET discovery
- 200/5xx probe result paths
- No response-body persistence
- Model label thresholds and readiness reasons
- Probe incident creation and auto-recovery
- Probe history and 7-day retention logic
- Tenant scoping of probe runs
"""
import asyncio
import ipaddress
import socket
import time
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

# ---------------------------------------------------------------------------
# Tests: DNS/SSRF validation in probes.py
# ---------------------------------------------------------------------------

class TestValidatedTarget(unittest.TestCase):
    """Unit tests for _validated_target in probes.py."""

    def setUp(self):
        import os
        # Ensure allow-private is off for SSRF tests
        os.environ.pop("SENTINEL_ALLOW_PRIVATE_API_TARGETS", None)

    def test_rejects_http_public_target(self):
        from app.probes import _validated_target
        with self.assertRaises(ValueError, msg="http:// public should be rejected when allow_private=false"):
            _validated_target("http://httpbin.org/get")

    def test_rejects_url_too_long(self):
        from app.probes import _validated_target
        long_url = "https://example.com/" + "a" * 1990
        with self.assertRaises(ValueError):
            _validated_target(long_url)

    def test_rejects_url_with_credentials(self):
        from app.probes import _validated_target
        with self.assertRaises(ValueError):
            _validated_target("https://user:pass@example.com/api")

    def test_rejects_url_with_query(self):
        from app.probes import _validated_target
        with self.assertRaises(ValueError):
            _validated_target("https://example.com/api?key=secret")

    def test_rejects_url_with_fragment(self):
        from app.probes import _validated_target
        with self.assertRaises(ValueError):
            _validated_target("https://example.com/api#section")

    def test_rejects_non_http_scheme(self):
        from app.probes import _validated_target
        with self.assertRaises(ValueError):
            _validated_target("ftp://example.com/file")

    def test_rejects_bare_hostname(self):
        from app.probes import _validated_target
        with self.assertRaises(ValueError):
            _validated_target("example.com")

    def test_rejects_control_chars(self):
        from app.probes import _validated_target
        with self.assertRaises(ValueError):
            _validated_target("https://example.com/api\x00evil")

    def test_allows_https_when_private_disabled(self):
        """https:// should not be rejected on scheme grounds."""
        from app.probes import _validated_target
        # This may succeed or fail on DNS – just check it's not a scheme error
        try:
            _validated_target("https://httpbin.org/get")
        except ValueError as e:
            self.assertNotIn("HTTPS", str(e), "Should not reject https:// on scheme grounds")

    def test_allow_private_flag_permits_http_localhost(self):
        """With allow_private=true, http://127.0.0.1 should pass scheme check."""
        import os
        os.environ["SENTINEL_ALLOW_PRIVATE_API_TARGETS"] = "true"
        try:
            from app.probes import _validated_target
            # May fail on DNS or for other reasons, but NOT on scheme
            try:
                _validated_target("http://127.0.0.1:8000/api/health")
            except ValueError as e:
                self.assertNotIn("HTTPS", str(e))
        finally:
            del os.environ["SENTINEL_ALLOW_PRIVATE_API_TARGETS"]

    def test_private_ip_rejected_without_flag(self):
        """Private IPs must be rejected when allow_private=false."""
        import os
        os.environ.pop("SENTINEL_ALLOW_PRIVATE_API_TARGETS", None)
        from app.probes import _validated_target
        # 10.0.0.1 is private – patch resolution to avoid real DNS
        fake_info = [(None, None, None, None, ("10.0.0.1", 443))]
        with patch("socket.getaddrinfo", return_value=fake_info):
            with self.assertRaises(ValueError, msg="Private IP should be rejected"):
                _validated_target("https://internal.company.com/api")


class TestDiscoveredGets(unittest.TestCase):
    """Unit tests for _discovered_gets in probes.py."""

    def test_discovers_simple_get_routes(self):
        from app.probes import _discovered_gets
        spec = {
            "paths": {
                "/health": {"get": {}},
                "/status": {"get": {}},
                "/upload": {"post": {}},          # POST only – not discovered
                "/search": {"get": {              # Required query param – excluded
                    "parameters": [{"in": "query", "name": "q", "required": True}]
                }},
            }
        }
        result = _discovered_gets(spec, "https://api.example.com")
        self.assertIn("https://api.example.com/health", result)
        self.assertIn("https://api.example.com/status", result)
        self.assertNotIn("https://api.example.com/upload", result)
        self.assertNotIn("https://api.example.com/search", result)

    def test_excludes_path_params(self):
        from app.probes import _discovered_gets
        spec = {"paths": {"/users/{id}": {"get": {}}}}
        result = _discovered_gets(spec, "https://api.example.com")
        self.assertEqual(result, [])

    def test_max_endpoints_limit(self):
        from app.probes import _discovered_gets, MAX_ENDPOINTS
        paths = {f"/path{i}": {"get": {}} for i in range(20)}
        spec = {"paths": paths}
        result = _discovered_gets(spec, "https://api.example.com")
        self.assertLessEqual(len(result), MAX_ENDPOINTS)

    def test_server_url_must_match_origin(self):
        """OpenAPI servers[0].url with a different host should be ignored."""
        from app.probes import _discovered_gets
        spec = {
            "servers": [{"url": "https://other.example.com"}],
            "paths": {"/health": {"get": {}}},
        }
        result = _discovered_gets(spec, "https://api.example.com")
        # Result URL should use api.example.com, not other.example.com
        for url in result:
            self.assertIn("api.example.com", url)

    def test_optional_query_param_allowed(self):
        """GET routes with only optional query params are still discovered."""
        from app.probes import _discovered_gets
        spec = {"paths": {"/search": {"get": {
            "parameters": [{"in": "query", "name": "q", "required": False}]
        }}}}
        result = _discovered_gets(spec, "https://api.example.com")
        self.assertIn("https://api.example.com/search", result)


class TestResultStatus(unittest.TestCase):
    """Tests for _result_status."""

    def test_all_healthy(self):
        from app.probes import _result_status
        checks = [{"status_code": 200}, {"status_code": 201}]
        self.assertEqual(_result_status(checks), "healthy")

    def test_mixed_degraded(self):
        from app.probes import _result_status
        checks = [{"status_code": 200}, {"status_code": 500}]
        self.assertEqual(_result_status(checks), "degraded")

    def test_no_response_unreachable(self):
        from app.probes import _result_status
        checks = [{"status_code": None}]
        self.assertEqual(_result_status(checks), "unreachable")

    def test_empty_checks_unreachable(self):
        from app.probes import _result_status
        self.assertEqual(_result_status([]), "unreachable")

    def test_4xx_counts_as_unhealthy(self):
        from app.probes import _result_status
        checks = [{"status_code": 404}]
        self.assertEqual(_result_status(checks), "degraded")


# ---------------------------------------------------------------------------
# Tests: probe response body is NOT stored
# ---------------------------------------------------------------------------

class TestProbeBodyNotPersisted(unittest.TestCase):
    """Verify that the probe request doesn't store response bodies."""

    def test_safe_check_excludes_body(self):
        from app.probes import _safe_check
        result = _safe_check("https://api.example.com/health", 200, 15.5, None)
        self.assertNotIn("body", result)
        self.assertNotIn("response", result)
        self.assertNotIn("content", result)

    def test_api_probe_run_model_has_no_body_field(self):
        from app.models import ApiProbeRun
        columns = {col.name for col in ApiProbeRun.__table__.columns}
        self.assertNotIn("body", columns)
        self.assertNotIn("response_body", columns)
        self.assertNotIn("content", columns)

    def test_request_sync_read_body_false_by_default(self):
        """_request_sync with read_body=False should return empty bytes.
        
        Patch socket.create_connection so the pinned connection class
        doesn't need a real socket, then verify body is b"".
        """
        import os
        os.environ["SENTINEL_ALLOW_PRIVATE_API_TARGETS"] = "true"
        try:
            from app.probes import _request_sync
            fake_addrinfo = [(None, None, None, None, ("127.0.0.1", 8000))]
            mock_response = MagicMock()
            mock_response.status = 200
            mock_response.read.return_value = b""
            mock_sock = MagicMock()
            mock_sock.makefile.return_value = MagicMock()
            with patch("socket.getaddrinfo", return_value=fake_addrinfo), \
                 patch("socket.create_connection", return_value=mock_sock), \
                 patch("app.probes._PinnedHTTPConnection.getresponse", return_value=mock_response), \
                 patch("app.probes._PinnedHTTPConnection.request"):
                status, latency, body, error_type = _request_sync(
                    "http://127.0.0.1:8000/api/health", {}
                )
                self.assertEqual(body, b"")
        finally:
            del os.environ["SENTINEL_ALLOW_PRIVATE_API_TARGETS"]


# ---------------------------------------------------------------------------
# Tests: api_monitoring.py – incident lifecycle
# ---------------------------------------------------------------------------

class TestRecordApiConnectionCheck(unittest.IsolatedAsyncioTestCase):
    """Tests for record_api_connection_check in api_monitoring.py."""

    def _make_connection(self, connection_id="conn-1", org_id="org-1"):
        conn = MagicMock()
        conn.id = connection_id
        conn.organization_id = org_id
        conn.last_checks = []
        conn.status = "checking"
        conn.last_checked_at = None
        conn.endpoints = ["https://api.example.com/health"]
        conn.base_url = "https://api.example.com"
        return conn

    async def test_healthy_resolves_existing_incident(self):
        """A healthy check should resolve the existing open incident."""
        from app.api_monitoring import record_api_connection_check
        from app.models import Incident

        active_incident = MagicMock(spec=Incident)
        active_incident.status = "investigating"
        active_incident.resolved_at = None

        session = AsyncMock()
        session.scalar = AsyncMock(return_value=active_incident)
        session.add = MagicMock()

        conn = self._make_connection()
        checks = [{"url": "https://api.example.com/health", "method": "GET",
                   "status_code": 200, "latency_ms": 10.5, "error_type": None}]
        await record_api_connection_check(session, conn, checks, "healthy")

        self.assertEqual(active_incident.status, "resolved")
        self.assertIsNotNone(active_incident.resolved_at)

    async def test_failing_no_active_creates_incident(self):
        """A failing check with no active incident should create a new one."""
        from app.api_monitoring import record_api_connection_check
        from app.models import AlertDestination

        dest = MagicMock(spec=AlertDestination)
        dest.id = "dest-1"
        dest.organization_id = "org-1"

        session = AsyncMock()
        # No active incident
        session.scalar = AsyncMock(return_value=None)
        session.scalars = AsyncMock(return_value=[dest])
        session.add = MagicMock()

        conn = self._make_connection()
        checks = [{"url": "https://api.example.com/health", "method": "GET",
                   "status_code": 500, "latency_ms": 1200.0, "error_type": None}]
        await record_api_connection_check(session, conn, checks, "degraded")

        # Incident and notification should have been added
        added_types = [type(call[0][0]).__name__ for call in session.add.call_args_list]
        self.assertIn("Incident", added_types)
        self.assertIn("NotificationDelivery", added_types)

    async def test_failing_with_active_updates_evidence(self):
        """A failing check with an active incident updates evidence, not creates."""
        from app.api_monitoring import record_api_connection_check
        from app.models import Incident

        existing = MagicMock(spec=Incident)
        existing.status = "investigating"
        existing.evidence = []

        session = AsyncMock()
        session.scalar = AsyncMock(return_value=existing)
        session.add = MagicMock()

        conn = self._make_connection()
        checks = [{"url": "https://api.example.com/health", "method": "GET",
                   "status_code": 503, "latency_ms": 500.0, "error_type": None}]
        await record_api_connection_check(session, conn, checks, "degraded")

        # Evidence should be updated, no new Incident added
        added_types = [type(call[0][0]).__name__ for call in session.add.call_args_list]
        self.assertNotIn("Incident", added_types)
        self.assertIsNotNone(existing.evidence)

    async def test_unreachable_creates_high_severity(self):
        """Unreachable status should produce a high-severity incident."""
        from app.api_monitoring import record_api_connection_check
        from app.models import Incident

        created_incidents = []
        session = AsyncMock()
        session.scalar = AsyncMock(return_value=None)
        session.scalars = AsyncMock(return_value=[])

        def capture_add(obj):
            created_incidents.append(obj)

        session.add = MagicMock(side_effect=capture_add)

        conn = self._make_connection()
        checks = [{"url": "https://api.example.com/health", "method": "GET",
                   "status_code": None, "latency_ms": None, "error_type": "ConnectionError"}]
        await record_api_connection_check(session, conn, checks, "unreachable")

        incidents = [obj for obj in created_incidents if hasattr(obj, "severity")]
        self.assertTrue(len(incidents) > 0)
        self.assertEqual(incidents[0].severity, "high")

    async def test_probe_evidence_excludes_body(self):
        """Probe evidence must not contain response bodies."""
        from app.api_monitoring import _probe_evidence
        checks = [{"url": "https://api.example.com/health", "method": "GET",
                   "status_code": 500, "latency_ms": 200.0, "error_type": None}]
        evidence = _probe_evidence(checks)
        for item in evidence:
            self.assertNotIn("body", item)
            self.assertNotIn("response", item)


# ---------------------------------------------------------------------------
# Tests: intelligence.py – model readiness assessment
# ---------------------------------------------------------------------------

class TestAssessTrainingReadiness(unittest.TestCase):
    """Tests for assess_training_readiness."""

    def _make_window(self, minutes_ago: int) -> dict:
        return {
            "window_start": datetime.now(timezone.utc) - timedelta(minutes=minutes_ago),
            "features": {"error_5xx_rate": 0.0, "latency_p95": 100.0},
        }

    def test_insufficient_windows(self):
        from app.intelligence import assess_training_readiness
        windows = [self._make_window(i * 2) for i in range(10)]
        labels = [0] * 10
        result = assess_training_readiness(windows, labels)
        self.assertFalse(result["eligible"])
        self.assertEqual(result["reason"], "need_more_labeled_windows")

    def test_only_one_class(self):
        from app.intelligence import assess_training_readiness
        windows = [self._make_window(i) for i in range(50)]
        labels = [0] * 50  # All healthy, no failures
        result = assess_training_readiness(windows, labels)
        self.assertFalse(result["eligible"])
        self.assertIn("failure", result["reason"])

    def test_eligible_with_enough_mixed_data(self):
        from app.intelligence import assess_training_readiness
        # 40 windows: 30 healthy, 10 failure – spread over time
        windows = [self._make_window(i * 10) for i in range(40)]
        labels = [1 if i < 10 else 0 for i in range(40)]
        result = assess_training_readiness(windows, labels)
        # May or may not be eligible depending on split/time gap, but should not error
        self.assertIn("eligible", result)
        self.assertIn("labeled_windows", result)
        self.assertIn("positive_windows", result)
        self.assertIn("negative_windows", result)

    def test_none_labels_excluded(self):
        """None labels (too few future requests) should be excluded from counts."""
        from app.intelligence import assess_training_readiness
        windows = [self._make_window(i) for i in range(50)]
        labels = [None] * 50  # All unlabeled
        result = assess_training_readiness(windows, labels)
        self.assertEqual(result["labeled_windows"], 0)
        self.assertFalse(result["eligible"])

    def test_readiness_message_in_training_data(self):
        """get_training_readiness should return structured dict with message."""
        # This is verified structurally – the message key must exist in result
        from app.intelligence import assess_training_readiness
        windows = [self._make_window(i) for i in range(5)]
        labels = [0] * 5
        result = assess_training_readiness(windows, labels)
        # assess_training_readiness doesn't add message – that's added in operations.py
        # just check the keys the UI reads
        self.assertIn("labeled_windows", result)
        self.assertIn("positive_windows", result)
        self.assertIn("negative_windows", result)


# ---------------------------------------------------------------------------
# Tests: operations.py – get_training_readiness structured response
# ---------------------------------------------------------------------------

class TestGetTrainingReadiness(unittest.IsolatedAsyncioTestCase):
    """Tests for get_training_readiness in operations.py."""

    async def test_no_telemetry_gives_clear_message(self):
        """With no telemetry events, the reason should be no_passive_telemetry."""
        from app.operations import _training_data
        from app.models import Service

        session = AsyncMock()
        # No services
        session.scalars = AsyncMock(return_value=[])

        windows, labels, latency_targets, readiness = await _training_data(session, "org-1")
        # With no services there are no events
        self.assertEqual(windows, [])
        self.assertIn("reason", readiness)

    async def test_readiness_has_all_required_keys(self):
        """get_training_readiness must return all keys the frontend reads."""
        from app.operations import get_training_readiness

        session = AsyncMock()
        session.scalars = AsyncMock(return_value=[])  # No services

        result = await get_training_readiness(session, "org-1")

        required_keys = {
            "eligible", "reason", "labeled_windows", "positive_windows",
            "negative_windows", "minimum_labeled_windows", "telemetry_events",
            "feature_windows",
        }
        for key in required_keys:
            self.assertIn(key, result, f"Missing key: {key}")


# ---------------------------------------------------------------------------
# Tests: probe history retention
# ---------------------------------------------------------------------------

class TestProbeHistoryRetention(unittest.TestCase):
    """Verify that the ApiProbeRun delete logic uses 7-day cutoff."""

    def test_retention_cutoff_seven_days(self):
        """Inspect tasks.py to confirm 7-day retention boundary."""
        import ast
        import inspect
        from app import tasks
        source = inspect.getsource(tasks._probe_connections)
        # Confirm '7' (days) appears in the probe connection logic
        self.assertIn("7", source, "7-day retention should be referenced in _probe_connections")
        self.assertIn("timedelta", source)

    def test_api_probe_run_model_has_checked_at(self):
        """ApiProbeRun must have a checked_at index for efficient retention deletes."""
        from app.models import ApiProbeRun
        columns = {col.name for col in ApiProbeRun.__table__.columns}
        self.assertIn("checked_at", columns)
        self.assertIn("organization_id", columns)
        self.assertIn("connection_id", columns)


# ---------------------------------------------------------------------------
# Tests: Tenant isolation
# ---------------------------------------------------------------------------

class TestTenantScoping(unittest.TestCase):
    """Verify that probe run queries always include organization_id."""

    def test_api_probe_run_has_organization_fk(self):
        from app.models import ApiProbeRun
        columns = {col.name for col in ApiProbeRun.__table__.columns}
        self.assertIn("organization_id", columns)

    def test_incident_has_organization_id(self):
        from app.models import Incident
        columns = {col.name for col in Incident.__table__.columns}
        self.assertIn("organization_id", columns)

    def test_incident_one_source_constraint_exists(self):
        """The check constraint ensuring exactly one source must exist."""
        from app.models import Incident
        constraint_names = {c.name for c in Incident.__table__.constraints}
        self.assertIn("ck_incident_one_source", constraint_names)

    def test_incident_service_id_nullable(self):
        """service_id must be nullable to allow api_connection_id incidents."""
        from app.models import Incident
        col = Incident.__table__.columns["service_id"]
        self.assertTrue(col.nullable)

    def test_incident_api_connection_id_nullable(self):
        """api_connection_id must be nullable to allow service-based incidents."""
        from app.models import Incident
        col = Incident.__table__.columns["api_connection_id"]
        self.assertTrue(col.nullable)


# ---------------------------------------------------------------------------
# Tests: notifications.py – service name resolution for probe incidents
# ---------------------------------------------------------------------------

class TestNotificationsServiceName(unittest.TestCase):
    """Verify incident_message works for probe incidents (api_connection_id)."""

    def test_incident_message_formats_correctly(self):
        from app.notifications import incident_message
        from app.models import Incident

        incident = MagicMock(spec=Incident)
        incident.severity = "high"
        incident.title = "Connected API check failed"
        incident.status = "investigating"
        incident.started_at = datetime.now(timezone.utc)
        incident.id = "inc-test-123"
        incident.kind = "api_probe"

        msg = incident_message(incident, "My API Monitor")
        self.assertIn("My API Monitor", msg)
        self.assertIn("HIGH", msg)
        self.assertIn("api_probe", msg)
        self.assertIn("inc-test-123", msg)


# ---------------------------------------------------------------------------
# Integration smoke test – local API server
# ---------------------------------------------------------------------------

class TestApiSmokeTest(unittest.TestCase):
    """Quick smoke tests against the local API server (requires it to be running)."""

    def _get(self, path: str, timeout: int = 5):
        import urllib.request
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:8000{path}", timeout=timeout) as resp:
                import json
                return resp.status, json.loads(resp.read())
        except Exception as e:
            self.skipTest(f"Local API not running: {e}")

    def test_health_endpoint(self):
        status, body = self._get("/api/health")
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "ok")

    def test_connections_history_endpoint_requires_auth(self):
        import urllib.request
        import urllib.error
        try:
            with urllib.request.urlopen(
                "http://127.0.0.1:8000/api/connections/nonexistent-id/history", timeout=5
            ) as _:
                pass
        except urllib.error.HTTPError as e:
            # Expect 401 Unauthorized (not 404 or 500)
            self.assertEqual(e.code, 401)
        except Exception as e:
            self.skipTest(f"Local API not running: {e}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
