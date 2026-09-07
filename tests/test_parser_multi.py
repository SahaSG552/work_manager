"""Tests for parser.parse_message_multi — multi-order parsing, folder names."""

import pytest
from app.parser import parse_message_multi


class TestMultiOrder:
    """Multi-order: 'ИП-1115, 1116' → two orders with shared folder."""

    def test_two_codes_with_dash(self):
        orders, folder = parse_message_multi("ИП-1115, 1116 заказ")
        assert len(orders) == 2
        assert orders[0].order_code == "ИП-1115"
        assert orders[1].order_code == "ИП-1116"
        assert folder == "ИП-1115, 1116"

    def test_three_codes(self):
        orders, folder = parse_message_multi("ЧМ-1102, 1103, 1104")
        assert len(orders) == 3
        assert orders[0].order_code == "ЧМ-1102"
        assert orders[1].order_code == "ЧМ-1103"
        assert orders[2].order_code == "ЧМ-1104"
        assert folder == "ЧМ-1102, 1103, 1104"

    def test_codes_without_dash(self):
        orders, folder = parse_message_multi("МСЛ1105, 1106")
        assert len(orders) == 2
        assert orders[0].order_code == "МСЛ1105"
        assert orders[1].order_code == "МСЛ1106"
        assert folder == "МСЛ1105, 1106"

    def test_single_order_no_folder(self):
        orders, folder = parse_message_multi("ЧМ-1102 столешка")
        assert len(orders) == 1
        assert folder == ""

    def test_shared_data(self):
        """All orders in a multi-order share the same parsed data (except order_code)."""
        orders, _ = parse_message_multi("ИП-1115, 1116 толщина 40мм")
        assert orders[0].thickness == orders[1].thickness
        assert orders[0].owner == orders[1].owner

    def test_folder_name_prefix_once(self):
        """Folder name shows prefix only once: 'ИП-1115, 1116' not 'ИП-1115, ИП-1116'."""
        _, folder = parse_message_multi("ИП-1115, 1116")
        assert folder == "ИП-1115, 1116"
        assert "ИП-1115, ИП-1116" != folder

    def test_suffix_codes_multi(self):
        """Multi-order with suffix codes."""
        orders, folder = parse_message_multi("ЛММФ306/А1, 307/Б2 фасад")
        assert len(orders) == 2
        assert orders[0].order_code == "ЛММФ306/А1"
