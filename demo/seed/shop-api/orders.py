"""Tiny fake order service used by the workflow demo."""

ORDERS = {
    "A100": {"id": "A100", "status": "shipped", "items": ["mug"]},
    "A101": {"id": "A101", "status": "processing", "items": ["poster"]},
}


def get_order(order_id):
    """Return a copy of the order, or raise KeyError."""
    return dict(ORDERS[order_id])

