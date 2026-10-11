"""
Tests for POST /employees and GET /employees.
Covers: creation, duplicate prevention, pagination, filtering, validation.
"""
import pytest


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
VALID_EMP = {
    "emp_code":   "EMP0010",
    "name":       "Test User",
    "email":      "test@example.com",
    "department": "Engineering",
    "shift_start": "09:30",
    "shift_end":   "18:30",
    "joined_on":  "2026-01-01",
}


def make_emp(client, overrides=None):
    payload = {**VALID_EMP, **(overrides or {})}
    return client.post("/employees", json=payload)


# ---------------------------------------------------------------------------
# POST /employees — happy path
# ---------------------------------------------------------------------------
class TestCreateEmployee:
    def test_returns_201(self, client):
        r = make_emp(client)
        assert r.status_code == 201

    def test_response_has_all_required_fields(self, client):
        r = make_emp(client).json()
        for field in ["emp_code", "name", "email", "department",
                      "shift_start", "shift_end", "joined_on", "created_at"]:
            assert field in r, f"missing field: {field}"

    def test_created_at_is_epoch_ms_integer(self, client):
        r = make_emp(client).json()
        assert isinstance(r["created_at"], int)
        assert r["created_at"] > 100_000_000_000   # valid epoch ms

    def test_no_id_field_in_response(self, client):
        r = make_emp(client).json()
        assert "_id" not in r
        assert "id" not in r

    def test_emp_code_returned_correctly(self, client):
        r = make_emp(client).json()
        assert r["emp_code"] == "EMP0010"

    def test_default_shift_times(self, client):
        payload = {k: v for k, v in VALID_EMP.items() if k not in ("shift_start", "shift_end")}
        payload["emp_code"] = "EMP0011"
        r = client.post("/employees", json=payload).json()
        assert r["shift_start"] == "09:30"
        assert r["shift_end"]   == "18:30"

    def test_overnight_shift_accepted(self, client):
        r = make_emp(client, {"emp_code": "EMP0012", "shift_start": "22:00", "shift_end": "06:00"})
        assert r.status_code == 201


# ---------------------------------------------------------------------------
# POST /employees — duplicate
# ---------------------------------------------------------------------------
class TestCreateEmployeeDuplicate:
    def test_duplicate_emp_code_returns_409(self, client):
        make_emp(client)
        r = make_emp(client)
        assert r.status_code == 409

    def test_duplicate_response_has_detail(self, client):
        make_emp(client)
        r = make_emp(client).json()
        assert "detail" in r


# ---------------------------------------------------------------------------
# POST /employees — validation errors
# ---------------------------------------------------------------------------
class TestCreateEmployeeValidation:
    def test_invalid_emp_code_format(self, client):
        r = make_emp(client, {"emp_code": "EMP123"})   # only 3 digits
        assert r.status_code == 422

    def test_emp_code_too_many_digits(self, client):
        r = make_emp(client, {"emp_code": "EMP1234567"})   # 7 digits
        assert r.status_code == 422

    def test_invalid_email(self, client):
        r = make_emp(client, {"email": "notanemail"})
        assert r.status_code == 422

    def test_shift_start_equals_shift_end_returns_422(self, client):
        r = make_emp(client, {"shift_start": "09:30", "shift_end": "09:30"})
        assert r.status_code == 422

    def test_missing_required_field(self, client):
        payload = {k: v for k, v in VALID_EMP.items() if k != "email"}
        r = client.post("/employees", json=payload)
        assert r.status_code == 422

    def test_invalid_shift_format(self, client):
        r = make_emp(client, {"shift_start": "9:30"})   # missing leading zero
        assert r.status_code == 422

    def test_invalid_joined_on(self, client):
        r = make_emp(client, {"joined_on": "01-01-2026"})
        assert r.status_code == 422


# ---------------------------------------------------------------------------
# GET /employees — listing, pagination, filtering
# ---------------------------------------------------------------------------
class TestListEmployees:
    def _seed_many(self, client, n=5):
        for i in range(1, n + 1):
            make_emp(client, {
                "emp_code":   f"EMP{1000 + i:04d}",
                "department": "Engineering" if i % 2 == 0 else "Sales",
            })

    def test_empty_collection_returns_zero(self, client):
        r = client.get("/employees").json()
        assert r["total"] == 0
        assert r["items"] == []

    def test_total_reflects_filter(self, client):
        self._seed_many(client)
        r = client.get("/employees?department=Engineering").json()
        assert r["total"] == 2   # EMP1002, EMP1004
        assert all(e["department"] == "Engineering" for e in r["items"])

    def test_sorted_by_emp_code_ascending(self, client):
        self._seed_many(client)
        codes = [e["emp_code"] for e in client.get("/employees").json()["items"]]
        assert codes == sorted(codes)

    def test_page_1_skip_is_correct(self, client):
        self._seed_many(client, 5)
        r1 = client.get("/employees?page=1&page_size=2").json()
        r2 = client.get("/employees?page=2&page_size=2").json()
        assert r1["items"][0]["emp_code"] != r2["items"][0]["emp_code"]
        assert r1["page"] == 1
        assert r2["page"] == 2

    def test_page_size_max_100(self, client):
        r = client.get("/employees?page_size=101")
        assert r.status_code == 422

    def test_page_minimum_1(self, client):
        r = client.get("/employees?page=0")
        assert r.status_code == 422

    def test_pagination_total_does_not_change_with_page(self, client):
        self._seed_many(client, 5)
        r1 = client.get("/employees?page=1&page_size=2").json()
        r2 = client.get("/employees?page=2&page_size=2").json()
        assert r1["total"] == r2["total"] == 5
