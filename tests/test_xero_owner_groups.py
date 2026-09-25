"""Account owner from Xero contact groups (CFO, 2026-09-25), reconciled with the database."""

from __future__ import annotations

import base64
import copy
import json
import urllib.request
from datetime import date, datetime, timedelta
from typing import Any

import pytest
from conftest import fixture_json, load

from sales_orders import service
from sales_orders.db import Connection
from sales_orders.models import MasterDataIn
from sales_orders.xero import (
    XeroCredentials,
    XeroFormatError,
    fetch_contact_groups,
    parse_contact_groups,
)

CUSTOMER = "Test Customer Ltd"
CONTACT = "00000000-0000-4000-8000-00000000c001"
TERRITORY_GROUP, ISAM_GROUP, HOUSE_GROUP = 0, 1, 2  # positions in the fixture


def _groups(members: dict[int, list[str]] | None = None) -> dict[str, Any]:
    """The fixture response, optionally with the owner groups' contacts replaced."""
    doc = copy.deepcopy(fixture_json("test_xero_contact_groups.json"))
    for index, contacts in (members or {}).items():
        doc["ContactGroups"][index]["Contacts"] = [{"ContactID": c, "Name": "x"} for c in contacts]
    return doc


def _now(conn: Connection) -> datetime:
    row = conn.execute("SELECT now() AS t").fetchone()
    assert row is not None
    value: datetime = row["t"]
    return value


def _sync(
    conn: Connection, doc: dict[str, Any], days_from_now: int, adopt_xero: bool = False
) -> dict[str, Any]:
    observed = _now(conn) + timedelta(days=days_from_now)
    return service.sync_xero_groups(
        conn, parse_contact_groups(doc), "test", adopt_xero=adopt_xero, observed_at=observed
    )


@pytest.fixture
def aged_master_data(conn: Connection, master_data: MasterDataIn) -> MasterDataIn:
    """Master data whose ownership was recorded ten days ago, so syncs can be dated after it.

    The touch trigger stamps updated_at with the transaction time; it is paused for this one
    back-dating update (the test transaction is rolled back).
    """
    conn.execute("ALTER TABLE sales.customer_account_allocation DISABLE TRIGGER touch_row")
    conn.execute("UPDATE sales.customer_account_allocation SET updated_at = now() - interval '10 days'")
    conn.execute("ALTER TABLE sales.customer_account_allocation ENABLE TRIGGER touch_row")
    return master_data


def _status(conn: Connection) -> tuple[str, str | None, str | None]:
    row = conn.execute(
        """SELECT status, db_owner, xero_owner FROM sales.v_account_owner_reconciliation
            WHERE legal_name = %s""",
        (CUSTOMER,),
    ).fetchone()
    assert row is not None
    return row["status"], row["db_owner"], row["xero_owner"]


def _owners(conn: Connection) -> list[tuple[str | None, date, date | None]]:
    rows = conn.execute(
        """SELECT COALESCE(e.full_name, a.house_account) AS owner, a.allocated_from, a.allocated_to
             FROM sales.customer_account_allocation a LEFT JOIN sales.employee e USING (employee_id)
             JOIN sales.customer c USING (customer_id) WHERE c.legal_name = %s ORDER BY a.allocated_from""",
        (CUSTOMER,),
    ).fetchall()
    return [(r["owner"], r["allocated_from"], r["allocated_to"]) for r in rows]


def _london_date(conn: Connection, days_from_now: int) -> date:
    row = conn.execute(
        "SELECT ((now() + make_interval(days => %s)) AT TIME ZONE 'Europe/London')::date AS d",
        (days_from_now,),
    ).fetchone()
    assert row is not None
    value: date = row["d"]
    return value


# ------------------------------------------------------------------ parsing


def test_parser_keeps_active_groups_and_their_contacts() -> None:
    groups = parse_contact_groups(fixture_json("test_xero_contact_groups.json"))
    assert [g.name for g in groups] == [
        "a. Test Territory",
        "b. Test ISAM",
        "z. House",
        "Direct Debit customers",
    ]  # the DELETED group is dropped
    assert [str(c.contact_id) for c in groups[0].contacts] == [CONTACT]


@pytest.mark.parametrize(
    ("doc", "message"),
    [
        ({"Contacts": []}, "'ContactGroups' list"),
        ({"ContactGroups": [{"Name": "a. Sales Person"}]}, "without ContactGroupID"),
        ({"ContactGroups": [{"ContactGroupID": "nope", "Name": "a. Sales Person"}]}, "not a Xero id"),
        (
            {
                "ContactGroups": [
                    {"ContactGroupID": CONTACT, "Name": "a. Sales Person", "Contacts": [{"Name": "x"}]}
                ]
            },
            "contact without ContactID",
        ),
    ],
)
def test_parser_rejects_a_response_that_is_not_xeros(doc: dict[str, Any], message: str) -> None:
    with pytest.raises(XeroFormatError, match=message):
        parse_contact_groups(doc)


def test_live_fetch_authenticates_and_reads_each_group() -> None:
    fixture = fixture_json("test_xero_contact_groups.json")
    calls: list[urllib.request.Request] = []

    def transport(request: urllib.request.Request) -> bytes:
        calls.append(request)
        if request.full_url.endswith("/connect/token"):
            return json.dumps({"access_token": "tok"}).encode()
        if request.full_url.endswith("/ContactGroups"):
            listing = [{k: v for k, v in g.items() if k != "Contacts"} for g in fixture["ContactGroups"]]
            return json.dumps({"ContactGroups": listing}).encode()
        group_id = request.full_url.rsplit("/", 1)[1]
        return json.dumps(
            {"ContactGroups": [g for g in fixture["ContactGroups"] if g["ContactGroupID"] == group_id]}
        ).encode()

    groups, raw = fetch_contact_groups(XeroCredentials("id", "secret"), transport)

    token_request = calls[0]
    assert token_request.get_header("Authorization") == "Basic " + base64.b64encode(b"id:secret").decode()
    assert b"grant_type=client_credentials" in (token_request.data or b"")  # type: ignore[operator]
    assert all(c.get_header("Authorization") == "Bearer tok" for c in calls[1:])
    # one listing call, then one call per ACTIVE group (the deleted group is never fetched)
    assert len(calls) == 2 + 4
    assert [str(c.contact_id) for c in groups[0].contacts] == [CONTACT]
    assert len(raw["ContactGroups"]) == 4


# ------------------------------------------------------------------ reconciliation


def test_first_sync_that_agrees_changes_nothing(conn: Connection, aged_master_data: MasterDataIn) -> None:
    before = _owners(conn)
    result = _sync(conn, _groups(), days_from_now=-1)
    assert _status(conn) == ("MATCH", "Test Territory Manager", "Test Territory Manager")
    assert result["applied"] == []
    assert result["differences"] == []
    assert result["unmapped_groups"] == ["Direct Debit customers"]  # non-owner groups are ignored
    assert _owners(conn) == before


def test_first_sync_that_differs_waits_for_cfo_then_adopts_xero(
    conn: Connection, aged_master_data: MasterDataIn
) -> None:
    moved = _groups({TERRITORY_GROUP: [], ISAM_GROUP: [CONTACT]})
    result = _sync(conn, moved, days_from_now=-5)
    assert _status(conn) == ("DIFFERS", "Test Territory Manager", "Test Internal Sales")
    assert result["applied"] == []  # baseline: nothing shows which side is newer

    result = _sync(conn, moved, days_from_now=-1, adopt_xero=True)
    first_seen = _london_date(conn, -5)  # dated from the first sync that saw it
    assert [a["result"] for a in result["applied"]] == ["ownership set"]
    assert _owners(conn) == [
        ("Test Territory Manager", date(2026, 1, 1), first_seen - timedelta(days=1)),
        ("Test Internal Sales", first_seen, None),
    ]
    source = conn.execute(
        "SELECT source FROM sales.customer_account_allocation WHERE allocated_to IS NULL"
    ).fetchone()
    assert source is not None
    assert "adopted on CFO instruction" in source["source"]
    assert _status(conn)[0] == "MATCH"


def test_later_xero_change_is_adopted_automatically(conn: Connection, aged_master_data: MasterDataIn) -> None:
    _sync(conn, _groups(), days_from_now=-5)
    result = _sync(conn, _groups({TERRITORY_GROUP: [], HOUSE_GROUP: [CONTACT]}), days_from_now=-3)
    changed_on = _london_date(conn, -3)
    assert result["xero_owner_changes"] == 1
    assert [(a["xero_owner"], a["result"]) for a in result["applied"]] == [("House", "ownership set")]
    assert _owners(conn)[-2:] == [
        ("Test Territory Manager", date(2026, 1, 1), changed_on - timedelta(days=1)),
        ("House", changed_on, None),
    ]
    assert _status(conn)[0] == "MATCH"


def test_database_change_after_sync_tells_finance_to_update_xero(
    conn: Connection, aged_master_data: MasterDataIn
) -> None:
    _sync(conn, _groups(), days_from_now=-2)  # Xero read two days ago: agreed
    service.allocate_account(conn, CUSTOMER, "test.isam@example.com", date(2026, 6, 1), "CFO")
    assert _status(conn)[0] == "DB_CHANGED"
    # the next sync (Xero not yet updated) must not undo the database change
    result = _sync(conn, _groups(), days_from_now=-1)
    assert result["applied"] == []
    assert [(d["status"], d["action"]) for d in result["differences"]] == [
        (
            "DB_CHANGED",
            "Move the Xero contact to the group for Test Internal Sales (CFO)",
        )
    ]


def test_xero_group_of_a_leaver_is_not_applied(conn: Connection, aged_master_data: MasterDataIn) -> None:
    _sync(conn, _groups(), days_from_now=-2)
    service.employee_leaves(conn, "test.territory@example.com", date(2026, 8, 31))
    result = _sync(conn, _groups(), days_from_now=-1)
    assert _status(conn) == ("XERO_OWNER_HAS_LEFT", "House", "Test Territory Manager")
    assert result["applied"] == []


@pytest.mark.parametrize(
    ("members", "status"),
    [
        ({TERRITORY_GROUP: []}, "NOT_IN_XERO_OWNER_GROUP"),
        ({ISAM_GROUP: [CONTACT]}, "MULTIPLE_XERO_OWNER_GROUPS"),
    ],
)
def test_contact_must_be_in_exactly_one_owner_group(
    conn: Connection, aged_master_data: MasterDataIn, members: dict[int, list[str]], status: str
) -> None:
    result = _sync(conn, _groups(members), days_from_now=-1, adopt_xero=True)
    assert _status(conn)[0] == status
    assert result["applied"] == []  # an ambiguous Xero record is never applied


def test_xero_contact_that_is_not_a_customer_is_reported(
    conn: Connection, aged_master_data: MasterDataIn
) -> None:
    stranger = "00000000-0000-4000-8000-00000000c999"
    result = _sync(conn, _groups({ISAM_GROUP: [stranger]}), days_from_now=-1)
    assert [(str(u["xero_contact_id"]), u["group_name"]) for u in result["unmatched_contacts"]] == [
        (stranger, "b. Test ISAM")
    ]


def test_customer_without_xero_contact_is_reported(conn: Connection, aged_master_data: MasterDataIn) -> None:
    conn.execute("UPDATE sales.customer SET xero_contact_id = NULL")
    _sync(conn, _groups(), days_from_now=-1)
    assert _status(conn)[0] == "NO_XERO_CONTACT"


# ------------------------------------------------------------------ exception rule


def test_mismatch_warns_on_open_orders_only(
    conn: Connection, aged_master_data: MasterDataIn, order_json: dict[str, Any]
) -> None:
    number = load(conn, order_json).order_number
    assert service.order_exceptions(conn, number) == []  # not synced yet: no warning

    _sync(conn, _groups({TERRITORY_GROUP: [], ISAM_GROUP: [CONTACT]}), days_from_now=-5)
    rules = [(e["rule_code"], e["severity"]) for e in service.order_exceptions(conn, number)]
    assert rules == [("XERO_OWNER_MISMATCH", "warning")]

    _sync(conn, _groups({TERRITORY_GROUP: [], ISAM_GROUP: [CONTACT]}), days_from_now=-1, adopt_xero=True)
    # Xero adopted: the order's salesperson is now not the owner (the 0010 rule), but Xero agrees
    assert "XERO_OWNER_MISMATCH" not in {e["rule_code"] for e in service.order_exceptions(conn, number)}
