"""HTTP-level tests using Flask's test client."""

import io
import re

from recon.synthetic import generate


def upload(client, csvs, **extra):
    data = {name: (io.BytesIO(text.encode()), f"{name}.csv") for name, text in csvs.items()}
    data.update(extra)
    return client.post("/runs", data=data, content_type="multipart/form-data")


def run_id_from(resp):
    return int(re.search(r"/runs/(\d+)", resp.headers["Location"]).group(1))


def test_index_page(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert b"New reconciliation run" in resp.data and b"No runs yet" in resp.data


def test_sample_run_end_to_end(client):
    resp = client.post("/runs/sample")
    assert resp.status_code == 302
    page = client.get(resp.headers["Location"])
    assert page.status_code == 200
    assert b"Exceptions by type" in page.data and b"Duplicate Charge" in page.data
    assert b"Sample data" in client.get("/").data


def test_upload_valid_files(client):
    data = generate(n_orders=60, per_type=1, seed=5)
    resp = upload(client, data.to_csv(), label="Week 1", settlement_window="2", as_of=data.as_of.isoformat())
    assert resp.status_code == 302
    run_id = run_id_from(resp)
    api = client.get(f"/api/runs/{run_id}").get_json()
    assert api["label"] == "Week 1"
    assert api["exception_count"] == len(data.expected)
    assert api["exceptions_by_status"] == {"OPEN": len(data.expected)}
    assert 0 < api["match_rate"] < 100


def test_upload_missing_file_shows_error(client):
    data = generate(n_orders=40, per_type=1).to_csv()
    del data["settlements"]
    resp = upload(client, data, settlement_window="2")
    assert resp.status_code == 303
    assert b"Please upload settlements.csv" in client.get("/").data


def test_upload_invalid_rows_shows_line_errors(client):
    csvs = generate(n_orders=40, per_type=1).to_csv()
    csvs["orders"] += "BAD1,C1,2025-08-01,not-money,INR\n"
    upload(client, csvs)
    page = client.get("/").data
    assert b"not a number" in page and b"No runs yet" in page


def test_upload_rejects_bad_options_and_non_utf8(client):
    csvs = generate(n_orders=40, per_type=1).to_csv()
    data = {name: (io.BytesIO(text.encode()), f"{name}.csv") for name, text in csvs.items()}
    data["orders"] = (io.BytesIO(b"\xff\xfe\x00bad"), "orders.csv")
    data.update(settlement_window="99", as_of="31-31-2025")
    client.post("/runs", data=data, content_type="multipart/form-data")
    page = client.get("/").data
    assert b"between 0 and 30" in page and b"valid date" in page and b"UTF-8" in page


def test_filters_and_export(client):
    run_id = run_id_from(client.post("/runs/sample"))
    page = client.get(f"/runs/{run_id}?type=DUPLICATE_CHARGE&severity=HIGH&status=OPEN&q=ORD").data
    rows = page.split(b"<h2>Exceptions</h2>")[1].split(b"<tbody>")[1]
    assert b"Duplicate Charge" in rows and b"Missing Payment" not in rows

    resp = client.get(f"/runs/{run_id}/export.csv")
    assert resp.mimetype == "text/csv"
    lines = resp.data.decode().strip().splitlines()
    assert lines[0].startswith("id,type,severity,status")
    assert len(lines) - 1 == client.get(f"/api/runs/{run_id}").get_json()["exception_count"]


def test_exception_workflow_via_http(client):
    run_id_from(client.post("/runs/sample"))
    page = client.get("/exceptions/1")
    assert page.status_code == 200 and b"Audit history" in page.data

    client.post("/exceptions/1/assign", data={"actor": "Aryan", "assignee": "Priya"})
    client.post("/exceptions/1/status", data={"actor": "Priya", "status": "IN_REVIEW"})
    client.post("/exceptions/1/comment", data={"actor": "Priya", "note": "Checking with gateway"})
    resp = client.post("/exceptions/1/status", data={"actor": "Priya", "status": "RESOLVED"}, follow_redirects=True)
    assert b"note is required" in resp.data
    resp = client.post("/exceptions/1/status", data={"actor": "Priya", "status": "RESOLVED", "note": "Refunded"},
                       follow_redirects=True)
    assert b"Saved." in resp.data
    for text in (b"Assigned to Priya", b"Checking with gateway", b"Refunded", b"Resolved"):
        assert text in resp.data


def test_not_found_routes(client):
    assert client.get("/runs/999").status_code == 404
    assert client.get("/runs/999/export.csv").status_code == 404
    assert client.get("/exceptions/999").status_code == 404
    assert client.post("/exceptions/999/status", data={}).status_code == 404
    assert client.get("/api/runs/999").status_code == 404


def test_health(client):
    assert client.get("/api/health").get_json() == {"status": "ok"}
