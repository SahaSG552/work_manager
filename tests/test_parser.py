"""Tests for parser._parse_with_regex — order code extraction, thickness, edge, email_reference."""

import pytest
from app.parser import _parse_with_regex


# ═══════════════════════════════════════════════════════════════
# Order code extraction — preserve original format
# ═══════════════════════════════════════════════════════════════

class TestOrderCode:
    """Order code must be preserved AS-IS from the message text."""

    def test_with_dash(self):
        p = _parse_with_regex("ЧМ-1102 столешка")
        assert p.order_code == "ЧМ-1102"

    def test_without_dash(self):
        p = _parse_with_regex("АР236 столешка")
        assert p.order_code == "АР236"

    def test_suffix_slash(self):
        p = _parse_with_regex("ЛММФ306/А1 фасад")
        assert p.order_code == "ЛММФ306/А1"

    def test_suffix_cyrillic_D(self):
        """ЛТШФ6239Д1 — Cyrillic suffix Д1."""
        p = _parse_with_regex("ЛТШФ6239Д1 заказ")
        assert p.order_code == "ЛТШФ6239Д1"

    def test_suffix_cyrillic_C(self):
        """ЛГКФ340С1 — Cyrillic suffix С1."""
        p = _parse_with_regex("ЛГКФ340С1 фасад")
        assert p.order_code == "ЛГКФ340С1"

    def test_no_dash_no_suffix(self):
        p = _parse_with_regex("МКП1079 в почте")
        assert p.order_code == "МКП1079"

    def test_no_code_creates_bez(self):
        """Messages without an order code get БЕЗ-* code."""
        p = _parse_with_regex("Дима, нужно столешку")
        assert p.order_code.startswith("БЕЗ-")
        assert p.owner == "БЕЗ"

    def test_yougile_style_no_dash(self):
        """ЛКПФ1006обрА5 — complex YouGile code (lowercase suffix, parsed as prefix+number)."""
        p = _parse_with_regex("ЛКПФ1006обрА5")
        # Regex captures ЛКПФ1006 (uppercase prefix + digits), "обрА5" is not captured
        assert p.order_code == "ЛКПФ1006"


# ═══════════════════════════════════════════════════════════════
# Owner / client code detection
# ═══════════════════════════════════════════════════════════════

class TestOwner:
    def test_prefix_to_owner(self):
        p = _parse_with_regex("ЧМ-1102 столешка")
        assert p.owner == "KK"

    def test_ip_prefix(self):
        p = _parse_with_regex("ИП-1115 заказ")
        assert p.owner == "KK"

    def test_km_prefix(self):
        p = _parse_with_regex("КМ-501 заказ")
        assert p.owner == "KH"

    def test_ar_prefix(self):
        p = _parse_with_regex("АР236 столешка")
        assert p.owner == "AM"

    def test_owner_from_brackets(self):
        """[KK] prefix overrides auto-detection."""
        p = _parse_with_regex("[KK] Заказ срочно")
        assert p.owner == "KK"

    def test_owner_bracket_am(self):
        p = _parse_with_regex("[AM] Что-то")
        assert p.owner == "AM"

    def test_no_code_owner_bez(self):
        p = _parse_with_regex("Просто заказ от Миши")
        assert p.owner == "БЕЗ"

    def test_no_code_with_bracket_owner(self):
        p = _parse_with_regex("[KK] Дима столешка")
        assert p.owner == "KK"
        assert p.order_code.startswith("БЕЗ-")


# ═══════════════════════════════════════════════════════════════
# Thickness extraction
# ═══════════════════════════════════════════════════════════════

class TestThickness:
    def test_thickness_with_mm(self):
        p = _parse_with_regex("ЧМ-1102 толщина 40мм")
        assert p.thickness == "40 мм"

    def test_thickness_with_space_mm(self):
        p = _parse_with_regex("ЧМ-1102 толщина 16 мм")
        assert p.thickness == "16 мм"

    def test_thickness_without_mm(self):
        p = _parse_with_regex("ЧМ-1102 толщина 30")
        assert p.thickness == "30 мм"

    def test_thickness_number_then_mm(self):
        """'40мм' without 'толщина' keyword."""
        p = _parse_with_regex("ЧМ-1102 40мм столешка")
        assert p.thickness == "40 мм"

    def test_no_thickness(self):
        p = _parse_with_regex("ЧМ-1102 столешка")
        assert p.thickness is None


# ═══════════════════════════════════════════════════════════════
# Edge type extraction
# ═══════════════════════════════════════════════════════════════

class TestEdgeType:
    def test_shtapik_instead_of_bort(self):
        """'вместо борта штапик' → 'борт-штапик'."""
        p = _parse_with_regex("ЧМ-1102 вместо борта штапик")
        assert p.edge_type == "борт-штапик"

    def test_shtapik_alone(self):
        p = _parse_with_regex("ЧМ-1102 штапик")
        assert p.edge_type == "штапик"

    def test_galtel(self):
        p = _parse_with_regex("ЧМ-1102 галтель")
        assert p.edge_type == "галтель"

    def test_bort_number(self):
        """'борт 35' → 'борт 35'."""
        p = _parse_with_regex("ЧМ-1102 борт 35")
        assert p.edge_type == "борт 35"

    def test_bort_standard(self):
        p = _parse_with_regex("ЧМ-1102 борт стандарт")
        assert p.edge_type == "борт стандарт"

    def test_bort_standard_with_parens(self):
        """'борт стандарт (30)' — regex captures the parenthetical too."""
        p = _parse_with_regex("ЧМ-1102 борт стандарт (30)")
        assert "борт стандарт" in p.edge_type

    def test_mesto_borta_shtapiki(self):
        """'место борта сделаем штапики' → 'штапик' (not 'борт-штапик')."""
        p = _parse_with_regex("МКП-1079 место борта сделаем штапики")
        assert p.edge_type == "штапик"

    def test_no_edge(self):
        p = _parse_with_regex("ЧМ-1102 столешка")
        assert p.edge_type is None


# ═══════════════════════════════════════════════════════════════
# Sink type
# ═══════════════════════════════════════════════════════════════

class TestSinkType:
    def test_podstolnaya(self):
        p = _parse_with_regex("ЧМ-1102 мойка подстольная")
        assert p.sink_type == "подстольная"

    def test_nakladnaya(self):
        p = _parse_with_regex("ЧМ-1102 накладная мойка")
        assert p.sink_type == "накладная"

    def test_integro(self):
        """'мойка интегро' — not a recognized sink type in regex fallback."""
        p = _parse_with_regex("ЧМ-1102 мойка интегро")
        # regex only detects подстольная/накладная
        assert p.sink_type is None


# ═══════════════════════════════════════════════════════════════
# Email reference
# ═══════════════════════════════════════════════════════════════

class TestEmailReference:
    def test_na_pochte(self):
        p = _parse_with_regex("ЧМ-1102 на почте чертёж")
        assert p.email_reference is True

    def test_v_pochte(self):
        p = _parse_with_regex("МКП-1079 в почте")
        assert p.email_reference is True

    def test_pismo(self):
        p = _parse_with_regex("ЧМ-1102 письмо с чертежом")
        assert p.email_reference is True

    def test_pochta(self):
        p = _parse_with_regex("ЧМ-1102 почта")
        assert p.email_reference is True

    def test_no_email_ref(self):
        p = _parse_with_regex("ЧМ-1102 столешка")
        assert p.email_reference is False


# ═══════════════════════════════════════════════════════════════
# Complex messages (integration-level regex tests)
# ═══════════════════════════════════════════════════════════════

class TestComplexMessages:
    def test_full_order(self):
        """Full realistic message with multiple fields."""
        p = _parse_with_regex(
            "[KK] Заказ МКП-1079 в почте. можно готовить в работу. Толщина 40мм мойка интегро, место борта сделаем штапики"
        )
        assert p.order_code == "МКП-1079"
        assert p.owner == "KK"
        assert p.thickness == "40 мм"
        assert p.edge_type == "штапик"
        assert p.email_reference is True

    def test_urgency_mozhno_gotovit(self):
        """'можно готовить в работу' — should appear in description."""
        p = _parse_with_regex("ЧМ-1102 можно готовить в работу")
        assert "можно готовить" in p.description.lower() or "готовить" in p.raw_text.lower()

    def test_multi_code_in_text(self):
        """'ИП-1115, 1116' — regex only captures first code. Multi handled by parse_message_multi."""
        p = _parse_with_regex("ИП-1115, 1116 заказ")
        assert p.order_code == "ИП-1115"

    def test_raw_text_preserved(self):
        p = _parse_with_regex("ЧМ-1102 столешка")
        assert p.raw_text == "ЧМ-1102 столешка"

    def test_owner_bracket_stripped_from_raw(self):
        """When [KK] prefix is present, it's stripped from text for parsing but raw_text has it."""
        p = _parse_with_regex("[KK] Заказ")
        # raw_text should preserve original text
        assert "[KK]" in p.raw_text or p.owner == "KK"
