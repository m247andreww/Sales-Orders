"""The nightly sync: Google Sheets reader, Reference sheet, Xero customers and chart, account moves, run log."""

from __future__ import annotations

import base64
import csv
import json
import re
import urllib.parse
import urllib.request
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import date
from uuid import UUID

import pytest
from conftest import FIXTURES
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from sales_orders import service, sync
from sales_orders.db import Connection
from sales_orders.google_sheets import (
    GoogleSheetsError,
    ServiceAccount,
    SheetsClient,
    rows_as_csv,
    signed_assertion,
)
from sales_orders.jobs import NightlyConfig, Step, StepOutcome, nightly_steps, run_job
from sales_orders.models import MasterDataIn
from sales_orders.reference import (
    MOVES_HEADERS,
    STAFF_HEADERS,
    ReferenceFormatError,
    parse_moves,
    parse_staff,
)
from sales_orders.register import parse_register
from sales_orders.xero import XeroCustomer, parse_accounts, parse_customers

TERRITORY = "test.territory@example.com"
ISAM = "test.isam@example.com"

# ------------------------------------------------------------------ Google Sheets


def _key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _account(key: rsa.RSAPrivateKey) -> ServiceAccount:
    pem = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()
    info = {
        "type": "service_account",
        "client_email": "reader@example.iam.gserviceaccount.com",
        "private_key": pem,
        "token_uri": "https://oauth2.example.com/token",
    }
    return ServiceAccount.from_json(json.dumps(info))


def _b64decode(part: str) -> bytes:
    return base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))


def test_signed_assertion_verifies_with_the_public_key() -> None:
    key = _key()
    header, claims, signature = signed_assertion(_account(key), now=1_000_000).split(".")
    key.public_key().verify(
        _b64decode(signature), f"{header}.{claims}".encode(), padding.PKCS1v15(), hashes.SHA256()
    )  # raises if the signature is wrong
    body = json.loads(_b64decode(claims))
    assert body == {
        "iss": "reader@example.iam.gserviceaccount.com",
        "scope": "https://www.googleapis.com/auth/spreadsheets.readonly",
        "aud": "https://oauth2.example.com/token",
        "iat": 1_000_000,
        "exp": 1_003_600,
    }


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("not json", "not valid JSON"),
        ('{"type": "authorized_user"}', "not a service-account key"),
        ('{"type": "service_account", "client_email": "x"}', "missing: private_key, token_uri"),
    ],
)
def test_unusable_google_key_is_rejected(text: str, message: str) -> None:
    with pytest.raises(GoogleSheetsError, match=message):
        ServiceAccount.from_json(text)


def test_sheets_client_signs_in_once_and_reads_displayed_values() -> None:
    calls: list[urllib.request.Request] = []

    def transport(request: urllib.request.Request) -> bytes:
        calls.append(request)
        if request.full_url == "https://oauth2.example.com/token":
            return json.dumps({"access_token": "tok"}).encode()
        return json.dumps({"values": [["a", 1], ["b"]]}).encode()

    client = SheetsClient(_account(_key()), transport, clock=lambda: 1_000_000)
    assert client.values("sheet-1", "'Account moves'") == [["a", "1"], ["b"]]
    client.values("sheet-1", "Staff")
    assert len(calls) == 3  # one sign-in, two reads
    grant = urllib.parse.parse_qs((calls[0].data or b"").decode())  # type: ignore[union-attr]
    assert grant["grant_type"] == ["urn:ietf:params:oauth:grant-type:jwt-bearer"]
    assert calls[1].full_url.startswith(
        "https://sheets.googleapis.com/v4/spreadsheets/sheet-1/values/%27Account%20moves%27?"
    )
    assert "valueRenderOption=FORMATTED_VALUE" in calls[1].full_url
    assert calls[1].get_header("Authorization") == "Bearer tok"


def test_register_read_from_sheets_parses_as_the_csv_export_does() -> None:
    with (FIXTURES / "test_register.csv").open(encoding="utf-8", newline="") as fh:
        exported = list(csv.reader(fh))
    # Google leaves out empty cells at the end of a row
    as_google_returns = [row[: max((i + 1 for i, c in enumerate(row) if c), default=0)] for row in exported]
    assert as_google_returns != exported  # the fixture really has trailing empty cells
    with (FIXTURES / "test_register.csv").open(encoding="utf-8", newline="") as fh:
        direct = parse_register(fh)
    via_sheets = parse_register(rows_as_csv(as_google_returns))
    assert via_sheets == direct
    assert len(direct.rows) > 0


# ------------------------------------------------------------------ Reference sheet


def _staff(*rows: Sequence[str]) -> list[list[str]]:
    return [list(STAFF_HEADERS), *[list(r) for r in rows]]


def test_staff_tab_is_parsed() -> None:
    staff = parse_staff(
        _staff(
            [
                "Test Territory Manager",
                "Test.Territory@example.com",
                "",
                "Territory, T Manager",
                "a. Test",
                "",
                "",
            ],
            ["", "", "", "", "", "", ""],  # blank rows are ignored
            ["Test Leaver", "leaver@example.com", "TM", "Leaver", "", "14/12/2025", "gardening leave"],
        )
    )
    assert [(s.email, s.register_labels, s.xero_group, s.last_working_day) for s in staff] == [
        ("test.territory@example.com", ("Territory", "T Manager"), "a. Test", None),
        ("leaver@example.com", ("Leaver",), None, date(2025, 12, 14)),
    ]


@pytest.mark.parametrize(
    ("rows", "message"),
    [
        ([["Name", "Email"]], "headings must be"),
        (_staff(["X", "not-an-email", "", "", "", "", ""]), "row 2: 'not-an-email' is not an email"),
        (_staff(["X", "x@example.com", "", "", "", "2025-12-14", ""]), "not a date in dd/mm/yyyy"),
        (_staff(["X", "x@example.com"], ["Y", "X@example.com"]), "row 3: x@example.com appears twice"),
        (
            _staff(["X", "x@example.com", "", "Sam"], ["Y", "y@example.com", "", "sam"]),
            "also given to x@example.com",
        ),
        (_staff(["X", "x@example.com", "", "Auto Renew"]), "is a House label"),
    ],
)
def test_staff_tab_problems_stop_the_load(rows: list[list[str]], message: str) -> None:
    with pytest.raises(ReferenceFormatError, match=message):
        parse_staff(rows)


def test_account_moves_tab_is_parsed() -> None:
    moves = parse_moves(
        [
            list(MOVES_HEADERS),
            ["a@example.com", "b@example.com", "31/03/2026", "CFO decision"],
            ["a@example.com", "house", "01/04/2026", "CFO decision"],
        ]
    )
    assert [(m.row, m.to, m.effective) for m in moves] == [
        (2, "b@example.com", date(2026, 3, 31)),
        (3, "House", date(2026, 4, 1)),
    ]
    with pytest.raises(ReferenceFormatError, match="Reason is required"):
        parse_moves([list(MOVES_HEADERS), ["a@example.com", "House", "01/04/2026", ""]])


def test_staff_load_creates_people_labels_groups_and_applies_leavers(
    conn: Connection, master_data: MasterDataIn
) -> None:
    staff = parse_staff(
        _staff(
            ["Test Territory Manager", TERRITORY, "", "Territory Label", "t. Territory", "31/05/2026", ""],
            ["New Starter", "new.starter@example.com", "Account Manager", "New Starter", "n. New", "", ""],
        )
    )
    detail = sync.load_staff(conn, staff)
    assert detail == "staff: 2 people, 1 leaver(s); Test Territory Manager: 1 account(s) to House"
    assert sync.load_staff(conn, staff) == "staff: 2 people, 1 leaver(s)"  # a second night changes nothing
    labels = conn.execute(
        """SELECT a.register_label, e.email FROM sales.register_owner_alias a JOIN sales.employee e USING (employee_id)
            ORDER BY 1"""
    ).fetchall()
    assert [(r["register_label"], r["email"]) for r in labels] == [
        ("new starter", "new.starter@example.com"),
        ("territory label", TERRITORY),
    ]
    groups = conn.execute(
        """SELECT g.group_name, e.email FROM sales.xero_owner_group g JOIN sales.employee e USING (employee_id)
            WHERE g.group_name IN ('n. New', 't. Territory') ORDER BY 1"""
    ).fetchall()
    assert [(g["group_name"], g["email"]) for g in groups] == [
        ("n. New", "new.starter@example.com"),
        ("t. Territory", TERRITORY),
    ]


# ------------------------------------------------------------------ Xero customers and chart


def _customer(n: int, name: str, archived: bool = False) -> XeroCustomer:
    return XeroCustomer(UUID(f"00000000-0000-4000-8000-{n:012d}"), name, archived)


def test_xero_contacts_response_is_parsed() -> None:
    parsed = parse_customers(
        {
            "Contacts": [
                {
                    "ContactID": "00000000-0000-4000-8000-000000000001",
                    "Name": "Acme  Ltd",
                    "IsCustomer": True,
                },
                {
                    "ContactID": "00000000-0000-4000-8000-000000000002",
                    "Name": "Supplier",
                    "IsCustomer": False,
                },
                {
                    "ContactID": "00000000-0000-4000-8000-000000000003",
                    "Name": "Old Co",
                    "ContactStatus": "ARCHIVED",
                },
            ]
        }
    )
    assert [(c.name, c.archived) for c in parsed] == [("Acme Ltd", False), ("Old Co", True)]


def test_customers_follow_xero_by_contact_id(conn: Connection, master_data: MasterDataIn) -> None:
    conn.execute("UPDATE sales.customer SET xero_contact_id = NULL")  # not yet linked to Xero
    first = sync.sync_xero_customers(
        conn,
        [
            _customer(1, "Test Customer Ltd"),  # links the existing customer by name
            _customer(2, "Brand New Customer Ltd"),
        ],
    )
    assert (first.added, first.updated, first.unchanged) == (1, 1, 0)
    second = sync.sync_xero_customers(
        conn,
        [
            _customer(1, "Test Customer Holdings Ltd"),  # renamed in Xero
            _customer(2, "Brand New Customer Ltd", archived=True),
            _customer(3, "Test Customer Holdings Ltd"),  # a second contact wanting the same name
        ],
    )
    assert (second.added, second.updated, second.unchanged) == (0, 2, 0)
    assert second.skipped == ["'Test Customer Holdings Ltd' is already linked to another Xero contact"]
    rows = conn.execute("SELECT legal_name, xero_archived FROM sales.customer ORDER BY legal_name").fetchall()
    assert [(r["legal_name"], r["xero_archived"]) for r in rows] == [
        ("Brand New Customer Ltd", True),
        ("Test Customer Holdings Ltd", False),
    ]


def test_chart_of_accounts_follows_xero(conn: Connection, master_data: MasterDataIn) -> None:
    accounts = parse_accounts(
        {
            "Accounts": [
                {"AccountID": "00000000-0000-4000-8000-00000000a001", "Code": "4001", "Name": "New Revenue",
                 "Class": "REVENUE", "Type": "REVENUE", "Status": "ACTIVE"},
                {"AccountID": "00000000-0000-4000-8000-00000000a002", "Code": "090", "Name": "Bank",
                 "Class": "ASSET", "Type": "BANK"},
                {"AccountID": "00000000-0000-4000-8000-00000000a003", "Code": "4002", "Name": "Old Revenue",
                 "Class": "REVENUE", "Type": "REVENUE", "Status": "ARCHIVED"},
            ]
        }
    )  # fmt: skip
    counts = sync.sync_xero_accounts(conn, accounts)
    assert (counts.added, counts.skipped) == (2, ["090 Bank"])
    rows = conn.execute(
        "SELECT account_code, is_active FROM sales.gl_account WHERE account_code IN ('4001', '4002') ORDER BY 1"
    ).fetchall()
    assert [(r["account_code"], r["is_active"]) for r in rows] == [("4001", True), ("4002", False)]
    again = sync.sync_xero_accounts(conn, accounts)
    assert (again.added, again.updated, again.unchanged) == (0, 0, 2)


# ------------------------------------------------------------------ account moves


def _owners(conn: Connection) -> list[tuple[str | None, date, date | None]]:
    rows = conn.execute(
        """SELECT COALESCE(e.email, a.house_account) AS owner, a.allocated_from, a.allocated_to
             FROM sales.customer_account_allocation a LEFT JOIN sales.employee e USING (employee_id)
            ORDER BY a.allocated_from"""
    ).fetchall()
    return [(r["owner"], r["allocated_from"], r["allocated_to"]) for r in rows]


def test_leavers_accounts_move_to_successor_once(conn: Connection, master_data: MasterDataIn) -> None:
    service.employee_leaves(conn, TERRITORY, date(2026, 2, 28))
    moves = parse_moves([list(MOVES_HEADERS), [TERRITORY, ISAM, "31/03/2026", "CFO: accounts to successor"]])
    assert sync.apply_account_moves(conn, moves) == (
        "account moves: 1 row(s), 1 change(s); row 2 Test Customer Ltd: ownership set"
    )
    assert _owners(conn) == [
        (TERRITORY, date(2026, 1, 1), date(2026, 2, 28)),
        ("House", date(2026, 3, 1), date(2026, 3, 30)),
        (ISAM, date(2026, 3, 31), None),
    ]
    assert sync.apply_account_moves(conn, moves) == "account moves: 1 row(s), 0 change(s)"


def test_period_handed_to_house_after_leaving_also_moves(conn: Connection, master_data: MasterDataIn) -> None:
    # The fixture's period starts after this last day, so the leaver routine hands it to House
    # (source "...; <name> had left ...") rather than closing it (source "Leaver: ...").
    service.employee_leaves(conn, TERRITORY, date(2025, 12, 14))
    assert _owners(conn) == [("House", date(2026, 1, 1), None)]
    moves = parse_moves([list(MOVES_HEADERS), [TERRITORY, ISAM, "31/03/2026", "CFO"]])
    sync.apply_account_moves(conn, moves)
    assert _owners(conn) == [("House", date(2026, 1, 1), date(2026, 3, 30)), (ISAM, date(2026, 3, 31), None)]


def test_move_to_an_unknown_person_is_refused(conn: Connection, master_data: MasterDataIn) -> None:
    moves = parse_moves([list(MOVES_HEADERS), [TERRITORY, "nobody@example.com", "31/03/2026", "CFO"]])
    with pytest.raises(Exception, match=re.escape("unknown or inactive employee nobody@example.com")):
        sync.apply_account_moves(conn, moves)


# ------------------------------------------------------------------ run log and the nightly job


@contextmanager
def _in_savepoint(conn: Connection) -> Iterator[Connection]:
    with conn.transaction():
        yield conn


def test_failed_step_is_logged_and_the_rest_still_run(conn: Connection) -> None:
    def boom(_c: Connection) -> str:
        raise RuntimeError("Xero is down")

    report = run_job(
        "nightly-sync",
        [
            Step("first", lambda _c: "fine"),
            Step("second", boom),
            StepOutcome("third", "skipped", "not configured"),
            Step("fourth", lambda _c: "also fine"),
        ],
        lambda: _in_savepoint(conn),
    )
    assert not report.ok
    rows = conn.execute(
        """SELECT status, summary, step_name, step_status, detail FROM sales.v_job_run_latest
            WHERE job_name = 'nightly-sync' ORDER BY step_no"""
    ).fetchall()
    assert [(r["step_name"], r["step_status"]) for r in rows] == [
        ("first", "succeeded"),
        ("second", "failed"),
        ("third", "skipped"),
        ("fourth", "succeeded"),
    ]
    assert rows[1]["detail"].startswith("RuntimeError: Xero is down (test_nightly_sync.py:")
    assert (rows[0]["status"], rows[0]["summary"]) == ("failed", "failed: second")


class _FakeSheets(SheetsClient):
    def __init__(self, tabs: dict[tuple[str, str], list[list[str]]]) -> None:
        self._tabs = tabs

    def values(self, spreadsheet_id: str, sheet_range: str) -> list[list[str]]:
        return self._tabs[(spreadsheet_id, sheet_range)]


def test_nightly_sync_loads_staff_register_and_moves(conn: Connection, master_data: MasterDataIn) -> None:
    with (FIXTURES / "test_register.csv").open(encoding="utf-8", newline="") as fh:
        register = list(csv.reader(fh))
    sheets = _FakeSheets(
        {
            ("ref", "Staff"): _staff(
                ["Test Territory Manager", TERRITORY, "", "Test Territory Manager", "", "", ""]
            ),
            ("reg", "Register"): register,
            ("ref", "'Account moves'"): [list(MOVES_HEADERS)],
        }
    )
    report = run_job(
        "nightly-sync",
        list(nightly_steps(NightlyConfig("reg", "Register", "ref"), sheets, xero=None)),
        lambda: _in_savepoint(conn),
    )
    outcomes = [(s.name, s.status) for s in report.steps]
    assert outcomes == [
        ("reference-staff", "succeeded"),
        ("xero-chart-of-accounts", "skipped"),
        ("xero-customers", "skipped"),
        ("register", "succeeded"),
        ("account-history", "succeeded"),
        ("account-moves", "succeeded"),
        ("xero-owner-groups", "skipped"),
    ]
    assert report.ok
    register_step = next(s for s in report.steps if s.name == "register")
    assert register_step.detail.startswith("Register sync ")
    assert "rows loaded" in register_step.detail
