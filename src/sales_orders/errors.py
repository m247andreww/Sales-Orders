"""Domain errors. Anything raised here means the input was rejected and nothing was written."""

from __future__ import annotations


class SalesOrderError(Exception):
    """Base class for all sales order errors."""


class UnknownReferenceError(SalesOrderError):
    """The submission refers to master data (customer, supplier, employee...) that does not exist.

    Master data is never created implicitly from an order: a typo in a customer name must
    fail loudly rather than create a duplicate customer.
    """

    def __init__(self, kind: str, key: str) -> None:
        super().__init__(f"unknown {kind}: {key!r} (load it as master data first)")
        self.kind = kind
        self.key = key


class OrderNotFoundError(SalesOrderError):
    def __init__(self, order_number: str) -> None:
        super().__init__(f"order {order_number!r} not found")
        self.order_number = order_number
