"""Tests for email_checker._extract_order_code."""

from app.channels.email_checker import _extract_order_code


class TestExtractOrderCode:
    def test_with_dash(self):
        assert _extract_order_code("Чертеж МКП-1079") == "МКП-1079"

    def test_without_dash(self):
        assert _extract_order_code("Заказ МКП1079") == "МКП1079"

    def test_no_code(self):
        assert _extract_order_code("No order here") is None

    def test_complex_code(self):
        # Extracts ЛКПФ1006 (uppercase prefix + digits), lower case suffix not captured
        result = _extract_order_code("ЛКПФ1006обрА5 чертеж")
        assert result == "ЛКПФ1006"

    def test_standard_code(self):
        assert _extract_order_code("АР-236 замер") == "АР-236"

    def test_multi_code_first(self):
        """First code in a multi-order string."""
        assert _extract_order_code("ИП-1115, 1116") == "ИП-1115"
