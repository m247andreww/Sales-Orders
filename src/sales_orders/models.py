"""Validated input models.

These describe what arrives from outside the database (an order submission parsed from
the New Orders mailbox, or master data). They are validated *before* anything touches
the database; the database then enforces its own constraints as a second line of defence.

Money rule: amounts must arrive as strings or Decimals, never floats, so no value is
ever rounded by binary floating point on the way in.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Any
from uuid import UUID

from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    EmailStr,
    Field,
    StringConstraints,
    model_validator,
)


def _reject_float(value: Any) -> Any:
    if isinstance(value, float):
        raise ValueError("monetary and rate values must be given as strings, not floats")
    return value


def _require_aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("timestamps must include a timezone offset")
    return value


NonEmpty = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
CurrencyCode = Annotated[str, StringConstraints(pattern=r"^[A-Z]{3}$")]
Sha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
Money = Annotated[Decimal, BeforeValidator(_reject_float), Field(ge=0, max_digits=18, decimal_places=4)]
StatedTotal = Annotated[Decimal, BeforeValidator(_reject_float), Field(max_digits=18, decimal_places=2)]
Quantity = Annotated[Decimal, BeforeValidator(_reject_float), Field(gt=0, max_digits=12, decimal_places=4)]
Rate = Annotated[Decimal, BeforeValidator(_reject_float), Field(gt=0, max_digits=18, decimal_places=8)]
AwareDatetime = Annotated[datetime, AfterValidator(_require_aware)]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


# --------------------------------------------------------------------------- master data


class EmployeeIn(StrictModel):
    email: EmailStr
    full_name: NonEmpty
    job_title: str | None = None


class CreditTermsIn(StrictModel):
    effective_from: date
    effective_to: date | None = None
    recurring_terms_days: int = Field(ge=0, le=180)
    recurring_payment_method: NonEmpty
    one_off_terms_days: int = Field(ge=0, le=180)
    one_off_prepayment_required: bool = False
    credit_limit: Money | None = None
    risk_rating: NonEmpty = "standard"
    is_non_standard: bool = False
    reason: str | None = None
    approved_by_email: EmailStr | None = None
    approved_at: AwareDatetime | None = None


class ContactIn(StrictModel):
    email: EmailStr
    full_name: str | None = None
    role_description: str | None = None


class TenantIn(StrictModel):
    tenant_guid: UUID
    primary_domain: NonEmpty


class CustomerIn(StrictModel):
    legal_name: NonEmpty
    trading_name: str | None = None
    company_number: Annotated[str, StringConstraints(pattern=r"^[A-Z0-9]{8}$")] | None = None
    xero_contact_id: UUID | None = None
    notes: str | None = None
    credit_terms: tuple[CreditTermsIn, ...] = ()
    contacts: tuple[ContactIn, ...] = ()
    m365_tenants: tuple[TenantIn, ...] = ()


class SupplierIn(StrictModel):
    name: NonEmpty
    is_internal: bool = False
    account_status: NonEmpty = "approved"
    payment_terms_days: int | None = Field(default=None, ge=0, le=180)
    default_currency: CurrencyCode = "GBP"
    xero_contact_id: UUID | None = None


class FxRateIn(StrictModel):
    from_currency: CurrencyCode
    to_currency: CurrencyCode
    rate_date: date
    rate: Rate
    source: NonEmpty


class MasterDataIn(StrictModel):
    employees: tuple[EmployeeIn, ...] = ()
    suppliers: tuple[SupplierIn, ...] = ()
    customers: tuple[CustomerIn, ...] = ()
    fx_rates: tuple[FxRateIn, ...] = ()


# --------------------------------------------------------------------------- order submission


class SourceEmailIn(StrictModel):
    mailbox: EmailStr
    internet_message_id: NonEmpty
    graph_message_id: str | None = None
    conversation_id: str | None = None
    subject: NonEmpty
    sender_email: EmailStr
    received_at: AwareDatetime
    body_text: str | None = None


class DocumentIn(StrictModel):
    document_type: NonEmpty
    file_name: NonEmpty
    content_type: NonEmpty
    size_bytes: int = Field(ge=0)
    sha256: Sha256
    storage_uri: NonEmpty
    pandadoc_document_id: str | None = None


class SupplierQuoteIn(StrictModel):
    supplier_name: NonEmpty
    supplier_reference: NonEmpty
    quote_date: date | None = None
    valid_until: date | None = None
    currency: CurrencyCode
    document_sha256: Sha256 | None = None


class FxRateRef(StrictModel):
    """Identifies a rate already held in sales.fx_rate; lines never carry a typed-in rate."""

    rate_date: date
    source: NonEmpty


class OrderLineIn(StrictModel):
    sku: str | None = None
    description: NonEmpty
    line_category: NonEmpty
    supplier_name: NonEmpty
    supplier_quote_reference: str | None = None
    quantity: Quantity
    billing_frequency: NonEmpty
    billing_periods: int = Field(gt=0)
    cost_currency: CurrencyCode = "GBP"
    unit_cost: Money  # in cost_currency, per billing period
    fx_rate: FxRateRef | None = None
    unit_sell: Money  # in order currency, per billing period
    margin_rationale: str | None = None  # required (here or at order level) if sold below cost
    notes: str | None = None

    @model_validator(mode="after")
    def _one_off_has_one_period(self) -> OrderLineIn:
        if self.billing_frequency == "one_off" and self.billing_periods != 1:
            raise ValueError("one_off lines must have billing_periods = 1")
        return self


class CheckIn(StrictModel):
    check_type: NonEmpty
    notes: str | None = None


class StatedTotalsIn(StrictModel):
    """Totals as typed in the submission email: used only for reconciliation."""

    net_cost: StatedTotal
    net_sell: StatedTotal
    gross_margin: StatedTotal


class OrderSubmissionIn(StrictModel):
    source_email: SourceEmailIn
    source_sequence: int = Field(default=1, gt=0)
    customer_legal_name: NonEmpty
    title: NonEmpty
    order_type: NonEmpty
    currency: CurrencyCode = "GBP"
    quote_reference: str | None = None
    pandadoc_document_id: str | None = None
    customer_po_reference: str | None = None
    price_list_name: str | None = None
    m365_tenant_guid: UUID | None = None
    signed_date: date | None = None
    submitted_by_email: EmailStr
    account_manager_email: EmailStr | None = None
    is_expedited: bool = False
    margin_exception_reason: str | None = None  # order-level rationale for any loss
    stated_totals: StatedTotalsIn | None = None
    notes: str | None = None
    documents: tuple[DocumentIn, ...] = ()
    supplier_quotes: tuple[SupplierQuoteIn, ...] = ()
    lines: tuple[OrderLineIn, ...] = Field(min_length=1)
    checks: tuple[CheckIn, ...] = ()

    @model_validator(mode="after")
    def _supplier_quotes_resolve(self) -> OrderSubmissionIn:
        quotes = {(q.supplier_name.lower(), q.supplier_reference) for q in self.supplier_quotes}
        for number, line in enumerate(self.lines, start=1):
            ref = line.supplier_quote_reference
            if ref is not None and (line.supplier_name.lower(), ref) not in quotes:
                raise ValueError(
                    f"line {number}: supplier quote {ref!r} for {line.supplier_name!r} "
                    "is not listed in supplier_quotes"
                )
            if (line.cost_currency == self.currency) != (line.fx_rate is None):
                raise ValueError(
                    f"line {number}: fx_rate is required exactly when cost currency "
                    f"({line.cost_currency}) differs from order currency ({self.currency})"
                )
        return self
