"""First figures: the CFO enters bureau limits read from the portals (migration 0023).

All companies, names and figures are synthetic.
"""

from __future__ import annotations

from decimal import Decimal
from uuid import UUID

import pytest
from conftest import CFO, FINANCE, act_as, savepoint_rejects

from sales_orders import credit, credit_exposure
from sales_orders.credit import BureauFigure
from sales_orders.db import Connection
from sales_orders.errors import SalesOrderError
from sales_orders.models import CreditSubjectIn, CustomerIn, MasterDataIn
from sales_orders.service import load_master_data
from sales_orders.xero import XeroDirectoryContact

ACME = UUID("00000000-0000-4000-8000-0000000f1001")
BETA = UUID("00000000-0000-4000-8000-0000000f1002")


def _setup(conn: Connection) -> None:
    credit.sync_xero_contacts(
        conn,
        [
            XeroDirectoryContact(ACME, "Acme Widgets Ltd", "01234567", True, False, "ACTIVE"),
            XeroDirectoryContact(BETA, "Beta Services Ltd", "07654321", True, False, "ACTIVE"),
        ],
    )
    load_master_data(
        conn,
        MasterDataIn(
            customers=(
                CustomerIn(legal_name="Acme Widgets", arr_prefix="ACM", xero_contact_id=ACME),
                CustomerIn(legal_name="Beta Services", arr_prefix="BET", xero_contact_id=BETA),
            ),
            credit_subjects=(
                CreditSubjectIn(
                    display_name="Acme Widgets",
                    relationship="customer",
                    customer_legal_name="Acme Widgets",
                    company_number="01234567",
                ),
                CreditSubjectIn(
                    display_name="Beta Services",
                    relationship="customer",
                    customer_legal_name="Beta Services",
                    company_number="07654321",
                ),
            ),
        ),
    )
    credit_exposure.load_receivables(
        conn,
        {
            "as_of": "2026-10-02",
            "source": "first figures test",
            "contacts": [
                {
                    "xero_contact_id": None,
                    "name": n,
                    "current": cur,
                    "overdue": "0.00",
                    "overdue_over_60": "0.00",
                    "oldest_due_date": None,
                    "invoice_count": 1,
                }
                for n, cur in (("Acme Widgets Ltd", "500.00"), ("Beta Services Ltd", "9000.00"))
            ],
        },
    )


def _position(conn: Connection, company: str, bureau: str) -> dict[str, object] | None:
    return conn.execute(
        """SELECT p.credit_limit, p.risk_band FROM sales.v_credit_bureau_position p
             JOIN sales.credit_subject s USING (credit_subject_id)
            WHERE s.display_name = %s AND p.bureau_code = %s""",
        (company, bureau),
    ).fetchone()


def test_clients_without_figures_are_listed_largest_owed_first(
    conn: Connection, master_data: MasterDataIn
) -> None:
    _setup(conn)
    listed = [
        (r["company"], r["owed"], r["needs"])
        for r in credit.first_figures_needed(conn)
        if r["company"] in ("Acme Widgets", "Beta Services")
    ]
    assert listed == [
        ("Beta Services", "9000.00", ["experian", "creditsafe"]),
        ("Acme Widgets", "500.00", ["experian", "creditsafe"]),
    ]


def test_entered_figures_become_bureau_readings_and_leave_the_list(
    conn: Connection, master_data: MasterDataIn
) -> None:
    _setup(conn)
    act_as(conn, CFO)
    credit.enter_bureau_figures(
        conn,
        "Beta Services",
        {"experian": BureauFigure(Decimal("120000"), "Low Risk"), "creditsafe": BureauFigure(None)},
    )
    assert _position(conn, "Beta Services", "experian") == {
        "credit_limit": Decimal("120000.00"),
        "risk_band": "Low Risk",
    }
    assert _position(conn, "Beta Services", "creditsafe") == {"credit_limit": None, "risk_band": None}
    row = conn.execute(
        "SELECT entered_by FROM sales.credit_report WHERE source_code = 'cfo_entry' AND bureau_code = 'experian'"
    ).fetchone()
    assert row == {"entered_by": CFO}
    assert "Beta Services" not in [r["company"] for r in credit.first_figures_needed(conn)]


def test_one_bureau_only_keeps_the_client_listed_for_the_other(
    conn: Connection, master_data: MasterDataIn
) -> None:
    _setup(conn)
    act_as(conn, CFO)
    credit.enter_bureau_figures(conn, "Acme Widgets", {"creditsafe": BureauFigure(Decimal("40000"))})
    acme = [r for r in credit.first_figures_needed(conn) if r["company"] == "Acme Widgets"]
    assert [(r["needs"], r["creditsafe"]) for r in acme] == [(["experian"], "40000.00")]


def test_entered_figures_make_the_client_due_for_assessment(
    conn: Connection, master_data: MasterDataIn
) -> None:
    _setup(conn)
    act_as(conn, CFO)
    due_before = {
        r["display_name"] for r in conn.execute("SELECT display_name FROM sales.v_credit_assessment_due")
    }
    credit.enter_bureau_figures(conn, "Acme Widgets", {"experian": BureauFigure(Decimal("30000"))})
    due_after = {
        r["display_name"] for r in conn.execute("SELECT display_name FROM sales.v_credit_assessment_due")
    }
    assert "Acme Widgets" in due_after - due_before


def test_only_the_credit_approver_may_enter_figures(conn: Connection, master_data: MasterDataIn) -> None:
    _setup(conn)
    act_as(conn, FINANCE)
    sid = conn.execute(
        "SELECT credit_subject_id FROM sales.credit_subject WHERE display_name = 'Acme Widgets'"
    ).fetchone()
    assert sid is not None
    msg = savepoint_rejects(
        conn,
        """INSERT INTO sales.credit_report (bureau_code, credit_subject_id, company_name, observed_at,
                                            source_code, limit_status, credit_limit)
           VALUES ('experian', %s, 'Acme Widgets', now(), 'cfo_entry', 'value', 1000)""",
        (sid["credit_subject_id"],),
    )
    assert "entering bureau figures is not permitted" in msg


def test_database_refuses_an_unknown_band(conn: Connection, master_data: MasterDataIn) -> None:
    _setup(conn)
    act_as(conn, CFO)
    sid = conn.execute(
        "SELECT credit_subject_id FROM sales.credit_subject WHERE display_name = 'Acme Widgets'"
    ).fetchone()
    assert sid is not None
    msg = savepoint_rejects(
        conn,
        """INSERT INTO sales.credit_report (bureau_code, credit_subject_id, company_name, observed_at,
                                            source_code, limit_status, credit_limit, risk_band)
           VALUES ('experian', %s, 'Acme Widgets', now(), 'cfo_entry', 'value', 1000, 'Pretty Safe')""",
        (sid["credit_subject_id"],),
    )
    assert "unknown experian band" in msg


def test_database_refuses_a_cfo_entry_with_no_limit_status(
    conn: Connection, master_data: MasterDataIn
) -> None:
    _setup(conn)
    act_as(conn, CFO)
    sid = conn.execute(
        "SELECT credit_subject_id FROM sales.credit_subject WHERE display_name = 'Acme Widgets'"
    ).fetchone()
    assert sid is not None
    msg = savepoint_rejects(
        conn,
        """INSERT INTO sales.credit_report (bureau_code, credit_subject_id, company_name, observed_at,
                                            source_code, limit_status)
           VALUES ('experian', %s, 'Acme Widgets', now(), 'cfo_entry', 'not_reported')""",
        (sid["credit_subject_id"],),
    )
    assert "credit_report_cfo_entry_limit_check" in msg


@pytest.mark.parametrize(
    ("figures", "error"),
    [
        ({}, "at least one"),
        ({"dun": BureauFigure(Decimal(1))}, "unknown bureau"),
        ({"experian": BureauFigure(Decimal(-1))}, "0 or more"),
        ({"experian": BureauFigure(1000.0)}, "amount of money"),  # type: ignore[arg-type]
        ({"creditsafe": BureauFigure(Decimal(1), "Low Risk")}, "only Experian"),
    ],
)
def test_python_refuses_bad_figures(
    conn: Connection, master_data: MasterDataIn, figures: dict[str, BureauFigure], error: str
) -> None:
    _setup(conn)
    act_as(conn, CFO)
    with pytest.raises(SalesOrderError, match=error):
        credit.enter_bureau_figures(conn, "Acme Widgets", figures)


def test_bands_are_in_experian_order(conn: Connection) -> None:
    bands = credit.experian_bands(conn)
    assert bands[0] == "Very Low Risk"
    assert bands.index("Low Risk") < bands.index("High Risk") < bands.index("Serious Adverse Information")


def test_desk_lists_first_figures_once_not_also_under_attention(
    conn: Connection, master_data: MasterDataIn
) -> None:
    from datetime import UTC, datetime  # noqa: PLC0415

    _setup(conn)
    desk = credit.desk_export(conn, datetime(2026, 1, 1, tzinfo=UTC))
    assert "Beta Services" in [f["company"] for f in desk["first_figures"]]
    assert desk["risk_bands"][0] == "Very Low Risk"
    missing = [a for a in desk["attention"] if a["rule"] in ("SINGLE_BUREAU", "NO_BUREAU_LIMIT")]
    assert [a for a in missing if a["company"] in ("Acme Widgets", "Beta Services")] == []
    act_as(conn, CFO)
    credit.enter_bureau_figures(conn, "Beta Services", {"experian": BureauFigure(Decimal("50000"))})
    desk = credit.desk_export(conn, datetime(2026, 1, 1, tzinfo=UTC))
    beta = [f for f in desk["first_figures"] if f["company"] == "Beta Services"]
    assert [f["needs"] for f in beta] == [["creditsafe"]]


def test_a_desk_request_is_applied_once(conn: Connection, master_data: MasterDataIn) -> None:
    assert credit.claim_desk_request(conn, "abc123", "figures", "Acme Widgets") is True
    assert (
        credit.claim_desk_request(conn, "abc123", "figures", "Acme Widgets") is False
    )  # a second job skips it
    assert credit.claim_desk_request(conn, "def456", "decision", "Acme Widgets") is True
    assert credit.claim_desk_request(conn, None, "decision", "Acme Widgets") is True  # session change: no id


def test_desk_request_log_is_append_only(conn: Connection, master_data: MasterDataIn) -> None:
    credit.claim_desk_request(conn, "abc123", "figures", "Acme Widgets")
    msg = savepoint_rejects(conn, "DELETE FROM sales.credit_desk_request WHERE request_id = 'abc123'")
    assert "append-only" in msg


@pytest.mark.parametrize("command", ["credit-decide", "credit-enter-figures", "credit-add-monitored"])
def test_desk_commands_take_the_request_id(command: str) -> None:
    from sales_orders import cli  # noqa: PLC0415

    argv = {
        "credit-decide": [command, "Acme", "1000", "--reason", "r", "--review-by", "2099-01-01"],
        "credit-enter-figures": [command, "Acme", "--experian", "1000"],
        "credit-add-monitored": [command, "ACM", "--number", "01234567"],
    }[command]
    assert cli.build_parser().parse_args([*argv, "--request", "abc123"]).request == "abc123"
