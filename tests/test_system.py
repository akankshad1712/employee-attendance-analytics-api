"""
Tests for system endpoints: /health and /admin/explain.
"""
import pytest


class TestHealth:
    def test_returns_200(self, client):
        r = client.get("/health")
        assert r.status_code == 200

    def test_response_body(self, client):
        r = client.get("/health").json()
        assert r == {"status": "ok"}


class TestExplain:
    def test_unknown_endpoint_returns_422(self, client):
        r = client.get("/admin/explain/nonexistent_endpoint")
        assert r.status_code == 422

    def test_attendance_list_explain(self, client):
        r = client.get("/admin/explain/attendance_list")
        assert r.status_code == 200
        body = r.json()
        assert body["endpoint"]   == "attendance_list"
        assert body["collection"] == "attendance_logs"
        assert "explain" in body

    def test_employee_monthly_requires_emp_code(self, client):
        r = client.get("/admin/explain/employee_monthly?month=2026-07")
        assert r.status_code == 422

    def test_employee_monthly_requires_month(self, client):
        r = client.get("/admin/explain/employee_monthly?emp_code=EMP0001")
        assert r.status_code == 422

    def test_employee_monthly_explain(self, client):
        r = client.get("/admin/explain/employee_monthly?emp_code=EMP0001&month=2026-07")
        assert r.status_code == 200
        assert r.json()["collection"] == "attendance_logs"

    def test_department_summary_requires_month(self, client):
        r = client.get("/admin/explain/department_summary")
        assert r.status_code == 422

    def test_department_summary_explain(self, client):
        r = client.get("/admin/explain/department_summary?month=2026-07")
        assert r.status_code == 200

    def test_late_leaderboard_requires_month(self, client):
        r = client.get("/admin/explain/late_leaderboard")
        assert r.status_code == 422

    def test_late_leaderboard_explain(self, client):
        r = client.get("/admin/explain/late_leaderboard?month=2026-07")
        assert r.status_code == 200

    def test_department_trend_requires_from_to(self, client):
        r = client.get("/admin/explain/department_trend?department=Engineering")
        assert r.status_code == 422

    def test_department_trend_explain(self, client):
        r = client.get(
            "/admin/explain/department_trend"
            "?department=Engineering&from=2026-07-01&to=2026-07-07"
        )
        assert r.status_code == 200
        assert r.json()["collection"] == "attendance_logs"

    def test_explain_contains_no_collscan_on_indexed_query(self, client):
        """The winning plan for attendance_list must use IXSCAN not COLLSCAN."""
        r = client.get("/admin/explain/attendance_list?emp_code=EMP0001")
        assert r.status_code == 200
        explain_str = str(r.json()["explain"])
        assert "IXSCAN"   in explain_str
        assert "COLLSCAN" not in explain_str
